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

    def should_close(self):
        return self.lasterror == ERROR_ALREADY_EXISTS

    def notify_existing(self):
        try:
            handle = OpenEvent(EVENT_MODIFY_STATE, False, self.event_name)
            SetEvent(handle)
            CloseHandle(handle)
        except Exception:
            pass

    def listen(self, callback):
        event = CreateEvent(None, False, False, self.event_name)

        def wait():
            while True:
                WaitForSingleObject(event, INFINITE)
                callback()

        threading.Thread(target=wait, daemon=True).start()

    def __del__(self):
        if self.mutex:
            CloseHandle(self.mutex)
