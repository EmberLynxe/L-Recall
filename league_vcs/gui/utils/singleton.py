import threading

from win32api import CloseHandle, GetLastError
from win32event import CreateEvent, CreateMutex, OpenEvent, SetEvent, WaitForSingleObject, EVENT_MODIFY_STATE, INFINITE
from winerror import ERROR_ALREADY_EXISTS

_GUID = '{13560846-8FB1-430F-B24B-2AE8A2F3CC71}'


class Singleton:
    """one instance only. second launch just pokes the first one to show its window"""

    def __init__(self):
        self.mutex = CreateMutex(None, False, f'league_vcs_{_GUID}')
        self.lasterror = GetLastError()
        self.event_name = f'league_vcs_show_{_GUID}'
        self.uninstall_event = f'league_vcs_uninstall_{_GUID}'

    def should_close(self):
        return self.lasterror == ERROR_ALREADY_EXISTS

    def notify_existing(self, uninstall=False):
        try:
            handle = OpenEvent(EVENT_MODIFY_STATE, False, self.uninstall_event if uninstall else self.event_name)
            SetEvent(handle)
            CloseHandle(handle)
        except Exception:
            pass

    def listen(self, callback, on_uninstall=None):
        for name, cb in ((self.event_name, callback), (self.uninstall_event, on_uninstall)):
            if cb:
                self._wait(CreateEvent(None, False, False, name), cb)

    @staticmethod
    def _wait(event, callback):
        def wait():
            while True:
                WaitForSingleObject(event, INFINITE)
                callback()

        threading.Thread(target=wait, daemon=True).start()

    def __del__(self):
        if self.mutex:
            CloseHandle(self.mutex)
