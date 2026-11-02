from app import ad_roles


def test_sam_from_variants():
    assert ad_roles.sam_from("alice@lab.local") == "alice"
    assert ad_roles.sam_from("LAB\\alice") == "alice"
    assert ad_roles.sam_from(" alice ") == "alice"


def test_merge_is_union_ordered_and_deduped():
    assert ad_roles.merge_roles(["approvers", "auditors", "approvers"]) == ["auditors", "approvers"]


def test_merge_drops_unknown_roles():
    assert ad_roles.merge_roles(["root", "users", ""]) == ["users"]


def test_no_mapped_roles_is_empty():
    assert ad_roles.merge_roles([]) == []

import time


def test_local_accounts_are_never_stale():
    assert ad_roles.is_stale(None) is False


def test_stale_by_age(monkeypatch):
    monkeypatch.setattr(ad_roles, "_interval_seconds", lambda: 900.0)
    assert ad_roles.is_stale(time.time() - 60) is False
    assert ad_roles.is_stale(time.time() - 1000) is True


def test_mapping_change_makes_everyone_stale(monkeypatch):
    monkeypatch.setattr(ad_roles, "_interval_seconds", lambda: 900.0)
    checked = time.time() - 5
    assert ad_roles.is_stale(checked) is False
    ad_roles.mark_mappings_changed()
    assert ad_roles.is_stale(checked) is True


def test_decide():
    assert ad_roles.decide("users", []) == ("revoked", "users")
    assert ad_roles.decide("users", ["users"]) == ("same", "users")
    assert ad_roles.decide("users", ["approvers", "users"]) == ("changed", "approvers,users")