"""Regression tests for t_8a10877a: a kanban worker whose openai-codex OAuth
credential is rate-limited-but-PRESENT must exit KANBAN_RATE_LIMIT_EXIT_CODE
(75), not 0/1, so ``_classify_worker_exit`` records ``rate_limited`` (parks
under the dispatcher's ``rate_limit_cooldown`` guard) instead of
``clean_exit``/``protocol_violation``.

Root cause (two parts, both covered here):
  1. ``hermes_cli/auth_codex.py::_read_codex_pool_entries`` did a LOCAL-ONLY
     ``_load_auth_store`` + ``_pool_entries`` read that skipped the
     global-root borrow fallback ``read_credential_pool()`` applies
     everywhere else, so a rate-limited credential parked only at the
     global root was invisible and ``resolve_codex_runtime_credentials``
     fell through to "No Codex credentials stored" (see
     tests/hermes_cli/test_auth_profile_fallback.py for that half).
  2. ``cli.py``'s kanban exit-code translation only fires in the ``-Q``/quiet
     single-query branch (``_run_quiet_single_query``); the plain ``-q``
     branch (what the kanban worker actually spawns — see
     ``hermes_cli/kanban_db_dispatch.py::_worker_argv``, no ``-Q`` unless
     ``goal_mode``) called ``cli.chat(...)`` and returned without ever
     inspecting the failure reason, so it always fell through to Python's
     normal exit 0.
"""
from types import SimpleNamespace

import pytest

import cli as cli_mod


def test_credential_failure_exit_code_translates_rate_limit_under_kanban(monkeypatch):
    """rate_limit failure reason + HERMES_KANBAN_TASK set -> KANBAN_RATE_LIMIT_EXIT_CODE."""
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    fake_cli = SimpleNamespace(_last_credential_failure_reason="rate_limit")

    assert cli_mod._credential_failure_exit_code(fake_cli, 1) == KANBAN_RATE_LIMIT_EXIT_CODE


def test_credential_failure_exit_code_leaves_missing_credential_unchanged(monkeypatch):
    """A genuinely absent/misconfigured credential (no rate_limit reason) must NOT be
    folded into 75 — that would silently park-and-retry a config error forever."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    fake_cli = SimpleNamespace(_last_credential_failure_reason=None)

    assert cli_mod._credential_failure_exit_code(fake_cli, 1) == 1


def test_credential_failure_exit_code_noop_outside_kanban(monkeypatch):
    """Outside a kanban worker (no HERMES_KANBAN_TASK) the default exit code is untouched,
    even if a rate_limit reason happens to be set."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    fake_cli = SimpleNamespace(_last_credential_failure_reason="rate_limit")

    assert cli_mod._credential_failure_exit_code(fake_cli, 1) == 1


def test_kanban_worker_plain_q_path_exits_75_on_rate_limited_credential(monkeypatch):
    """End-to-end through ``_run_single_query_mode``'s non-quiet (plain ``-q``) branch —
    the exact branch a kanban worker spawns (no ``-Q`` unless goal_mode). A credential
    failure inside ``chat()`` returns None; previously this exited 0 with no translation
    at all (H2 in the investigation): the worker printed a message and exited cleanly,
    misclassified as ``clean_exit``/protocol_violation instead of ``rate_limited``.
    """
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = SimpleNamespace(print=lambda *_a, **_kw: None)
            self.session_id = "kanban-worker-session"
            self.agent = None
            self._last_credential_failure_reason = "rate_limit"

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _show_security_advisories(self):
            pass

        def chat(self, query, images=None):
            # Simulates _ensure_runtime_credentials() returning False inside chat():
            # chat() returns None without ever calling run_conversation.
            return None

        def _print_exit_summary(self, clear_screen=True):
            pass

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _fake_cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(query="work kanban task t_test", quiet=False, toolsets="terminal")

    assert exc_info.value.code == KANBAN_RATE_LIMIT_EXIT_CODE


def test_kanban_worker_plain_q_path_exits_1_on_missing_credential(monkeypatch):
    """Sanity: the same plain-`-q` path with a genuinely missing credential (no
    rate_limit reason) still exits 1, not 75 — folding "missing" into the rate-limit
    sentinel would make the dispatcher park-and-retry a config error forever."""

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = SimpleNamespace(print=lambda *_a, **_kw: None)
            self.session_id = "kanban-worker-session"
            self.agent = None
            self._last_credential_failure_reason = None

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _show_security_advisories(self):
            pass

        def chat(self, query, images=None):
            return None

        def _print_exit_summary(self, clear_screen=True):
            pass

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _fake_cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(query="work kanban task t_test", quiet=False, toolsets="terminal")

    assert exc_info.value.code == 1
