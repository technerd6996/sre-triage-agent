from app import rbac


def caps(role):
    return rbac.caps_for(role)


def test_matrix():
    assert "chat.create" in caps("users") and "console.view" not in caps("users")
    assert "console.act" in caps("approvers") and not any(c.startswith("kb.") for c in rbac.ALL_CAPS)
    assert caps("auditors") == {"admin.open", "monitor.view", "audit.view"}
    assert "config.manage" in caps("admin") and "users.manage" not in caps("admin")
    assert "chat.create" not in caps("admin")
    assert caps("super_admin") == rbac.ALL_CAPS


def test_roles_are_additive():
    assert caps("users,auditors") == caps("users") | caps("auditors")


def test_unknown_role_gets_nothing_but_can_sign_out():
    assert caps("wizard") == frozenset()
    assert rbac.allows("wizard", rbac.rule_for("POST", "/api/auth/logout"))
    assert not rbac.allows("wizard", rbac.rule_for("GET", "/api/admin/summary"))


def test_deny_by_default():
    assert rbac.rule_for("GET", "/api/nothing/here") is None


def test_route_rules():
    cases = [
        ("GET", "/api/admin/summary", "approvers", True), ("GET", "/api/admin/summary", "users", False),
        ("POST", "/api/admin/config", "admin", True), ("POST", "/api/admin/config", "approvers", False),
        ("GET", "/api/admin/users", "admin", False), ("GET", "/api/admin/users", "super_admin", True),
        ("GET", "/api/admin/audit", "approvers", False), ("GET", "/api/admin/audit", "auditors", True),
        ("POST", "/api/chat", "users", True), ("POST", "/api/chat", "auditors", False),
        ("POST", "/api/agent/runs/abc/decision", "approvers", True),
        ("POST", "/api/agent/runs/abc/decision", "users", False),
        ("GET", "/api/agent/runs/abc", "users", True), ("GET", "/api/ready", "users", True),
    ]
    for method, path, role, ok in cases:
        need = rbac.rule_for(method, path)
        assert need is not None, (method, path)
        assert rbac.allows(role, need) is ok, (method, path, role)


def test_home_pages():
    assert rbac.home_for("approvers") == "/" and rbac.home_for("users") == "/"
    assert rbac.home_for("admin") == "/admin" and rbac.home_for("super_admin") == "/admin"
    assert rbac.home_for("auditors") == "/admin"