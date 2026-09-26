"""progress for long jobs. the gui sets a callback for its thread, the cli just gets tqdm.
also where cancelling happens: every progress update checks whether someone hit cancel"""
import threading
import time
from contextlib import contextmanager

from tqdm import tqdm

_local = threading.local()
# the page redraws on every update, so don't send more than this many a second
_PER_SECOND = 10


class Cancelled(Exception):
    pass


@contextmanager
def reporting(callback, cancel=None):
    """callback(done, total, label) for everything tracked on this thread. cancel is a
    threading.Event, once it's set the next progress update raises Cancelled"""
    previous = getattr(_local, 'callback', None), getattr(_local, 'last', 0), getattr(_local, 'cancel', None)
    _local.callback, _local.last, _local.cancel = callback, 0, cancel
    try:
        yield
    finally:
        _local.callback, _local.last, _local.cancel = previous


def check():
    """raise Cancelled if someone asked. for spots with no progress to report"""
    cancel = getattr(_local, 'cancel', None)
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def step(done, total, label=''):
    """report where we are. cheap to call in a tight loop, it throttles itself"""
    check()
    callback = getattr(_local, 'callback', None)
    if not callback:
        return
    now = time.monotonic()
    if done in (0, total) or now - _local.last >= 1 / _PER_SECOND:
        _local.last = now
        callback(done, total, label)


def track(iterable, total=None, label=''):
    if getattr(_local, 'callback', None) is None:
        for item in tqdm(iterable, total=total, desc=label or None):
            check()
            yield item
        return
    if total is None:
        total = len(iterable)
    step(0, total, label)
    for done, item in enumerate(iterable, 1):
        yield item
        step(done, total, label)
