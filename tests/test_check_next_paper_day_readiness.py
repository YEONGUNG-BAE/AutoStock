"""Offline tests for ops/check_next_paper_day_readiness.py.

Fully deterministic and offline: git is faked via a monkeypatched ``_run_git``,
the filesystem is a tmp_path, and env vars are supplied as a plain mapping. No
network, no live KIS, no secret value ever reaches stdout/JSON.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_MOD_PATH = REPO_ROOT / "ops" / "check_next_paper_day_readiness.py"
_spec = importlib.util.spec_from_file_location("check_next_paper_day_readiness", _MOD_PATH)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


_SECRET = "SUPERSECRETKEYVALUE_must_not_leak_1234567890"


def _clean_git_responses() -> dict[tuple[str, ...], tuple[int, str]]:
    return {
        ("rev-parse", "HEAD"): (0, "a" * 40 + "\n"),
        ("status", "--short"): (0, ""),
        ("ls-files", "runtime"): (0, ""),
        ("ls-files", "--", "config/config.toml"): (0, ""),
    }


def _install_fake_git(monkeypatch, responses: dict[tuple[str, ...], tuple[int, str]]) -> None:
    def _fake(args: list[str]) -> tuple[int, str]:
        for prefix, value in responses.items():
            if tuple(args[: len(prefix)]) == prefix:
                return value
        return (0, "")

    monkeypatch.setattr(checker, "_run_git", _fake)


def _good_env() -> dict[str, str]:
    return {
        "KIS_LIVE_APP_KEY": "k" * 36,
        "KIS_LIVE_APP_SECRET": "s" * 180,
        "KIS_LIVE_ACCOUNT": "12345678-01",
        "KIS_WS_READONLY_CONFIRM": "1",
    }


def _make_repo(tmp_path: Path, monkeypatch) -> None:
    """chdir into a fake repo root with a config/config.toml file present."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text("# offline test config\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def _check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


def _evaluate(monkeypatch, tmp_path, *, environ=None, **overrides) -> dict:
    _make_repo(tmp_path, monkeypatch)
    _install_fake_git(monkeypatch, _clean_git_responses())
    kwargs = {
        "session_date": "2026-06-30",
        "run_label": "day-1",
        "duration_seconds": "120",
        "run_dir": "runtime/paper-day/2026-06-30/day-1",
        "config_path": "config/config.toml",
        "environ": environ if environ is not None else _good_env(),
    }
    kwargs.update(overrides)
    return checker.evaluate_readiness(**kwargs)


# --- happy path -------------------------------------------------------------


def test_happy_path_all_checks_pass(monkeypatch, tmp_path) -> None:
    result = _evaluate(monkeypatch, tmp_path)
    assert result["ok"] is True
    assert result["hard_failures"] == []
    assert _check(result, "repo_head_readable")["status"] == "ok"
    assert _check(result, "git_status_clean")["status"] == "ok"
    assert _check(result, "runtime_untracked")["status"] == "ok"
    assert _check(result, "config_exists")["status"] == "ok"
    assert _check(result, "config_untracked")["status"] == "ok"
    assert _check(result, "session_date_valid")["status"] == "ok"
    assert _check(result, "run_label_valid")["status"] == "ok"
    assert _check(result, "duration_valid")["status"] == "ok"
    assert _check(result, "run_dir_matches")["status"] == "ok"
    assert _check(result, "run_dir_no_stale_artifacts")["status"] == "ok"
    for name in checker._REQUIRED_ENV:
        assert _check(result, f"env:{name}")["status"] == "ok"


def test_operator_reminder_always_present(monkeypatch, tmp_path) -> None:
    result = _evaluate(monkeypatch, tmp_path)
    assert result["reminders"]
    joined = " ".join(result["reminders"])
    assert "session_state=OPEN" in joined
    assert "regular KR market session" in joined


# --- env var failures -------------------------------------------------------


def test_missing_env_var_is_hard_fail(monkeypatch, tmp_path) -> None:
    env = _good_env()
    del env["KIS_LIVE_ACCOUNT"]
    result = _evaluate(monkeypatch, tmp_path, environ=env)
    assert result["ok"] is False
    assert "env:KIS_LIVE_ACCOUNT" in result["hard_failures"]
    chk = _check(result, "env:KIS_LIVE_ACCOUNT")
    assert chk["status"] == "fail"
    assert chk["detail"] == "missing"


def test_placeholder_env_var_is_hard_fail(monkeypatch, tmp_path) -> None:
    env = _good_env()
    env["KIS_LIVE_APP_KEY"] = "YOUR_KEY"
    result = _evaluate(monkeypatch, tmp_path, environ=env)
    assert result["ok"] is False
    chk = _check(result, "env:KIS_LIVE_APP_KEY")
    assert chk["status"] == "fail"
    assert chk["detail"] == "placeholder value"
    assert _check(result, "env:KIS_LIVE_APP_KEY")["status"] == "fail"
    # The env report flags placeholder without exposing the value.
    entry = next(e for e in result["env"] if e["name"] == "KIS_LIVE_APP_KEY")
    assert entry["placeholder"] is True


def test_whitespace_contaminated_env_var_is_hard_fail(monkeypatch, tmp_path) -> None:
    env = _good_env()
    env["KIS_LIVE_APP_SECRET"] = " " + "s" * 180
    result = _evaluate(monkeypatch, tmp_path, environ=env)
    assert result["ok"] is False
    chk = _check(result, "env:KIS_LIVE_APP_SECRET")
    assert chk["status"] == "fail"
    assert "strip_same=false" in chk["detail"]


# --- secret never leaks -----------------------------------------------------


def test_secret_value_never_appears_in_text_output(monkeypatch, tmp_path) -> None:
    env = _good_env()
    env["KIS_LIVE_APP_KEY"] = _SECRET
    env["KIS_LIVE_APP_SECRET"] = _SECRET + "_secret"
    result = _evaluate(monkeypatch, tmp_path, environ=env)
    buf = io.StringIO()
    checker._emit(result, as_json=False, out=buf)
    text = buf.getvalue()
    assert _SECRET not in text
    # Only metadata is printed.
    assert "KIS_LIVE_APP_KEY: present=True" in text
    assert "length=" in text


def test_secret_value_never_appears_in_json_output(monkeypatch, tmp_path) -> None:
    env = _good_env()
    env["KIS_LIVE_APP_KEY"] = _SECRET
    result = _evaluate(monkeypatch, tmp_path, environ=env)
    buf = io.StringIO()
    checker._emit(result, as_json=True, out=buf)
    blob = buf.getvalue()
    assert _SECRET not in blob
    # Round-trips as JSON and carries only env metadata keys.
    payload = json.loads(blob)
    entry = next(e for e in payload["env"] if e["name"] == "KIS_LIVE_APP_KEY")
    assert set(entry) == {"name", "present", "length", "strip_same", "placeholder"}


def test_account_value_never_appears_in_output(monkeypatch, tmp_path) -> None:
    env = _good_env()
    env["KIS_LIVE_ACCOUNT"] = "99998888-77"
    result = _evaluate(monkeypatch, tmp_path, environ=env)
    buf = io.StringIO()
    checker._emit(result, as_json=False, out=buf)
    assert "99998888-77" not in buf.getvalue()


# --- RUN_DIR / artifact failures --------------------------------------------


def test_run_dir_mismatch_is_hard_fail(monkeypatch, tmp_path) -> None:
    result = _evaluate(
        monkeypatch,
        tmp_path,
        run_dir="runtime/paper-day/2026-06-30/WRONG",
    )
    assert result["ok"] is False
    chk = _check(result, "run_dir_matches")
    assert chk["status"] == "fail"
    assert "runtime/paper-day/2026-06-30/day-1" in chk["detail"]


def test_stale_artifacts_detected(monkeypatch, tmp_path) -> None:
    # Create the RUN_DIR with a pre-existing artifact; matches stays OK because the
    # path matches the expected layout under the chdir'd repo root.
    run_dir = tmp_path / "runtime" / "paper-day" / "2026-06-30" / "day-1"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")
    (run_dir / "db").mkdir()
    result = _evaluate(monkeypatch, tmp_path)
    assert result["ok"] is False
    chk = _check(result, "run_dir_no_stale_artifacts")
    assert chk["status"] == "fail"
    assert "summary.json" in chk["detail"]
    assert "db" in chk["detail"]
    # The layout itself is still correct.
    assert _check(result, "run_dir_matches")["status"] == "ok"


# --- invalid run variables --------------------------------------------------


def test_invalid_session_date(monkeypatch, tmp_path) -> None:
    result = _evaluate(monkeypatch, tmp_path, session_date="2026-13-40", run_dir="runtime/paper-day/2026-13-40/day-1")
    assert result["ok"] is False
    assert _check(result, "session_date_valid")["status"] == "fail"


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "12.5", ""])
def test_invalid_duration(monkeypatch, tmp_path, bad: str) -> None:
    result = _evaluate(monkeypatch, tmp_path, duration_seconds=bad)
    assert result["ok"] is False
    assert _check(result, "duration_valid")["status"] == "fail"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../escape", "has space"])
def test_invalid_run_label(monkeypatch, tmp_path, bad: str) -> None:
    result = _evaluate(
        monkeypatch,
        tmp_path,
        run_label=bad,
        run_dir=f"runtime/paper-day/2026-06-30/{bad}",
    )
    assert result["ok"] is False
    assert _check(result, "run_label_valid")["status"] == "fail"


# --- git-derived failures ---------------------------------------------------


def test_dirty_tree_is_hard_fail_without_printing_contents(monkeypatch, tmp_path) -> None:
    responses = _clean_git_responses()
    responses[("status", "--short")] = (0, " M src/foo.py\n?? bar.txt\n")
    _make_repo(tmp_path, monkeypatch)
    _install_fake_git(monkeypatch, responses)
    result = checker.evaluate_readiness(
        session_date="2026-06-30",
        run_label="day-1",
        duration_seconds="120",
        run_dir="runtime/paper-day/2026-06-30/day-1",
        config_path="config/config.toml",
        environ=_good_env(),
    )
    assert result["ok"] is False
    chk = _check(result, "git_status_clean")
    assert chk["status"] == "fail"
    assert "src/foo.py" in chk["detail"]


def test_tracked_runtime_files_hard_fail(monkeypatch, tmp_path) -> None:
    responses = _clean_git_responses()
    responses[("ls-files", "runtime")] = (0, "runtime/paper-day/old/summary.json\n")
    _make_repo(tmp_path, monkeypatch)
    _install_fake_git(monkeypatch, responses)
    result = checker.evaluate_readiness(
        session_date="2026-06-30",
        run_label="day-1",
        duration_seconds="120",
        run_dir="runtime/paper-day/2026-06-30/day-1",
        config_path="config/config.toml",
        environ=_good_env(),
    )
    assert result["ok"] is False
    assert _check(result, "runtime_untracked")["status"] == "fail"


def test_tracked_config_is_hard_fail(monkeypatch, tmp_path) -> None:
    responses = _clean_git_responses()
    responses[("ls-files", "--", "config/config.toml")] = (0, "config/config.toml\n")
    _make_repo(tmp_path, monkeypatch)
    _install_fake_git(monkeypatch, responses)
    result = checker.evaluate_readiness(
        session_date="2026-06-30",
        run_label="day-1",
        duration_seconds="120",
        run_dir="runtime/paper-day/2026-06-30/day-1",
        config_path="config/config.toml",
        environ=_good_env(),
    )
    assert result["ok"] is False
    assert _check(result, "config_untracked")["status"] == "fail"


def test_unreadable_head_is_hard_fail(monkeypatch, tmp_path) -> None:
    responses = _clean_git_responses()
    responses[("rev-parse", "HEAD")] = (128, "")
    _make_repo(tmp_path, monkeypatch)
    _install_fake_git(monkeypatch, responses)
    result = checker.evaluate_readiness(
        session_date="2026-06-30",
        run_label="day-1",
        duration_seconds="120",
        run_dir="runtime/paper-day/2026-06-30/day-1",
        config_path="config/config.toml",
        environ=_good_env(),
    )
    assert result["ok"] is False
    assert _check(result, "repo_head_readable")["status"] == "fail"


def test_missing_config_is_hard_fail(monkeypatch, tmp_path) -> None:
    # chdir to an empty dir (no config/config.toml present).
    _install_fake_git(monkeypatch, _clean_git_responses())
    monkeypatch.chdir(tmp_path)
    result = checker.evaluate_readiness(
        session_date="2026-06-30",
        run_label="day-1",
        duration_seconds="120",
        run_dir="runtime/paper-day/2026-06-30/day-1",
        config_path="config/config.toml",
        environ=_good_env(),
    )
    assert result["ok"] is False
    assert _check(result, "config_exists")["status"] == "fail"


# --- non-default config path skips the gitignore check ----------------------


def test_example_config_skips_tracking_check(monkeypatch, tmp_path) -> None:
    _make_repo(tmp_path, monkeypatch)
    (tmp_path / "config" / "config.toml.example").write_text("x\n", encoding="utf-8")
    _install_fake_git(monkeypatch, _clean_git_responses())
    result = checker.evaluate_readiness(
        session_date="2026-06-30",
        run_label="day-1",
        duration_seconds="120",
        run_dir="runtime/paper-day/2026-06-30/day-1",
        config_path="config/config.toml.example",
        environ=_good_env(),
    )
    chk = _check(result, "config_untracked")
    assert chk["status"] == "info"
    assert chk["hard"] is False
