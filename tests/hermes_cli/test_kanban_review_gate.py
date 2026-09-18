"""Durable same-card review gate regressions for PR-backed Kanban work."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path):
    database = kbc.connect(tmp_path / "kanban.db")
    try:
        yield database
    finally:
        database.close()


def _pr_task(conn, title: str = "PR-backed implementation") -> str:
    return kb.create_task(conn, title=title, assignee="builder", completion_contract="acme/repo")


def _request_review(conn, task_id: str, reviewer: str = "reviewer"):
    implementation = kb.claim_task(conn, task_id, claimer="builder:implementation")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="implementation ready",
        reviewer=reviewer,
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer=f"{reviewer}:review")
    assert review is not None
    return implementation, review


def test_pr_backed_first_review_requires_distinct_explicit_reviewer(conn) -> None:
    task_id = _pr_task(conn)
    implementation = kb.claim_task(conn, task_id, claimer="builder:implementation")
    assert implementation is not None

    ok, reason = kb.request_review(
        conn,
        task_id,
        summary="ready",
        expected_run_id=implementation.current_run_id,
        with_reason=True,
    )
    assert ok is False
    assert reason is not None and "distinct reviewer" in reason
    assert kb.get_task(conn, task_id).status == "running"

    ok, reason = kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="builder",
        expected_run_id=implementation.current_run_id,
        with_reason=True,
    )
    assert ok is False
    assert reason is not None and "distinct reviewer" in reason
    assert kb.get_task(conn, task_id).status == "running"

    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )


def test_changes_requested_pr_task_cannot_complete_without_fresh_review(conn) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    assert kb.request_changes(
        conn,
        task_id,
        reason="add a regression",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    rework = kb.claim_task(conn, task_id, claimer="builder:rework")
    assert rework is not None
    child = kb.create_task(conn, title="QA", assignee="qa", parents=[task_id])
    before = kb.get_task(conn, task_id)
    before_events = len(kb.list_events(conn, task_id))
    before_runs = len(kb.list_runs(conn, task_id))

    ok, reason = kb.complete_task(
        conn,
        task_id,
        summary="bypass attempt",
        expected_run_id=rework.current_run_id,
        with_reason=True,
    )

    assert ok is False
    assert reason is not None and "kanban_request_review" in reason
    after = kb.get_task(conn, task_id)
    assert (after.status, after.current_run_id, after.result, after.completed_at) == (
        before.status,
        before.current_run_id,
        before.result,
        before.completed_at,
    )
    assert len(kb.list_events(conn, task_id)) == before_events
    assert len(kb.list_runs(conn, task_id)) == before_runs
    assert kb.get_task(conn, child).status == "todo"


def test_fresh_distinct_review_can_approve_after_changes(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    assert kb.request_changes(conn, task_id, reason="fix", expected_run_id=review.current_run_id) == (True, "builder")
    rework = kb.claim_task(conn, task_id, claimer="builder:rework")
    assert rework is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="fixed",
        expected_run_id=rework.current_run_id,
    )
    reviewer = kb.claim_review_task(conn, task_id, claimer="reviewer:retry")
    assert reviewer is not None
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)
    child = kb.create_task(conn, title="QA", assignee="qa", parents=[task_id])

    assert kb.complete_task(conn, task_id, expected_run_id=reviewer.current_run_id) is True
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, child).status == "ready"


def test_local_only_review_legacy_behavior_remains_compatible(conn) -> None:
    task_id = kb.create_task(conn, title="Local implementation", assignee="builder", completion_contract="local-only")
    implementation = kb.claim_task(conn, task_id, claimer="builder:implementation")
    assert implementation is not None
    assert kb.request_review(conn, task_id, summary="legacy", expected_run_id=implementation.current_run_id)
    assert kb.complete_task(conn, task_id, summary="manual legacy approval")


def test_guarded_pr_review_cannot_be_manually_completed_without_reviewer_run(conn) -> None:
    task_id = _pr_task(conn)
    implementation = kb.claim_task(conn, task_id, claimer="builder:implementation")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )

    ok, reason = kb.complete_task(conn, task_id, summary="manual approval", with_reason=True)

    assert ok is False
    assert reason is not None and "reviewer run" in reason
    assert kb.get_task(conn, task_id).status == "review"


def test_guarded_pr_reviewer_completion_fails_closed_on_malformed_claim_provenance(conn) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET payload = '[]' WHERE task_id = ? AND kind = 'claimed' AND run_id = ?",
            (task_id, review.current_run_id),
        )

    ok, reason = kb.complete_task(conn, task_id, expected_run_id=review.current_run_id, with_reason=True)

    assert ok is False
    assert reason is not None and "not claimed from review" in reason
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == review.current_run_id


def test_acceptance_race_rechecks_review_gate_before_receipt_or_completion(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    assert kb.request_changes(conn, task_id, reason="fix", expected_run_id=review.current_run_id) == (True, "builder")
    rework = kb.claim_task(conn, task_id, claimer="builder:rework")
    assert rework is not None
    assert kb.request_review(conn, task_id, summary="fixed", expected_run_id=rework.current_run_id)
    reviewer = kb.claim_review_task(conn, task_id, claimer="reviewer:retry")
    assert reviewer is not None

    def invalidate_handoff(_conn, _task_id, _run_id, _metadata):
        assert kb.request_changes(conn, task_id, reason="late change", expected_run_id=reviewer.current_run_id) == (True, "builder")
        return None

    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", invalidate_handoff)
    ok, reason = kb.complete_task(conn, task_id, expected_run_id=reviewer.current_run_id, with_reason=True)

    assert ok is False
    assert reason is not None and "current claimed reviewer run" in reason
    assert kb.get_task(conn, task_id).status == "ready"
    assert not [event for event in kb.list_events(conn, task_id) if event.kind == "pr_acceptance"]


def test_reopen_rework_requires_a_fresh_review_before_releasing_children(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_db_rework as kbr

    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)
    child = kb.create_task(conn, title="QA", assignee="qa", parents=[task_id])
    assert kb.complete_task(conn, task_id, expected_run_id=review.current_run_id)
    assert kb.get_task(conn, child).status == "ready"

    assert kbr.reopen_task_for_rework(
        conn,
        task_id,
        reason="production regression",
        author="operator",
    ) == (True, "builder")
    assert kb.get_task(conn, child).status == "todo"
    repair = kb.claim_task(conn, task_id, claimer="builder:repair")
    assert repair is not None
    ok, reason = kb.complete_task(conn, task_id, expected_run_id=repair.current_run_id, with_reason=True)
    assert ok is False
    assert reason is not None and "kanban_request_review" in reason
    assert kb.get_task(conn, child).status == "todo"

    assert kb.request_review(
        conn,
        task_id,
        summary="repair verified",
        reviewer="reviewer",
        expected_run_id=repair.current_run_id,
    )
    rereview = kb.claim_review_task(conn, task_id, claimer="reviewer:repair")
    assert rereview is not None
    assert kb.complete_task(conn, task_id, expected_run_id=rereview.current_run_id)
    assert kb.get_task(conn, child).status == "ready"


def test_pr_reviewer_block_unblock_retry_keeps_valid_approval_provenance(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    assert kb.block_task(
        conn,
        task_id,
        reason="maintainer decision",
        kind="needs_input",
        expected_run_id=review.current_run_id,
    )
    assert kb.unblock_task(conn, task_id)
    retry = kb.claim_review_task(conn, task_id, claimer="reviewer:retry")
    assert retry is not None
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    assert kb.complete_task(conn, task_id, expected_run_id=retry.current_run_id)


@pytest.mark.parametrize(("storage", "kind"), [
    ("event", "review_requested"),
    ("event", "changes_requested"),
    ("run", "review_requested"),
    ("run", "changes_requested"),
])
def test_orphaned_durable_review_lifecycle_fails_closed(
    conn,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    kind: str,
) -> None:
    """A partial durable review record must never downgrade a PR task to ordinary completion."""
    task_id = _pr_task(conn)
    with kb.write_txn(conn):
        if storage == "event":
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, kind, "{}", 1),
            )
        else:
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, "builder", "done", kind, 1, 1),
            )
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    ok, reason = kb.complete_task(conn, task_id, with_reason=True)

    assert ok is False
    assert reason is not None and "same-card review approval refused" in reason
    assert kb.get_task(conn, task_id).status == "ready"


def test_acceptance_race_does_not_bind_owner_repo_contract_before_final_review_gate(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    child = kb.create_task(conn, title="QA", assignee="qa", parents=[task_id])
    before_contract = kb.get_task(conn, task_id).completion_contract
    after_invalidation: dict[str, object] = {}

    def invalidate_during_collection(_contract, _published_pr, *, policy):
        assert kb.request_changes(
            conn, task_id, reason="late review finding", expected_run_id=review.current_run_id,
        ) == (True, "builder")
        task = kb.get_task(conn, task_id)
        after_invalidation.update({
            "status": task.status,
            "run": task.current_run_id,
            "result": task.result,
            "contract": task.completion_contract,
            "events": len(kb.list_events(conn, task_id)),
            "runs": len(kb.list_runs(conn, task_id)),
            "child": kb.get_task(conn, child).status,
        })
        return {"ok": True, "classification": "success", "recovery": "retry"}

    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.collect_acceptance", invalidate_during_collection)
    ok, reason = kb.complete_task(
        conn,
        task_id,
        expected_run_id=review.current_run_id,
        metadata={"published_pr": "https://github.com/acme/repo/pull/7"},
        with_reason=True,
    )

    assert ok is False
    assert reason is not None and "current claimed reviewer run" in reason
    task = kb.get_task(conn, task_id)
    assert task.completion_contract == before_contract == "acme/repo"
    assert (task.status, task.current_run_id, task.result, len(kb.list_events(conn, task_id)),
            len(kb.list_runs(conn, task_id)), kb.get_task(conn, child).status) == (
        after_invalidation["status"], after_invalidation["run"], after_invalidation["result"],
        after_invalidation["events"], after_invalidation["runs"], after_invalidation["child"],
    )


def test_stale_reviewer_run_cannot_approve_a_reclaimed_review(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = _pr_task(conn)
    _implementation, first_review = _request_review(conn, task_id)
    assert kb.block_task(conn, task_id, reason="review interrupted", expected_run_id=first_review.current_run_id)
    assert kb.unblock_task(conn, task_id)
    retry = kb.claim_review_task(conn, task_id, claimer="reviewer:retry")
    assert retry is not None
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    ok, reason = kb.complete_task(conn, task_id, expected_run_id=first_review.current_run_id, with_reason=True)

    assert ok is False
    assert reason is not None and "current claimed reviewer run" in reason
    assert kb.get_task(conn, task_id).current_run_id == retry.current_run_id


def test_spoofed_assignee_cannot_turn_rework_run_into_reviewer_approval(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    assert kb.request_changes(conn, task_id, reason="fix", expected_run_id=review.current_run_id) == (True, "builder")
    rework = kb.claim_task(conn, task_id, claimer="builder:rework")
    assert rework is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", ("reviewer", task_id))
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    ok, reason = kb.complete_task(conn, task_id, expected_run_id=rework.current_run_id, with_reason=True)

    assert ok is False
    assert reason is not None and "not claimed from review" in reason
    assert kb.get_task(conn, task_id).status == "running"


def test_event_ids_order_fresh_same_second_rereview_after_changes(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    assert kb.request_changes(conn, task_id, reason="fix", expected_run_id=review.current_run_id) == (True, "builder")
    rework = kb.claim_task(conn, task_id, claimer="builder:rework")
    assert rework is not None
    assert kb.request_review(conn, task_id, summary="fixed", expected_run_id=rework.current_run_id)
    rereview = kb.claim_review_task(conn, task_id, claimer="reviewer:rereview")
    assert rereview is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at = 1 WHERE task_id = ?", (task_id,))
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    assert kb.complete_task(conn, task_id, expected_run_id=rereview.current_run_id)


def test_repeated_changes_and_rereview_cycles_preserve_fresh_approval(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = _pr_task(conn)
    _implementation, review = _request_review(conn, task_id)
    for cycle in range(2):
        assert kb.request_changes(conn, task_id, reason=f"fix {cycle}", expected_run_id=review.current_run_id) == (True, "builder")
        rework = kb.claim_task(conn, task_id, claimer=f"builder:rework-{cycle}")
        assert rework is not None
        assert kb.request_review(conn, task_id, summary=f"fixed {cycle}", expected_run_id=rework.current_run_id)
        review = kb.claim_review_task(conn, task_id, claimer=f"reviewer:retry-{cycle}")
        assert review is not None
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    assert kb.complete_task(conn, task_id, expected_run_id=review.current_run_id)


def test_plain_pr_task_never_entered_review_keeps_existing_completion_behavior(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _pr_task(conn)
    monkeypatch.setattr("hermes_cli.kanban_pr_acceptance_store.prepare_acceptance", lambda *_args: None)

    assert kb.complete_task(conn, task_id)
