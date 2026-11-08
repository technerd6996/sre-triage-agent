"""More chat actions: clear temp files, clean old logs, unlock an AD account. Reading arguments, asking back, summaries."""
import pytest

from app import chat_actions
from app.tools import infra_perf

WIN = {"name": "srv1", "platform": "windows", "instance_id": "srv1"}
LIN = {"name": "lin1", "platform": "linux", "instance_id": "lin1"}


class _T:
    def invoke(self, args):
        return {}


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    chat_actions._PENDING.clear()
    monkeypatch.setattr(chat_actions, "_tool", lambda n: _T())


def _pending(card):
    return chat_actions._PENDING[card["token"]]


def test_clear_temp_files_needs_no_arguments():
    card = chat_actions.propose("please clean temp files on srv1", "e", WIN)
    assert card["tool"] == "clear_temp_files" and _pending(card)["args"] == {"target": "windows"}
    assert "temp files" in card["title"] and card["level"] == 1


def test_the_longest_phrase_wins():
    assert chat_actions.propose("clean old logs in C:\\inetpub\\logs\\LogFiles", "e", WIN)["tool"] == "clean_old_logs"
    assert chat_actions.propose("clean temp", "e", WIN)["tool"] == "clear_temp_files"


def test_clean_old_logs_reads_the_path_and_the_days(monkeypatch):
    monkeypatch.setattr(infra_perf, "_allowed_path", lambda target, path, env, default: path)
    card = chat_actions.propose("clean old logs in C:\\inetpub\\logs\\LogFiles older than 30 days", "e", WIN)
    assert _pending(card)["args"] == {"target": "windows", "path": "C:\\inetpub\\logs\\LogFiles", "older_than_days": 30}


def test_clean_old_logs_defaults_to_14_days_and_reads_a_linux_path(monkeypatch):
    monkeypatch.setattr(infra_perf, "_allowed_path", lambda target, path, env, default: path)
    card = chat_actions.propose("clean old logs in /var/log/nginx", "e", LIN)
    assert _pending(card)["args"] == {"target": "linux", "path": "/var/log/nginx", "older_than_days": 14}


def test_clean_old_logs_asks_for_a_folder_and_refuses_one_that_is_not_allowed(monkeypatch):
    with pytest.raises(chat_actions.AskBack, match="Which folder"):
        chat_actions.propose("clean old logs", "e", WIN)

    def refuse(*a):
        raise ValueError("path is not in an allowed log folder")
    monkeypatch.setattr(infra_perf, "_allowed_path", refuse)
    with pytest.raises(chat_actions.AskBack, match="allowed log folder"):
        chat_actions.propose("clean old logs in C:\\Users", "e", WIN)


@pytest.mark.parametrize("msg", ["unlock jsmith", "unlock the AD account jsmith", "please unlock account for jsmith (server: srv1)"])
def test_unlock_reads_the_username(msg):
    card = chat_actions.propose(msg, "e", WIN)
    assert card["tool"] == "unlock_ad_account" and _pending(card)["args"] == {"username": "jsmith"}


def test_unlock_asks_which_account_and_rejects_odd_names():
    with pytest.raises(chat_actions.AskBack, match="Which account"):
        chat_actions.propose("unlock", "e", WIN)
    with pytest.raises(chat_actions.AskBack, match="does not look like a username"):
        chat_actions.propose("unlock bob;drop", "e", WIN)


def test_unlock_asks_for_the_windows_host_not_the_named_server():
    modes = []

    def resolve(mode):
        modes.append(mode)
        return WIN
    card = chat_actions.propose("unlock jsmith", "e", resolve)
    assert modes == ["windows"] and card["host_mode"] == "windows"
    chat_actions.propose("restart nginx", "e", resolve)
    assert modes == ["windows", "named"]


def test_a_resolver_that_cannot_find_a_host_asks_back():
    def resolve(mode):
        raise chat_actions.AskBack("no server")
    with pytest.raises(chat_actions.AskBack, match="no server"):
        chat_actions.propose("restart nginx", "e", resolve)


def _action(tool, verb="x", name="n", label="did it"):
    return {"tool": tool, "verb": verb, "name": name, "host": "srv1", "label": label}


def test_summaries_say_what_happened_in_plain_words():
    s = chat_actions.summary
    assert "freed 12.5 MB" in s(_action("clear_temp_files"), {"freed_mb": 12.5})
    assert "removed 4 files" in s(_action("clear_temp_files"), {"files_deleted": 4})
    assert "Disk used 91% -> 70%" in s(_action("clean_old_logs", name="/var/log"),
                                         {"files_deleted": 9, "used_pct_before": 91, "used_pct_after": 70})
    assert s(_action("unlock_ad_account", name="jsmith"), {"LockedOut": False}) == "Unlocked the account jsmith."
    assert "still locked" in s(_action("unlock_ad_account", name="jsmith"), {"LockedOut": True})
    assert s(_action("clear_temp_files", label="clear temp files on srv1"), {"error": "boom"}).startswith("Could not clear temp files")


@pytest.mark.parametrize("msg", ["Can we free up space in the C: Drive?", "free up space on srv1"])
def test_free_up_space_becomes_a_temp_file_cleanup_card(msg):
    card = chat_actions.propose(msg, "e", WIN)
    assert card["tool"] == "clear_temp_files" and "free up space" in card["title"]


def test_a_plain_question_about_free_space_is_not_an_action():
    with pytest.raises(chat_actions.AskBack):
        chat_actions.propose("how much free space is left on srv1", "e", WIN)
