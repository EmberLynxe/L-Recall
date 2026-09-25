"""progress for long jobs. the gui sets a callback for its thread, the cli just gets tqdm"""
import threading
import time
from contextlib import contextmanager

from tqdm import tqdm

_local = threading.local()
# the page redraws on every update, so don't send more than this many a second
_PER_SECOND = 10


@contextmanager
def reporting(callback):
    """callback(done, total, label) for everything tracked on this thread"""
    previous = getattr(_local, 'callback', None), getattr(_local, 'last', 0)
    _local.callback, _local.last = callback, 0
    try:
        yield
    finally:
        _local.callback, _local.last = previous


def step(done, total, label=''):
    """report where we are. cheap to call in a tight loop, it throttles itself"""
    callback = getattr(_local, 'callback', None)
    if not callback:
        return
    now = time.monotonic()
    if done in (0, total) or now - _local.last >= 1 / _PER_SECOND:
        _local.last = now
        callback(done, total, label)


def track(iterable, total=None, label=''):
    if getattr(_local, 'callback', None) is None:
        yield from tqdm(iterable, total=total, desc=label or None)
        return
    if total is None:
        total = len(iterable)
    step(0, total, label)
    for done, item in enumerate(iterable, 1):
        yield item
        step(done, total, label)
