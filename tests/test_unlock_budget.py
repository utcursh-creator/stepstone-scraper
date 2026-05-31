from pathlib import Path
import utils.unlock_budget as ub


def _state_path(tmp_path) -> str:
    return str(tmp_path / "unlock_counter.json")


def test_starts_at_zero_when_no_file(tmp_path):
    p = _state_path(tmp_path)
    assert ub.unlocks_today(p, today="2026-06-02") == 0


def test_increment_persists(tmp_path):
    p = _state_path(tmp_path)
    ub.record_unlock(p, today="2026-06-02")
    ub.record_unlock(p, today="2026-06-02")
    assert ub.unlocks_today(p, today="2026-06-02") == 2
    # Simulate a fresh process reading the same file
    assert ub.unlocks_today(p, today="2026-06-02") == 2


def test_resets_on_new_day(tmp_path):
    p = _state_path(tmp_path)
    ub.record_unlock(p, today="2026-06-02")
    ub.record_unlock(p, today="2026-06-02")
    assert ub.unlocks_today(p, today="2026-06-02") == 2
    # Next day → counter resets
    assert ub.unlocks_today(p, today="2026-06-03") == 0
    ub.record_unlock(p, today="2026-06-03")
    assert ub.unlocks_today(p, today="2026-06-03") == 1


def test_budget_remaining(tmp_path):
    p = _state_path(tmp_path)
    for _ in range(3):
        ub.record_unlock(p, today="2026-06-02")
    assert ub.budget_remaining(p, cap=100, today="2026-06-02") == 97


def test_budget_remaining_never_negative(tmp_path):
    p = _state_path(tmp_path)
    for _ in range(5):
        ub.record_unlock(p, today="2026-06-02")
    assert ub.budget_remaining(p, cap=3, today="2026-06-02") == 0


def test_corrupt_file_treated_as_zero(tmp_path):
    p = _state_path(tmp_path)
    Path(p).write_text("not json{")
    assert ub.unlocks_today(p, today="2026-06-02") == 0
    ub.record_unlock(p, today="2026-06-02")
    assert ub.unlocks_today(p, today="2026-06-02") == 1
