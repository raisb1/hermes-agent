"""Behavioral regressions for reopening an approved Kanban card for rework."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_rework as kbr
from hermes_cli import kanban as kc


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _approved_task(conn, *, parents: list[str] | None = None) -> str:
    task_id = kb.create_task(conn, title="implementation", assignee="builder", parents=parents or ())
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="Implementation is ready for review.",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None
    assert kb.complete_task(
        conn,
        task_id,
        summary="Approved after independent verification.",
        expected_run_id=review.current_run_id,
    )
    return task_id


def test_reopen_done_restores_original_implementer_and_invalidates_descendants(conn):
    task_id = _approved_task(conn)
    qa_id = kb.create_task(conn, title="QA", assignee="qa", parents=[task_id])
    assert kb.complete_task(conn, qa_id)
    release_id = kb.create_task(conn, title="release", assignee="release", parents=[qa_id])
    assert kb.complete_task(conn, release_id)
    before = conn.execute(
        "SELECT workspace_kind, workspace_path, branch_name, project_id, completion_contract, "
        "pr_acceptance_policy FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()

    assert kbr.reopen_task_for_rework(
        conn, task_id, reason="Reviewer found a regression.", author="triage",
    ) == (True, "builder")

    reopened = kb.get_task(conn, task_id)
    assert reopened is not None
    assert reopened.status == "ready"
    assert reopened.assignee == "builder"
    assert reopened.completed_at is None
    after = conn.execute(
        "SELECT workspace_kind, workspace_path, branch_name, project_id, completion_contract, "
        "pr_acceptance_policy FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert tuple(after) == tuple(before)

    rework_event = [e for e in kb.list_events(conn, task_id) if e.kind == "rework_requested"][-1]
    assert rework_event.payload == {
        "reason": "Reviewer found a regression.",
        "author": "triage",
        "implementer": "builder",
        "prior_status": "done",
        "status": "ready",
    }
    comments = kb.list_comments(conn, task_id)
    assert any("REWORK REQUESTED: Reviewer found a regression." in c.body for c in comments)
    assert all(kb.get_task(conn, child_id).status == "todo" for child_id in (qa_id, release_id))
    assert all(
        any(e.kind == "descendant_invalidated" for e in kb.list_events(conn, child_id))
        for child_id in (qa_id, release_id)
    )


def test_reopen_done_regates_to_todo_when_parent_is_unsatisfied(conn):
    prerequisite = kb.create_task(conn, title="prerequisite", assignee="planner")
    assert kb.complete_task(conn, prerequisite)
    task_id = _approved_task(conn, parents=[prerequisite])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (prerequisite,))

    assert kbr.reopen_task_for_rework(
        conn, task_id, reason="Upstream contract changed.", author="triage",
    ) == (True, "builder")
    reopened = kb.get_task(conn, task_id)
    assert reopened is not None and reopened.status == "todo"


@pytest.mark.parametrize("reason", ["", "   "])
def test_reopen_done_rejects_blank_reason_without_mutation(conn, reason):
    task_id = _approved_task(conn)
    before = (kb.get_task(conn, task_id), kb.list_events(conn, task_id), kb.list_comments(conn, task_id))

    assert kbr.reopen_task_for_rework(conn, task_id, reason=reason, author="triage") == (
        False, "reason is required",
    )
    after = (kb.get_task(conn, task_id), kb.list_events(conn, task_id), kb.list_comments(conn, task_id))
    assert after == before


def test_reopen_done_rejects_wrong_status_and_malformed_latest_provenance(conn):
    assert kbr.reopen_task_for_rework(
        conn, "t_missing", reason="Missing task.", author="triage",
    ) == (False, "task not found")

    ready_id = kb.create_task(conn, title="not done", assignee="builder")
    assert kbr.reopen_task_for_rework(
        conn, ready_id, reason="Wrong state.", author="triage",
    ) == (False, "task is not done")

    task_id = _approved_task(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET payload = '{}' WHERE id = ("
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1)",
            (task_id,),
        )
    before_events = kb.list_events(conn, task_id)
    assert kbr.reopen_task_for_rework(
        conn, task_id, reason="Missing provenance.", author="triage",
    ) == (False, "review handoff has no valid implementer provenance")
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.list_events(conn, task_id) == before_events


def test_reopen_done_rejects_target_live_run_or_claim_without_reclaiming(conn):
    task_id = _approved_task(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET current_run_id = 999, claim_lock = 'worker:lock', worker_pid = 12345 "
            "WHERE id = ?",
            (task_id,),
        )

    assert kbr.reopen_task_for_rework(
        conn, task_id, reason="Cannot steal a worker.", author="triage",
    ) == (False, "task has a conflicting live run or claim")
    unchanged = kb.get_task(conn, task_id)
    assert unchanged is not None and unchanged.status == "done"
    assert unchanged.claim_lock == "worker:lock"


def test_reopen_done_commits_audit_before_terminating_invalidated_worker(conn, tmp_path, monkeypatch):
    task_id = _approved_task(conn)
    qa_id = kb.create_task(conn, title="running QA", assignee="qa", parents=[task_id])
    claim = kb.claim_task(conn, qa_id, claimer="qa:1")
    assert claim is not None
    kbd._set_worker_pid(conn, qa_id, 424242)
    terminations: list[tuple] = []

    def fake_terminate(pid, claim_lock, **kwargs):
        side = kbc.connect(tmp_path / "kanban.db")
        try:
            assert any(e.kind == "rework_requested" for e in kb.list_events(side, task_id))
            assert any(e.kind == "descendant_invalidated" for e in kb.list_events(side, qa_id))
        finally:
            side.close()
        terminations.append((pid, claim_lock))

    monkeypatch.setattr(kbr, "_terminate_reclaimed_worker", fake_terminate)
    assert kbr.reopen_task_for_rework(
        conn, task_id, reason="QA must rerun.", author="triage",
    ) == (True, "builder")
    assert terminations and terminations[0][0] == 424242


def test_reopen_done_fences_an_invalidated_descendants_old_run(conn):
    task_id = _approved_task(conn)
    qa_id = kb.create_task(conn, title="running QA", assignee="qa", parents=[task_id])
    qa_run = kb.claim_task(conn, qa_id, claimer="qa:1")
    assert qa_run is not None

    assert kbr.reopen_task_for_rework(
        conn, task_id, reason="QA result is stale.", author="triage",
    ) == (True, "builder")
    assert not kb.complete_task(conn, qa_id, expected_run_id=qa_run.current_run_id)
    qa = kb.get_task(conn, qa_id)
    assert qa is not None and qa.status == "todo" and qa.current_run_id is None


def test_reopen_done_rolls_back_target_and_descendants_before_termination(conn, monkeypatch):
    task_id = _approved_task(conn)
    qa_id = kb.create_task(conn, title="QA", assignee="qa", parents=[task_id])
    assert kb.complete_task(conn, qa_id)
    killed: list[tuple] = []

    def explode(*args, **kwargs):
        raise RuntimeError("simulated descendant audit write failure")

    monkeypatch.setattr(kb, "_insert_comment", explode)
    monkeypatch.setattr(kbr, "_terminate_reclaimed_worker", lambda *args: killed.append(args))
    with pytest.raises(RuntimeError, match="simulated descendant audit write failure"):
        kbr.reopen_task_for_rework(conn, task_id, reason="Must be atomic.", author="triage")

    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, qa_id).status == "done"
    assert not [e for e in kb.list_events(conn, task_id) if e.kind == "rework_requested"]
    assert not [e for e in kb.list_events(conn, qa_id) if e.kind == "descendant_invalidated"]
    assert killed == []


def test_cli_reopen_rework_redacts_reason_and_reports_implementer(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "triage")
    with kbc.connect() as conn:
        task_id = _approved_task(conn)
    secret = "ghp_" + "S" * 40

    output = kc.run_slash(f'reopen-rework {task_id} --reason "Fix {secret}"')
    assert "Reopened" in output
    assert "builder" in output
    assert "ready" in output
    assert secret not in output
    with kbc.connect() as conn:
        event = [e for e in kb.list_events(conn, task_id) if e.kind == "rework_requested"][-1]
        assert secret not in str(event.payload)
        assert event.payload["author"] == "triage"
