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

The two end-to-end tests below drive the REAL credential-resolution chain
(``CLIAgentSetupMixin._ensure_runtime_credentials`` -> ``resolve_runtime_provider``
-> ``resolve_codex_runtime_credentials``) against a temp ``HERMES_HOME`` auth
store, through ``cli.main()``'s plain ``-q`` single-query path — the exact
shape a kanban worker spawns. A break anywhere in that chain (the pool-read
fallback, ``is_rate_limited_auth_error`` classification, or the exit-code
translation) fails these tests, unlike a seam test that stops short of one
of those hops.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli as cli_mod
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


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


# ---------------------------------------------------------------------------
# Connected E2E: real credential resolution through the plain -q worker path
# ---------------------------------------------------------------------------


def _codex_jwt(email: str) -> str:
    import base64
    import json as _json

    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(_json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@pytest.fixture()
def codex_home(tmp_path, monkeypatch):
    """Isolated single-root HERMES_HOME (no profiles) for a real auth.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _write_auth_store(path: Path, payload: dict) -> None:
    import json as _json

    path.write_text(_json.dumps(payload, indent=2))


class _RealCredentialWorkerCLI(CLIAgentSetupMixin):
    """Minimal HermesCLI stand-in that uses the REAL ``_ensure_runtime_credentials``
    (bound from ``CLIAgentSetupMixin``) but stubs everything else a kanban worker's
    plain ``-q`` single-query path touches, so the credential-resolution chain runs
    for real while agent construction / networking / display stay out of scope."""

    def __init__(self, **_kwargs):
        self.requested_provider = "openai-codex"
        self._explicit_api_key = None
        self._explicit_base_url = None
        self._fallback_model = None
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


def test_kanban_worker_plain_q_path_exits_75_on_rate_limited_credential(codex_home, monkeypatch):
    """Real rate-limited-but-PRESENT openai-codex credential in a temp HERMES_HOME,
    driven through the real _ensure_runtime_credentials() -> resolve_runtime_provider()
    -> resolve_codex_runtime_credentials() chain and cli.main()'s plain -q path (the
    exact shape a kanban worker spawns, per kanban_db_dispatch.py::_worker_argv)."""
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    # resolve_codex_runtime_credentials() always asks the live usage endpoint whether a
    # pool-exhausted credential's quota already recovered before honoring the persisted
    # cooldown; stub it so the test never depends on real network access and stays fast.
    monkeypatch.setattr("hermes_cli.auth._probe_codex_quota_restored", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.auth_codex._probe_codex_quota_restored", lambda *a, **kw: None)

    # _ensure_runtime_credentials() prints the failure via cli.ChatConsole() (a module-level
    # symbol, not self.console); capture it to assert the message says rate-limited, not
    # "No Codex credentials stored".
    printed_messages: list[str] = []
    monkeypatch.setattr(
        cli_mod, "ChatConsole",
        lambda: SimpleNamespace(print=lambda *a, **_kw: printed_messages.append(
            " ".join(str(x) for x in a))))

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
    _write_auth_store(codex_home / "auth.json", {
        "version": 1, "credential_pool": {"openai-codex": [entry]}, "providers": {},
    })

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(cli_mod, "HermesCLI", _RealCredentialWorkerCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _fake_cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(query="work kanban task t_test", quiet=False, toolsets="terminal")

    assert exc_info.value.code == KANBAN_RATE_LIMIT_EXIT_CODE
    joined = " ".join(printed_messages)
    assert "No Codex credentials stored" not in joined
    assert any(kw in joined.lower() for kw in ("rate limit", "rate-limit", "quota", "usage limit"))


def test_kanban_worker_plain_q_path_unchanged_on_absent_credential(codex_home, monkeypatch):
    """Sanity: with NO openai-codex credential anywhere (real resolution raises the
    plain 'missing' AuthError), the plain -q path must NOT translate to 75 — it keeps
    the pre-fix behavior of falling through without a rate-limit sys.exit call, and
    the message is still the plain missing-credential one."""
    _write_auth_store(codex_home / "auth.json", {
        "version": 1, "credential_pool": {}, "providers": {},
    })

    printed_messages: list[str] = []
    monkeypatch.setattr(
        cli_mod, "ChatConsole",
        lambda: SimpleNamespace(print=lambda *a, **_kw: printed_messages.append(
            " ".join(str(x) for x in a))))

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(cli_mod, "HermesCLI", _RealCredentialWorkerCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _fake_cli: None)

    # No SystemExit raised at all: the plain -q path only sys.exit()s on a recorded
    # rate_limit failure; a genuinely missing credential falls through unchanged.
    cli_mod.main(query="work kanban task t_test", quiet=False, toolsets="terminal")

    assert "No Codex credentials stored" in " ".join(printed_messages)
