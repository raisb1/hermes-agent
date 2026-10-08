"""Behavior contracts for typed upstream-rework recovery and block identity."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
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


def _run_cli(*argv: str) -> int:
    """Invoke the registered ``hermes kanban`` argparse command."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kc.build_parser(subparsers)
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


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


def test_typed_recovery_releases_qa_then_preserves_release_owner_for_claim(conn):
    ancestor = kb.create_task(conn, title="implementation", assignee="coder")
    assert kb.complete_task(conn, ancestor, result="approved")
    qa = kb.create_task(conn, title="QA", assignee="qa", parents=[ancestor])
    release = kb.create_task(conn, title="release", assignee="release-manager", parents=[qa])

    for task_id in (qa, release):
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

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?", (ancestor,))
    kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="operator")
    assert (kb.get_task(conn, qa).status, kb.get_task(conn, release).status) == ("todo", "todo")

    kb.recompute_ready(conn)
    repaired_ancestor = kb.claim_task(conn, ancestor, claimer="coder:repair")
    assert repaired_ancestor is not None
    assert kb.complete_task(conn, ancestor, expected_run_id=repaired_ancestor.current_run_id, result="repaired")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, qa).status == "ready"
    assert kb.get_task(conn, release).status == "todo"

    qa_run = kb.claim_task(conn, qa, claimer="qa:verification")
    assert qa_run is not None
    assert kb.complete_task(conn, qa, expected_run_id=qa_run.current_run_id, result="fresh QA passed")
    kb.recompute_ready(conn)
    release_run = kb.claim_task(conn, release, claimer="release-manager:ship")
    assert release_run is not None
    assert release_run.assignee == "release-manager"


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


def test_operator_recovery_revalidates_current_graph_ancestor_and_claim_before_idempotency(conn):
    ancestor = kb.create_task(conn, title="ancestor", assignee="coder")
    child = kb.create_task(conn, title="legacy child", assignee="qa", parents=[ancestor])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (child,))
    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id=ancestor, reason="legacy provenance", author="operator",
    ) == (True, "todo")
    before = list(kb.list_events(conn, child))

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (ancestor,))
    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id=ancestor, reason="stale retry", author="operator",
    ) == (False, "upstream task is already satisfied")
    assert kb.list_events(conn, child) == before

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (ancestor,))
        conn.execute("DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (ancestor, child))
    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id=ancestor, reason="unlinked retry", author="operator",
    ) == (False, "upstream_task_id must name a real transitive ancestor of this task")
    assert kb.list_events(conn, child) == before

    kb.link_tasks(conn, parent_id=ancestor, child_id=child)
    before_claim_retry = list(kb.list_events(conn, child))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock = 'live:claim' WHERE id = ?", (child,))
    assert kbr.recover_upstream_rework(
        conn, child, upstream_task_id=ancestor, reason="claimed retry", author="operator",
    ) == (False, "task has a conflicting live run or claim")
    assert kb.list_events(conn, child) == before_claim_retry


@pytest.mark.parametrize("provenance", [None, "malformed"])
def test_completed_pr_descendant_without_durable_implementer_stays_triaged(conn, provenance):
    ancestor = kb.create_task(conn, title="ancestor", assignee="coder")
    assert kb.complete_task(conn, ancestor, result="done")
    descendant = kb.create_task(
        conn, title="PR implementation", assignee="reviewer", parents=[ancestor],
        completion_contract="acme/repo",
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (descendant,))
        if provenance == "malformed":
            kb._append_event(conn, descendant, "review_requested", {"implementer": ""})
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?", (ancestor,))

    kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="operator")
    task = kb.get_task(conn, descendant)
    assert task is not None and (task.status, task.assignee) == ("triage", "reviewer")
    event = [e for e in kb.list_events(conn, descendant) if e.kind == "descendant_invalidated"][-1]
    assert "durable implementer provenance" in event.payload["recovery_error"]
    assert not [e for e in kb.list_events(conn, descendant) if e.kind == "rework_requested"]
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, descendant, claimer="reviewer:wrong-role") is None


def test_typed_recovery_does_not_clear_a_blocked_descendants_live_run(conn):
    ancestor = kb.create_task(conn, title="ancestor", assignee="coder")
    assert kb.complete_task(conn, ancestor, result="done")
    child = kb.create_task(conn, title="claimed wait", assignee="qa", parents=[ancestor])
    claim = kb.claim_task(conn, child, claimer="qa:worker")
    assert claim is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', "
            "block_cause_key = 'upstream.contract', block_upstream_task_id = ? WHERE id = ?",
            (ancestor, child),
        )
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?", (ancestor,))

    result = kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="operator")
    current = kb.get_task(conn, child)
    assert current is not None
    assert (current.status, current.current_run_id, current.claim_lock) == (
        "blocked", claim.current_run_id, claim.claim_lock,
    )
    assert child not in {entry["id"] for entry in result["invalidated"]}
    assert kb.latest_run(conn, child).ended_at is None
    assert not [e for e in kb.list_events(conn, child) if e.kind == "upstream_rework_recovered"]


def test_cli_wires_typed_block_and_recovery_without_cross_board_leakage(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("alpha")
    kb.create_board("beta")
    with kbc.connect_closing(board="alpha") as alpha:
        parent = kb.create_task(alpha, title="ancestor", assignee="coder")
        child = kb.create_task(alpha, title="waiter", assignee="qa", parents=[parent])
        invalid = kb.create_task(alpha, title="invalid identity", assignee="qa")
        with kb.write_txn(alpha):
            alpha.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        invalid_events = list(kb.list_events(alpha, invalid))
    with kbc.connect_closing(board="beta") as beta:
        same_id = kb.create_task(beta, title="isolated", assignee="qa")
        assert same_id != child

    assert _run_cli(
        "--board", "alpha", "block", invalid, "bad identity", "--kind", "needs_input",
        "--cause-key", "contains a space",
    ) == 1
    assert "cause_key" in capsys.readouterr().err
    with kbc.connect_closing(board="alpha") as alpha:
        invalid_task = kb.get_task(alpha, invalid)
        assert invalid_task is not None and invalid_task.status == "ready"
        assert kb.list_events(alpha, invalid) == invalid_events

    assert _run_cli(
        "--board", "alpha", "block", child, "upstream defect", "--kind", "needs_input",
        "--cause-key", "upstream.contract", "--upstream-task-id", parent,
    ) == 0
    capsys.readouterr()
    with kbc.connect_closing(board="alpha") as alpha:
        typed = kb.get_task(alpha, child)
        assert (typed.block_cause_key, typed.block_upstream_task_id) == ("upstream.contract", parent)
        before_events = list(kb.list_events(alpha, child))
    assert _run_cli(
        "--board", "alpha", "recover-upstream-rework", child,
        "--upstream-task-id", parent, "--reason", "legacy redrive",
    ) == 0
    assert "Recovered" in capsys.readouterr().out
    with kbc.connect_closing(board="alpha") as alpha:
        assert kb.get_task(alpha, child).status == "todo"
        assert len(kb.list_events(alpha, child)) == len(before_events) + 1
    with kbc.connect_closing(board="beta") as beta:
        assert kb.get_task(beta, same_id).status == "ready"


def test_old_schema_migration_adds_nullable_typed_block_identity_columns(tmp_path):
    db_path = tmp_path / "old-kanban.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    legacy.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    legacy.execute("INSERT INTO tasks VALUES ('legacy', 'old task', 'ready', 1)")
    legacy.commit()
    legacy.close()

    with kbc.connect(db_path) as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")}
        row = migrated.execute(
            "SELECT block_cause_key, block_upstream_task_id FROM tasks WHERE id = 'legacy'"
        ).fetchone()
    assert {"block_cause_key", "block_upstream_task_id"} <= columns
    assert tuple(row) == (None, None)
