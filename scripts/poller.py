"""Finds the next ticket waiting in the L1 queue. Each ticket is handed out once per session."""
from app.tools.snow import list_queue


class QueuePoller:
    def __init__(self):
        self.tried = set()

    def next_ticket(self):
        for number in list_queue():
            if number not in self.tried:
                self.tried.add(number)
                return number
        return None