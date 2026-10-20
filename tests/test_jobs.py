from app.jobs import pick_next


def test_priority_beats_age():
    rows = [{"id": "a", "priority": 3, "created": 1}, {"id": "b", "priority": 1, "created": 5}]
    assert pick_next(rows)["id"] == "b"


def test_same_priority_oldest_first():
    rows = [{"id": "a", "priority": 2, "created": 9}, {"id": "b", "priority": 2, "created": 3}]
    assert pick_next(rows)["id"] == "b"


def test_empty():
    assert pick_next([]) is None