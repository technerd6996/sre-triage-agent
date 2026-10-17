"""One priority gate in front of the single Colab model (D-13).

Chat, agent steps and document ingestion all call the same /generate endpoint, which handles
one request at a time. Without a gate they queue inside Colab in arrival order. Here the lowest
priority number goes first, so an agent step never waits behind a chat message that arrived later.

Do not nest llm_slot() calls in one thread: the inner call would wait for the outer one forever.
"""
import contextlib
import heapq
import itertools
import os
import threading
import time

PRIORITY = {"resume": 0, "verify": 1, "triage": 2, "chat": 3, "ingest": 4}
CHAT_WAIT_SECONDS = int(os.getenv("LLM_CHAT_WAIT_SECONDS", "90"))
_DEFAULT = object()


class LLMBusy(Exception):
    """The model stayed busy for longer than the caller was willing to wait."""


class _Gate:
    def __init__(self, slots: int = 1):
        self._cv = threading.Condition()
        self._free = slots
        self._waiting: list = []          # heap of (priority, arrival number)
        self._seq = itertools.count()

    def acquire(self, priority: int, timeout) -> None:
        entry = (priority, next(self._seq))
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            heapq.heappush(self._waiting, entry)
            try:
                while not (self._free > 0 and self._waiting[0] == entry):
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise LLMBusy("The AI model is busy")
                    self._cv.wait(remaining)
                self._free -= 1
            finally:
                self._waiting.remove(entry)
                heapq.heapify(self._waiting)
                self._cv.notify_all()

    def release(self) -> None:
        with self._cv:
            self._free += 1
            self._cv.notify_all()

    def depth(self) -> int:
        with self._cv:
            return len(self._waiting)


GATE = _Gate(1)


@contextlib.contextmanager
def llm_slot(kind: str = "chat", wait_timeout=_DEFAULT):
    """with llm_slot("triage") as waited: ...   (waited = seconds spent in the queue).
    Chat gives up after LLM_CHAT_WAIT_SECONDS and raises LLMBusy; every other kind waits."""
    if wait_timeout is _DEFAULT:
        wait_timeout = CHAT_WAIT_SECONDS if kind == "chat" else None
    t0 = time.monotonic()
    GATE.acquire(PRIORITY.get(kind, 3), wait_timeout)
    waited = time.monotonic() - t0
    try:
        yield waited
    finally:
        GATE.release()