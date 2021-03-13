import json
import os
import sys
import threading
import time
import appdirs
import confuse
from configparser import ConfigParser
from pathlib import Path
from queue import Queue
from trakt_scrobbler import logger
from trakt_scrobbler.player_monitors.monitor import Monitor

if os.name == 'posix':
    import select
    import socket
elif os.name == 'nt':
    import win32event
    import win32file
    import win32pipe
    from winerror import (ERROR_BROKEN_PIPE, ERROR_MORE_DATA, ERROR_IO_PENDING,
                          ERROR_PIPE_BUSY)


class MPVMon(Monitor):
    name = 'mpv'
    exclude_import = True
    WATCHED_PROPS = frozenset(('pause', 'path', 'working-directory',
                               'duration', 'time-pos'))
    CONFIG_TEMPLATE = {
        "ipc_path": confuse.String(default="auto-detect"),
        "poll_interval": confuse.Number(default=10),
        # seconds to wait while reading data from mpv
        "read_timeout": confuse.Number(default=2),
        # seconds to wait after one file ends to check for the next play
        # usually needed for slow mpv wrappers which may cause delay
        "restart_delay": confuse.Number(default=0.1)
    }

    def __init__(self, scrobble_queue):
        super().__init__(scrobble_queue)
        self.ipc_path = self.config['ipc_path']
        self.read_timeout = self.config['read_timeout']
        self.poll_interval = self.config['poll_interval']
        self.restart_delay = self.config['restart_delay']
        self.buffer = ''
        self.ipc_lock = threading.Lock()  # for IPC write queue
        self.poll_timer = None
        self.write_queue = Queue()
        self.sent_commands = {}
        self.command_counter = 1
        self.vars = {}

    @classmethod
    def read_player_cfg(cls, auto_keys=None):
        if sys.platform == "darwin":
            conf_path = Path.home() / ".config" / "mpv" / "mpv.conf"
        else:
            conf_path = (
                Path(appdirs.user_config_dir("mpv", roaming=True, appauthor=False))
                / "mpv.conf"
            )
        mpv_conf = ConfigParser(
            allow_no_value=True, strict=False, inline_comment_prefixes="#"
        )
        mpv_conf.optionxform = lambda option: option
        mpv_conf.read_string("[root]\n" + conf_path.read_text())

        ipc_path = mpv_conf.get("root", "input-ipc-server")
        if ipc_path[0:2] == "~~":
            ipc_path = str(conf_path.parent / ipc_path[3:])

        return {
            "ipc_path": lambda: ipc_path
        }

    def run(self):
        while True:
            if self.can_connect():
                self.update_vars()
                self.conn_loop()
                if self.vars.get('state', 0) != 0:
                    # create a 'stop' event in case the player didn't send 'end-file'
                    self.vars['state'] = 0
                    self.update_status()
                self.vars = {}
                if self.poll_timer:
                    self.poll_timer.cancel()
                time.sleep(self.restart_delay)
            else:
                logger.info('Unable to connect to MPV. Check ipc path.')
                time.sleep(self.poll_interval)

    def update_status(self):
        if not self.WATCHED_PROPS.issubset(self.vars):
            logger.warning("Incomplete media status info")
            return
        fpath = Path(self.vars['working-directory']) / Path(self.vars['path'])

        # Update last known position if player is stopped
        pos = self.vars['time-pos']
        if self.vars['state'] == 0 and self.status['state'] == 2:
            pos += round(time.time() - self.status['time'], 3)
        pos = min(pos, self.vars['duration'])

        self.status = {
            'state': self.vars['state'],
            'filepath': str(fpath),
            'position': pos,
            'duration': self.vars['duration'],
            'time': time.time()
        }
        self.handle_status_update()

    def update_vars(self):
        """Query mpv for required properties."""
        self.updated_props_count = 0
        for prop in self.WATCHED_PROPS:
            self.send_command(['get_property', prop])
        if self.poll_timer:
            self.poll_timer.cancel()
        self.poll_timer = threading.Timer(self.poll_interval, self.update_vars)
        self.poll_timer.name = 'mpvpoll'
        self.poll_timer.start()

    def handle_event(self, event):
        if event == 'end-file':
            # Since the player might be shutting down, we can't update vars.
            # Reuse the previous self.vars and only update the state value.
            # This might be inaccurate in terms of position, which is why
            # regular polling is needed
            self.vars['state'] = 0
            self.update_status()
        elif event == 'pause':
            self.vars['state'] = 1
            self.update_vars()
        elif event == 'unpause' or event == 'playback-restart':
            self.vars['state'] = 2
            self.update_vars()

    def handle_cmd_response(self, resp):
        command = self.sent_commands.pop(resp['request_id'])
        if resp['error'] != 'success':
            logger.error(f'Error with command {command!s}. Response: {resp!s}')
            return
        elif command[0] != 'get_property':
            return
        param = command[1]
        data = resp['data']
        if param == 'pause':
            self.vars['state'] = 1 if data else 2
        if param in self.WATCHED_PROPS:
            self.vars[param] = data
            self.updated_props_count += 1
        if self.updated_props_count == len(self.WATCHED_PROPS):
            self.update_status()
            # resetting self.vars to {} here is a bad idea.

    def on_data(self, data):
        self.buffer = self.buffer + data.decode('utf-8')
        while True:
            line_end = self.buffer.find('\n')
            if line_end == -1:
                # partial line received
                # self.on_line() is called in next data batch
                break
            else:
                self.on_line(self.buffer[:line_end])  # doesn't include \n
                self.buffer = self.buffer[line_end + 1:]  # doesn't include \n

    def on_line(self, line):
        try:
            mpv_json = json.loads(line)
        except json.JSONDecodeError:
            logger.warning('Invalid JSON received. Skipping. ' + line, exc_info=True)
            return
        if 'event' in mpv_json:
            self.handle_event(mpv_json['event'])
        elif 'request_id' in mpv_json:
            self.handle_cmd_response(mpv_json)

    def send_command(self, elements):
        with self.ipc_lock:
            command = {'command': elements, 'request_id': self.command_counter}
            self.sent_commands[self.command_counter] = elements
            self.command_counter += 1
            self.write_queue.put(str.encode(json.dumps(command) + '\n'))


class MPVPosixMon(MPVMon):
    exclude_import = os.name != 'posix'

    def __init__(self, scrobble_queue):
        super().__init__(scrobble_queue)
        self.sock = socket.socket(socket.AF_UNIX)

    def can_connect(self):
        sock = socket.socket(socket.AF_UNIX)
        errno = sock.connect_ex(self.ipc_path)
        sock.close()
        return errno == 0

    def conn_loop(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.ipc_path)
        self.is_running = True
        sock_list = [self.sock]
        while self.is_running:
            r, _, _ = select.select(sock_list, [], [], self.read_timeout)
            if r:  # r == [self.sock]
                # socket has data to be read
                data = self.sock.recv(4096)
                if len(data) == 0:
                    # EOF reached
                    self.is_running = False
                    break
                self.on_data(data)
            while not self.write_queue.empty():
                # block until self.sock can be written to
                select.select([], sock_list, [])
                try:
                    self.sock.sendall(self.write_queue.get_nowait())
                except BrokenPipeError:
                    self.is_running = False
        self.sock.close()
        logger.debug('Sock closed')


class MPVWinMon(MPVMon):
    exclude_import = os.name != 'nt'

    def __init__(self, scrobble_queue):
        super().__init__(scrobble_queue)
        self.file_handle = None

    def can_connect(self):
        return win32file.GetFileAttributes((self.ipc_path)) == \
            win32file.FILE_ATTRIBUTE_NORMAL

    def _transact(self, write_data):
        """Wrapper over TransactNamedPipe"""
        read_buf = win32file.AllocateReadBuffer(512)
        err, data = win32pipe.TransactNamedPipe(self.file_handle, write_data, read_buf)
        while err == ERROR_MORE_DATA:
            err, d = win32file.ReadFile(self.file_handle, read_buf)
            data += d
        return data

    def _read_all_data(self):
        """Read all the remaining data on the pipe"""
        data = b""
        read_buf = win32file.AllocateReadBuffer(512)
        while win32file.GetFileSize(self.file_handle):
            _, d = win32file.ReadFile(self.file_handle, read_buf)
            data += d
        return data

    def _call(self, method, *args, max_retries=5):
        """Call a pipe API method and retry if necessary"""
        try:
            return method(*args)
        except win32file.error as e:
            if e.args[0] == ERROR_BROKEN_PIPE:
                self.is_running = False
            elif e.args[0] == ERROR_PIPE_BUSY and max_retries != 0:
                # something in the pipe, read the data and retry
                data = self._call(self._read_all_data)
                if not self.is_running:
                    return
                if data:
                    self.on_data(data)
                return self._call(method, *args, max_retries=max_retries - 1)
            else:
                raise

    def conn_loop(self):
        self.file_handle = win32file.CreateFile(
            self.ipc_path,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            win32file.FILE_FLAG_OVERLAPPED,
            None
        )

        # needed for blocking on read
        overlapped = win32file.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, 0, 0, None)

        # needed for transactions
        win32pipe.SetNamedPipeHandleState(
            self.file_handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        read_buf = win32file.AllocateReadBuffer(1024)
        self.is_running = True
        while self.is_running:
            val = self._call(win32file.ReadFile, self.file_handle, read_buf, overlapped)
            if not self.is_running:
                break
            err, data = val
            if err != 0 and err != ERROR_IO_PENDING:
                logger.warning(f"Unexpected read result {err}. Quitting.")
                logger.debug(f"data={bytes(data)}")
                self.is_running = False
                break
            if err == ERROR_IO_PENDING:
                err = win32event.WaitForSingleObject(
                    overlapped.hEvent, self.read_timeout)

            if err == win32event.WAIT_OBJECT_0:  # data is available
                data = bytes(data)
                line = data[:data.find(b"\n")]
                self.on_line(line)

            while not self.write_queue.empty():
                # first see if mpv sent some data that needs to be read
                data = self._call(self._read_all_data)
                if not self.is_running:
                    break
                if data:
                    self.on_data(data)
                # cancel all remaining reads/writes. Should be benign
                win32file.CancelIo(self.file_handle)

                write_data = self.write_queue.get_nowait()
                data = self._call(self._transact, write_data)
                if not self.is_running:
                    break
                self.on_line(data[:-1])

        self.is_running = False
        self.file_handle.close()
        self.file_handle = None
        logger.debug('Pipe closed.')
