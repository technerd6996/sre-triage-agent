from app.api import admin


def _rows(monkeypatch, rows):
    monkeypatch.setattr(admin.store, "llm_rows_since", lambda cutoff: rows)


def test_cost_groups_by_skill_and_counts_runs(monkeypatch):
    _rows(monkeypatch, [
        {"skill": "windows-service-down", "run_id": "r1", "tokens_in": 800, "tokens_out": 100,
         "gen_s": 2.0, "wasted_tokens": 0},
        {"skill": "windows-service-down", "run_id": "r1", "tokens_in": 500, "tokens_out": 50,
         "gen_s": 1.0, "wasted_tokens": 50},
        {"skill": "disk-cleanup", "run_id": "r2", "tokens_in": 300, "tokens_out": 100,
         "gen_s": 1.0, "wasted_tokens": 0},
    ])
    out = admin.token_cost(days=7)
    top = out["rows"][0]
    assert top["skill"] == "windows-service-down"
    assert top["runs"] == 1 and top["calls"] == 2
    assert top["tokens_total"] == 1450
    assert top["wasted_pct"] == round(100 * 50 / 1450, 1)


def test_budget_warns_then_reports_over(monkeypatch):
    _rows(monkeypatch, [{"tokens_in": 900, "tokens_out": 200}])
    monkeypatch.setattr(admin.config, "get", lambda key, *a: {"budget.daily_tokens": 1000,
                                                              "budget.warn_pct": 80}.get(key, 0))
    assert admin.token_budget()["status"] == "over"
    _rows(monkeypatch, [{"tokens_in": 500, "tokens_out": 300}])
    assert admin.token_budget()["status"] == "warn"


def test_budget_off_by_default(monkeypatch):
    _rows(monkeypatch, [{"tokens_in": 5000, "tokens_out": 5000}])
    monkeypatch.setattr(admin.config, "get", lambda key, *a: 0)
    out = admin.token_budget()
    assert out["status"] == "off" and out["enforced"] is False