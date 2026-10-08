"""Behavior contracts for typed upstream-rework recovery and block identity."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_rework as kbr


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _approved_pr(conn, monkeypatch, *, assignee: str, parents=()) -> str:
    task_id = kb.create_task(
        conn, title="implementation", assignee=assignee, parents=parents,
        completion_contract="acme/repo",
    )
    implementation = kb.claim_task(conn, task_id, claimer=f"{assignee}:implementation")
    assert implementation is not None
    assert kb.request_review(
        conn, task_id, summary="implemented", reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:approval")
    assert review is not None
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)
    assert kb.complete_task(conn, task_id, expected_run_id=review.current_run_id, result="approved")
    return task_id


def _running(conn, task_id: str):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id, claimer="worker")
    assert claimed is not None
    return claimed


def test_parent_rework_restores_original_pr_implementer_and_requires_fresh_review(conn, monkeypatch):
    ancestor = _approved_pr(conn, monkeypatch, assignee="coder-a")
    descendant = _approved_pr(conn, monkeypatch, assignee="coder-a", parents=[ancestor])

    assert kbr.reopen_task_for_rework(
        conn, ancestor, reason="upstream contract changed", author="operator",
    ) == (True, "coder-a")
    reopened = kb.get_task(conn, descendant)
    assert reopened is not None
    assert (reopened.status, reopened.assignee) == ("todo", "coder-a")
    invalidation = [e for e in kb.list_events(conn, descendant) if e.kind == "descendant_invalidated"][-1]
    assert invalidation.payload["restored_assignee"] == "coder-a"
    assert invalidation.payload["previous_assignee"] == "reviewer"

    # Once the reworked ancestor is approved again, the descendant is claimed by
    # its original implementer, not by the reviewer who last owned the card.
    repair = kb.claim_task(conn, ancestor, claimer="coder-a:repair")
    assert repair is not None
    assert kb.request_review(conn, ancestor, summary="repair", reviewer="reviewer", expected_run_id=repair.current_run_id)
    rereview = kb.claim_review_task(conn, ancestor, claimer="reviewer:repair")
    assert rereview is not None
    assert kb.complete_task(conn, ancestor, expected_run_id=rereview.current_run_id, result="approved repair")
    assert kb.get_task(conn, descendant).status == "ready"
    repair_b = kb.claim_task(conn, descendant, claimer="coder-a:descendant-repair")
    assert repair_b is not None
    ok, reason = kb.complete_task(conn, descendant, expected_run_id=repair_b.current_run_id, with_reason=True)
    assert ok is False
    assert reason is not None and "kanban_request_review" in reason


def test_typed_upstream_marker_recovers_only_matching_waits_and_preserves_stage_owner(conn):
    ancestor = kb.create_task(conn, title="ancestor", assignee="coder")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (ancestor,))
    qa = kb.create_task(conn, title="QA", assignee="qa", parents=[ancestor])
    release = kb.create_task(conn, title="release", assignee="release-manager", parents=[qa])
    human_triage = kb.create_task(conn, title="human decision", assignee="triage", parents=[qa])

    for task_id in (qa, release):
        # The release's direct QA parent is deliberately still open.  This
        # models an operator-recorded upstream wait without pretending the
        # dispatcher could normally claim it before QA; block_task owns the
        # state transition under test and does not require a synthetic run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        assert kb.block_task(
            conn, task_id, kind="needs_input", reason="upstream defect",
            cause_key="upstream.contract", upstream_task_id=ancestor,
        )
        assert kb.unblock_task(conn, task_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        assert kb.block_task(
            conn, task_id, kind="needs_input", reason="still upstream",
            cause_key="upstream.contract", upstream_task_id=ancestor,
        )
        assert kb.get_task(conn, task_id).status == "triage"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (human_triage,))
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?", (ancestor,))

    result = kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="operator")
    recovered = {entry["id"] for entry in result["invalidated"] if entry.get("typed_upstream_recovery")}
    assert recovered == {qa, release}
    assert (kb.get_task(conn, qa).status, kb.get_task(conn, qa).assignee) == ("todo", "qa")
    assert (kb.get_task(conn, release).status, kb.get_task(conn, release).assignee) == ("todo", "release-manager")
    assert kb.get_task(conn, human_triage).status == "triage"
    kb.recompute_ready(conn)
    assert kb.get_task(conn, qa).status == "todo"
    assert kb.get_task(conn, release).status == "todo"


def test_cause_identity_changes_reset_recurrence_and_legacy_omission_is_kind_only(conn):
    task_id = kb.create_task(conn, title="input", assignee="worker")
    first = _running(conn, task_id)
    assert kb.block_task(conn, task_id, kind="needs_input", reason="one", cause_key="input.one", expected_run_id=first.current_run_id)
    assert kb.unblock_task(conn, task_id)
    second = _running(conn, task_id)
    assert kb.block_task(conn, task_id, kind="needs_input", reason="two", cause_key="input.two", expected_run_id=second.current_run_id)
    task = kb.get_task(conn, task_id)
    assert (task.status, task.block_recurrences, task.block_cause_key) == ("blocked", 1, "input.two")
    assert kb.unblock_task(conn, task_id)
    third = _running(conn, task_id)
    assert kb.block_task(conn, task_id, kind="needs_input", reason="two again", cause_key="input.two", expected_run_id=third.current_run_id)
    assert kb.get_task(conn, task_id).status == "triage"

    legacy = kb.create_task(conn, title="legacy", assignee="worker")
    assert kb.block_task(conn, legacy, kind="capability", reason="first")
    assert kb.unblock_task(conn, legacy)
    assert kb.block_task(conn, legacy, kind="capability", reason="again")
    assert kb.get_task(conn, legacy).status == "triage"


def test_operator_recovery_requires_incomplete_real_ancestor_and_is_idempotent(conn):
    ancestor = kb.create_task(conn, title="ancestor", assignee="coder")
    child = kb.create_task(conn, title="legacy child", assignee="qa", parents=[ancestor])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage', consecutive_failures = 3 WHERE id = ?", (child,))
    before = kb.get_task(conn, child)
    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id="t_missing", reason="repair", author="operator",
    ) == (False, "upstream task not found")
    assert kb.get_task(conn, child) == before

    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id=ancestor, reason="legacy provenance", author="operator",
    ) == (True, "todo")
    recovered = kb.get_task(conn, child)
    assert (recovered.status, recovered.assignee, recovered.consecutive_failures) == ("todo", "qa", 0)
    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id=ancestor, reason="repeat", author="operator",
    ) == (True, "already recovered")
    events = [e for e in kb.list_events(conn, child) if e.kind == "upstream_rework_recovered"]
    assert len(events) == 1
