import pytest

from app.llm.gate import LLMBusy, _Gate


def test_grow_lets_a_second_request_in():
    g = _Gate(1)
    g.acquire(0, None)
    g.set_slots(2)
    g.acquire(0, 0.1)            # would raise LLMBusy with one slot
    g.release()
    g.release()
    g.set_slots(1)
    assert g._free == 1


def test_shrink_drains_before_admitting():
    g = _Gate(2)
    g.acquire(0, None)
    g.acquire(0, None)
    g.set_slots(1)               # two running, one allowed: free goes to -1
    g.release()
    with pytest.raises(LLMBusy):
        g.acquire(0, 0.05)       # still one running, so full
    g.release()
    g.acquire(0, 0.1)            # now there is room

def test_interactive_runs_while_background_is_capped():
    g = _Gate(2)
    g.set_reserved(1)
    g.acquire(2, None, True)                 # one background run holds its allowed slot
    with pytest.raises(LLMBusy):
        g.acquire(2, 0.05, True)             # a second background request may not take the reserved slot
    g.acquire(1, 0.1)                        # a title still gets in at once


def test_capped_background_waiter_does_not_block_an_interactive_one():
    import threading
    g = _Gate(2)
    g.set_reserved(1)
    g.acquire(2, None, True)
    def capped_waiter():
        with pytest.raises(LLMBusy):         # it never gets the reserved slot, so it times out
            g.acquire(2, 0.3, True)
    t = threading.Thread(target=capped_waiter, daemon=True)
    t.start()                                # waits, because it is capped
    import time; time.sleep(0.05)
    g.acquire(3, 0.1)                        # lower priority, but eligible, so it goes first
    t.join()


def test_no_reserved_slots_behaves_as_before():
    g = _Gate(2)
    g.acquire(2, None, True)
    g.acquire(2, 0.1, True)                  # both slots usable by background


def test_chat_slot_no_longer_raises_unbound_error(monkeypatch):
    from app.llm import gate
    with gate.llm_slot("chat", wait_timeout=0.1):
        pass
    with gate.llm_slot("title"):
        pass
