"""One priority gate in front of the single Colab model (D-13).

Chat, agent steps and document ingestion all call the same /generate endpoint, which handles
one request at a time. Without a gate they queue inside Colab in arrival order. Here the lowest
priority number goes first, so an agent step never waits behind a chat message that arrived later.

Do not nest llm_slot() calls in one thread: the inner call would wait for the outer one forever.
"""
import contextlib
from app import config
import itertools
import os
import threading
import time

PRIORITY = {"resume": 0, "verify": 1, "approver": 1, "title": 1, "rerank": 1, "triage": 2, "chat": 3, "ingest": 4}
BACKGROUND = {"triage", "ingest"}      # may not use the slots held back by model.reserved_slots
_DEFAULT = object()


class LLMBusy(Exception):
    """The model stayed busy for longer than the caller was willing to wait."""


class _Gate:
    def __init__(self, slots: int = 1):
        self._cv = threading.Condition()
        self._slots = slots
        self._free = slots
        self._reserved = 0
        self._bg_held = 0
        self._waiting: list = []          # (priority, arrival number, background)
        self._seq = itertools.count()

    def _bg_cap(self) -> int:
        return max(1, self._slots - self._reserved)

    def _next(self):
        """The first waiter that may run now: highest priority whose kind is allowed to take a slot."""
        if self._free <= 0:
            return None
        for e in sorted(self._waiting):
            if not e[2] or self._bg_held < self._bg_cap():
                return e
        return None

    def acquire(self, priority: int, timeout, background: bool = False) -> None:
        entry = (priority, next(self._seq), bool(background))
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            self._waiting.append(entry)
            try:
                while self._next() != entry:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise LLMBusy("The AI model is busy")
                    self._cv.wait(remaining)
                self._free -= 1
                if background:
                    self._bg_held += 1
            finally:
                self._waiting.remove(entry)
                self._cv.notify_all()

    def release(self, background: bool = False) -> None:
        with self._cv:
            self._free += 1
            if background:
                self._bg_held = max(0, self._bg_held - 1)
            self._cv.notify_all()

    def depth(self) -> int:
        with self._cv:
            return len(self._waiting)

    def set_slots(self, n: int) -> None:
        """Change the number of concurrent requests. Applies at once; running requests finish normally."""
        n = max(1, int(n))
        with self._cv:
            if n != self._slots:
                self._free += n - self._slots      # can go negative while extra requests drain
                self._slots = n
                self._cv.notify_all()

    def set_reserved(self, n: int) -> None:
        """Slots kept free for interactive kinds (title, approver, resume, verify, chat). 0 = no lane."""
        n = max(0, int(n))
        with self._cv:
            if n != self._reserved:
                self._reserved = n
                self._cv.notify_all()


GATE = _Gate(1)


@contextlib.contextmanager
def llm_slot(kind: str = "chat", wait_timeout=_DEFAULT):
    """with llm_slot("triage") as waited: ...   (waited = seconds spent in the queue).
    Chat and title give up after limits.chat_wait_seconds and raise LLMBusy; every other kind waits."""
    if wait_timeout is _DEFAULT:
        wait_timeout = config.get("limits.chat_wait_seconds") if kind in ("chat", "title") else None
    try:
        GATE.set_slots(config.get("model.slots"))   # cached for 5 s, so this is cheap
        GATE.set_reserved(config.get("model.reserved_slots"))
    except Exception:
        pass
    bg = kind in BACKGROUND
    t0 = time.monotonic()
    GATE.acquire(PRIORITY.get(kind, 3), wait_timeout, bg)
    waited = time.monotonic() - t0
    try:
        yield waited
    finally:
        GATE.release(bg)
