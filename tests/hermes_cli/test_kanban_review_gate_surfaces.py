"""CLI and tool delivery of durable same-card review-gate rejections."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def guarded_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "builder")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Guarded PR task",
            assignee="builder",
            completion_contract="acme/repo",
        )
        implementation = kb.claim_task(conn, task_id, claimer="builder:implementation")
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="reviewer:review")
        assert review is not None
        assert kb.request_changes(
            conn,
            task_id,
            reason="add regression",
            expected_run_id=review.current_run_id,
        ) == (True, "builder")
        rework = kb.claim_task(conn, task_id, claimer="builder:rework")
        assert rework is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(rework.current_run_id))
    return task_id


def test_tool_handler_returns_shared_actionable_review_gate_error(guarded_worker: str) -> None:
    from tools import kanban_tools

    output = json.loads(kanban_tools._handle_complete({"summary": "attempt bypass"}))

    assert "error" in output
    assert "kanban_request_review" in output["error"]
    with kbc.connect() as conn:
        task = kb.get_task(conn, guarded_worker)
        assert task is not None
        assert task.status == "running"


def test_cli_parser_dispatch_returns_shared_actionable_review_gate_error(guarded_worker: str) -> None:
    output = kc.run_slash(f"complete {guarded_worker} --result 'attempt bypass'")

    assert "kanban_request_review" in output
    with kbc.connect() as conn:
        task = kb.get_task(conn, guarded_worker)
        assert task is not None
        assert task.status == "running"
