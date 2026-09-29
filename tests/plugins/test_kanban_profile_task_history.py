"""Tests for GET /profiles/{profile_name}/runs — the desktop Task History view's

only new backend surface (t_1d80ef7d). Read-only: no mutation endpoint exists
here. Exercises the real FastAPI router against a temp kanban DB, not a mock.
"""

from __future__ import annotations

import importlib.util
import secrets
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    mod_name = "hermes_dashboard_plugin_kanban_profile_history_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name].router

    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _insert_run(conn, task_id, *, profile, status="running", outcome=None, summary=None,
                 error=None, worker_pid=None, started_at=None, ended_at=None,
                 worker_session_id=None):
    """Insert a task_runs row directly (bypassing claim machinery) and return run_id."""
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, outcome, summary, error, claim_lock, claim_expires, "
        "worker_pid, started_at, ended_at, worker_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, profile, status, outcome, summary, error, lock, future, worker_pid,
         started_at if started_at is not None else int(time.time()), ended_at, worker_session_id),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# GET /profiles/{profile_name}/runs
# ---------------------------------------------------------------------------

def test_profile_runs_empty(client):
    """A profile with no runs returns an empty list, not a 404."""
    r = client.get("/api/plugins/kanban/profiles/coder-a/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": []}


def test_profile_runs_filters_by_profile(client):
    """Only runs belonging to the requested profile are returned."""
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect()
    task_a = kb.create_task(conn, title="Task for coder-a", assignee="coder-a")
    task_b = kb.create_task(conn, title="Task for reviewer", assignee="reviewer")

    run_a = _insert_run(conn, task_a, profile="coder-a", worker_session_id="20260907_150000_abc123")
    _insert_run(conn, task_b, profile="reviewer")
    conn.close()

    r = client.get("/api/plugins/kanban/profiles/coder-a/runs")
    assert r.status_code == 200
    body = r.json()
    assert len(body["runs"]) == 1
    row = body["runs"][0]
    assert row["id"] == run_a
    assert row["task_id"] == task_a
    assert row["task_title"] == "Task for coder-a"
    assert row["profile"] == "coder-a"
    assert row["worker_session_id"] == "20260907_150000_abc123"


def test_profile_runs_most_recent_first(client):
    """Runs come back most-recent-first (started_at DESC)."""
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect()
    task_id = kb.create_task(conn, title="Retried task", assignee="coder-a")

    old_run = _insert_run(conn, task_id, profile="coder-a", started_at=1000)
    new_run = _insert_run(conn, task_id, profile="coder-a", started_at=2000)
    conn.close()

    r = client.get("/api/plugins/kanban/profiles/coder-a/runs")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["runs"]]
    assert ids == [new_run, old_run]


def test_profile_runs_include_run_without_worker_session_id(client):
    """A crashed run with no worker_session_id (died before first heartbeat)
    still shows, with worker_session_id null — no transcript-open action."""
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect()
    task_id = kb.create_task(conn, title="Crashed early", assignee="coder-a")
    _insert_run(
        conn, task_id, profile="coder-a", status="crashed", outcome="crashed",
        error="worker exited before first heartbeat", worker_session_id=None,
    )
    conn.close()

    r = client.get("/api/plugins/kanban/profiles/coder-a/runs")
    assert r.status_code == 200
    row = r.json()["runs"][0]
    assert row["worker_session_id"] is None
    assert row["status"] == "crashed"
    assert row["error"] == "worker exited before first heartbeat"


def test_profile_runs_survives_deleted_task(client):
    """LEFT JOIN: a run whose task was hard-deleted still returns, with null
    task_title/task_status instead of disappearing."""
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect()
    task_id = kb.create_task(conn, title="Will be deleted", assignee="coder-a")
    run_id = _insert_run(conn, task_id, profile="coder-a")
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    r = client.get("/api/plugins/kanban/profiles/coder-a/runs")
    assert r.status_code == 200
    row = r.json()["runs"][0]
    assert row["id"] == run_id
    assert row["task_title"] is None
    assert row["task_status"] is None
