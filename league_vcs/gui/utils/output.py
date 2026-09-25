"""per-thread stdout capture.

redirect_stdout swaps the stream for the whole process, so two things running at once
print into the wrong window (or a dead one). this doesn't.
"""
import sys
import threading
from contextlib import contextmanager

_local = threading.local()
_install_lock = threading.Lock()


class _Router:
    def __init__(self, fallback):
        self._fallback = fallback

    def _target(self):
        return getattr(_local, 'target', None) or self._fallback

    def write(self, s):
        target = self._target()
        return target.write(s) if target is not None else len(s)

    def flush(self):
        target = self._target()
        if target is not None and hasattr(target, 'flush'):
            target.flush()

    def __getattr__(self, name):
        return getattr(self._fallback, name)


def _install():
    with _install_lock:
        if not isinstance(sys.stdout, _Router):
            sys.stdout = _Router(sys.stdout)
        if not isinstance(sys.stderr, _Router):
            sys.stderr = _Router(sys.stderr)


@contextmanager
def capture(target):
    _install()
    previous = getattr(_local, 'target', None)
    _local.target = target
    try:
        yield
    finally:
        _local.target = previous
