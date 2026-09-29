"""Regression tests for t_8a10877a: a kanban worker whose openai-codex OAuth
credential is rate-limited-but-PRESENT must exit KANBAN_RATE_LIMIT_EXIT_CODE
(75), not 0, so ``_classify_worker_exit`` records ``rate_limited`` (parks
under the dispatcher's ``rate_limit_cooldown`` guard) instead of
``clean_exit``/``protocol_violation``.

Root cause (two parts, both covered by a single connected chain per test):
  H1. ``hermes_cli/auth_codex.py::_read_codex_pool_entries`` did a LOCAL-ONLY
      ``_load_auth_store`` + ``_pool_entries`` read that skipped the
      global-root borrow fallback ``read_credential_pool()`` applies
      everywhere else, so a rate-limited credential parked only at the
      global root (the profile has zero local openai-codex rows — the same
      "borrow" shape any profile worker relies on) was invisible and
      ``resolve_codex_runtime_credentials`` fell through to
      "No Codex credentials stored".
  H2. ``cli.py``'s kanban exit-code translation only fired in the ``-Q``/quiet
      single-query branch (``_run_quiet_single_query``); the plain ``-q``
      branch (what the kanban worker actually spawns — see
      ``hermes_cli/kanban_db_dispatch.py::_worker_argv``, no ``-Q`` unless
      ``goal_mode``) called ``cli.chat(...)`` and returned without ever
      inspecting the failure reason, so it always fell through to Python's
      normal exit 0.

Each test below drives the full chain in one shot: an active NAMED PROFILE
with no local openai-codex rows, a credential (or lack of one) parked only
at the global root, through the REAL ``CLIAgentSetupMixin._ensure_runtime_credentials``
-> ``resolve_runtime_provider`` -> ``resolve_codex_runtime_credentials`` ->
``read_credential_pool`` global-root-fallback chain, and then through
``cli.main()``'s plain ``-q`` single-query path (the exact shape a kanban
worker spawns). A break anywhere in that chain — the pool-read fallback,
``is_rate_limited_auth_error`` classification, or the exit-code translation —
fails these tests, unlike a seam test that stops short of one of those hops.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli as cli_mod
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


def _codex_jwt(email: str) -> str:
    import base64
    import json as _json

    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(_json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def _write_auth_store(path: Path, payload: dict) -> None:
    import json as _json

    path.write_text(_json.dumps(payload, indent=2))


@pytest.fixture()
def codex_profile_env(tmp_path, monkeypatch):
    """Active NAMED PROFILE mounted under a global root, mirroring the real
    "profile worker borrowing a global-root credential" layout (H1's shape).

    * Path.home() -> tmp_path
    * Global root -> tmp_path/.hermes                    (may own a credential)
    * Profile     -> tmp_path/.hermes/profiles/coder      (active; HERMES_HOME
      points here; has ZERO local openai-codex rows in every test below, so
      any visible credential must have come through the borrow fallback)
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    _write_auth_store(profile_dir / "auth.json", {
        "version": 1, "credential_pool": {}, "providers": {},
    })
    return {"global": global_root, "profile": profile_dir}


class _RealCredentialWorkerCLI(CLIAgentSetupMixin):
    """Minimal HermesCLI stand-in that uses the REAL ``_ensure_runtime_credentials``
    (bound from ``CLIAgentSetupMixin``) but stubs everything else a kanban worker's
    plain ``-q`` single-query path touches, so the credential-resolution chain runs
    for real while agent construction / networking / display stay out of scope.
    """

    # Mirrors CLIChatTurnMixin's class default; upstream's _single_query_exit_code reads
    # this as a bare attribute and a real HermesCLI inherits it from that mixin.
    _last_turn_result = None

    def __init__(self, **_kwargs):
        self.requested_provider = "openai-codex"
        self._explicit_api_key = None
        self._explicit_base_url = None
        self._fallback_model = None
        # Upstream's _ensure_runtime_credentials snapshots self.model / self.provider at
        # entry; a real HermesCLI always has both. Without them the mixin raises
        # AttributeError before the credential chain under test ever runs.
        self.model = None
        self.provider = "openai-codex"
        self._explicit_reasoning_config = None
        self.console = SimpleNamespace(print=lambda *_a, **_kw: None)
        self.session_id = "kanban-worker-session"
        self.agent = None

    def _claim_active_session(self, surface, *, stderr=False):
        return True

    def _show_security_advisories(self):
        pass

    def chat(self, query, images=None):
        # Mirrors CLIChatTurnMixin.chat()'s first line: a credential failure
        # returns None before ever reaching run_conversation.
        if not self._ensure_runtime_credentials():
            return None
        raise AssertionError("credential resolution should have failed in this test")

    def _print_exit_summary(self, clear_screen=True):
        pass


def _run_kanban_worker(monkeypatch, printed_messages):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(cli_mod, "HermesCLI", _RealCredentialWorkerCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _fake_cli: None)
    monkeypatch.setattr(
        cli_mod, "ChatConsole",
        lambda: SimpleNamespace(print=lambda *a, **_kw: printed_messages.append(
            " ".join(str(x) for x in a))))
    # resolve_codex_runtime_credentials() always asks the live usage endpoint whether a
    # pool-exhausted credential's quota already recovered before honoring the persisted
    # cooldown; stub it so the test never depends on real network access and stays fast.
    monkeypatch.setattr("hermes_cli.auth._probe_codex_quota_restored", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.auth_codex._probe_codex_quota_restored", lambda *a, **kw: None)


def test_kanban_worker_exits_75_on_rate_limited_credential_borrowed_from_global_root(
        codex_profile_env, monkeypatch):
    """H1+H2 connected: a rate-limited-but-PRESENT openai-codex credential parked
    ONLY at the global root (the active named profile has zero local openai-codex
    rows — the borrow-fallback shape) must surface through the real credential
    chain and the plain -q kanban worker path as exit 75 with a rate-limit message,
    never "No Codex credentials stored".
    """
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    entry = {
        "id": "codex-1",
        "label": "codex@example.com",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": _codex_jwt("codex@example.com"),
        "refresh_token": "refresh-token",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "last_refresh": "2026-06-15T10:00:00Z",
        "last_status": "exhausted",
        "last_status_at": 1_800_000_000.0,
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_message": "The usage limit has been reached",
        "last_error_reset_at": 4_800_000_000.0,  # far future: never resolves as restored
    }
    # Global root OWNS the credential; the active profile has NO local openai-codex
    # rows at all (borrows via read_credential_pool's per-provider fallback).
    _write_auth_store(codex_profile_env["global"] / "auth.json", {
        "version": 1, "credential_pool": {"openai-codex": [entry]}, "providers": {},
    })

    printed_messages: list[str] = []
    _run_kanban_worker(monkeypatch, printed_messages)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(query="work kanban task t_test", quiet=False, toolsets="terminal")

    assert exc_info.value.code == KANBAN_RATE_LIMIT_EXIT_CODE
    joined = " ".join(printed_messages)
    assert "No Codex credentials stored" not in joined
    assert any(kw in joined.lower() for kw in ("rate limit", "rate-limit", "quota", "usage limit"))


def test_kanban_worker_unchanged_on_credential_absent_from_profile_and_global_root(
        codex_profile_env, monkeypatch):
    """Sanity, same connected chain: with NO openai-codex credential anywhere (profile
    OR global root), the plain -q path must NOT translate to 75 (rate limit).

    Upstream later tightened this: a missing/rejected credential is a TERMINAL
    provider failure (``KANBAN_TERMINAL_PROVIDER_EXIT_CODE`` = 78) so the dispatcher
    parks the card after ONE spawn instead of re-spawning into the same wall until
    ``kanban.failure_limit`` is spent. The invariant this test protects is unchanged
    (a missing credential is never a 75); only the fall-through became an explicit 78.
    """
    _write_auth_store(codex_profile_env["global"] / "auth.json", {
        "version": 1, "credential_pool": {}, "providers": {},
    })

    printed_messages: list[str] = []
    _run_kanban_worker(monkeypatch, printed_messages)

    # A genuinely missing credential is terminal, not rate-limited: exit 78, never 75.
    import pytest as _pytest
    from hermes_cli.kanban_db import (
        KANBAN_RATE_LIMIT_EXIT_CODE, KANBAN_TERMINAL_PROVIDER_EXIT_CODE,
    )
    with _pytest.raises(SystemExit) as _exit:
        cli_mod.main(query="work kanban task t_test", quiet=False, toolsets="terminal")
    assert _exit.value.code != KANBAN_RATE_LIMIT_EXIT_CODE
    assert _exit.value.code == KANBAN_TERMINAL_PROVIDER_EXIT_CODE

    assert "No Codex credentials stored" in " ".join(printed_messages)
