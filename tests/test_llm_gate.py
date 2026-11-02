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