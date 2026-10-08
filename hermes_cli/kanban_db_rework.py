"""Audited reopening of completed implementation cards for same-card rework.

A thin, audited wrapper over the upstream done-reopen primitive
(:func:`hermes_cli.kanban_db.invalidate_descendants_for_parent_reopen`), which
owns descendant demotion, run reclaim and worker termination bookkeeping. What
this module adds on top is the part upstream has no equivalent for: a MANDATORY
operator reason, restoration to the card's original durable implementer (not
whoever happens to be assigned), and a ``rework_requested`` audit event the
review gate treats as a rework boundary (see
:func:`hermes_cli.kanban_db_review_gate.completion_gate_reason`).

Distinct from upstream's :func:`hermes_cli.kanban_db.reopen_review_task`, which
moves ``review -> ready`` with no reason and no descendant invalidation; this
moves ``done -> ready/todo`` and re-gates the whole subtree.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from hermes_cli.kanban_db import (
    _append_event,
    _insert_comment,
    _landing_status_after_parents,
    _terminate_reclaimed_worker,
    invalidate_descendants_for_parent_reopen,
    redact_review_value,
)
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_db_review_gate import durable_implementer


def reopen_task_for_rework(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    author: str,
) -> tuple[bool, Optional[str]]:
    """Restore an approved card to its original implementer for a fresh review.

    Only a completed task with valid durable ``review_requested`` provenance may
    be reopened. The parent transition, descendant invalidation, and audit trail
    commit together; reclaimed descendant workers are terminated afterwards.
    """
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"
    author = str(author or "").strip() or "operator"
    # Upstream's terminations rows are (worker_pid, claim_lock, worker_started_at):
    # the third element is the spawn-time fingerprint _terminate_reclaimed_worker
    # needs to avoid signalling a RECYCLED pid. Never drop it.
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []

    with write_txn(conn):
        task = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            return False, "task not found"
        if task["status"] != "done":
            return False, "task is not done"
        open_run = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL LIMIT 1",
            (task_id,),
        ).fetchone()
        if (
            task["claim_lock"] is not None
            or task["claim_expires"] is not None
            or task["worker_pid"] is not None
            or task["current_run_id"] is not None
            or open_run is not None
        ):
            return False, "task has a conflicting live run or claim"

        implementer, _provenance_error = durable_implementer(conn, task_id)
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"

        landing_status = _landing_status_after_parents(conn, task_id)
        updated = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = ?,
                   result = NULL,
                   completed_at = NULL,
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   worker_started_at = NULL,
                   current_run_id = NULL,
                   consecutive_failures = 0,
                   last_failure_error = NULL,
                   block_kind = NULL,
                   block_recurrences = 0,
                   block_cause_key = NULL,
                   block_upstream_task_id = NULL
             WHERE id = ? AND status = 'done'
            """,
            (landing_status, implementer, task_id),
        )
        if updated.rowcount != 1:
            return False, "task changed during rework reopen"

        payload = {
            "reason": reason,
            "author": author,
            "implementer": implementer,
            "prior_status": "done",
            "status": landing_status,
        }
        _append_event(conn, task_id, "rework_requested", payload)
        _insert_comment(
            conn,
            task_id,
            author,
            f"REWORK REQUESTED: {reason} (restored {implementer}; done -> {landing_status}).",
            int(time.time()),
        )
        invalidation = invalidate_descendants_for_parent_reopen(conn, task_id, author=author)
        terminations.extend(invalidation["terminations"])

    # Post-commit: audit trail is durable before any worker dies (upstream's
    # contract for a COMPOSED call — it leaves the draining to us).
    for worker_pid, claim_lock, started_at in terminations:
        _terminate_reclaimed_worker(worker_pid, claim_lock, started_at=started_at)
    return True, implementer


def recover_upstream_rework(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    upstream_task_id: str,
    reason: str,
    author: str,
) -> tuple[bool, str]:
    """Explicitly redrive one legacy upstream-rework wait into ``todo``.

    This deliberately does not mine old prose for a likely ancestor.  The
    operator names a real *incomplete* transitive parent and the state/audit
    transition is one transaction, preserving the task's stage owner.
    """
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"
    upstream_task_id = str(upstream_task_id or "").strip()
    if not upstream_task_id:
        return False, "upstream_task_id is required"
    author = str(author or "").strip() or "operator"
    with write_txn(conn):
        task = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            return False, "task not found"
        if task["status"] == "todo":
            prior = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'upstream_rework_recovered' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if prior is not None:
                try:
                    if json.loads(prior["payload"] or "{}").get("ancestor") == upstream_task_id:
                        return True, "already recovered"
                except (TypeError, ValueError):
                    pass
        if task["status"] not in {"blocked", "triage"}:
            return False, "task must be blocked or triage"
        if any(task[key] is not None for key in ("claim_lock", "claim_expires", "worker_pid", "current_run_id")):
            return False, "task has a conflicting live run or claim"
        upstream = conn.execute("SELECT status FROM tasks WHERE id = ?", (upstream_task_id,)).fetchone()
        if upstream is None:
            return False, "upstream task not found"
        if upstream_task_id == task_id:
            return False, "upstream_task_id must not be the task itself"
        ancestor = conn.execute(
            """
            WITH RECURSIVE ancestors(id) AS (
                SELECT parent_id FROM task_links WHERE child_id = ?
                UNION
                SELECT l.parent_id FROM task_links l JOIN ancestors a ON a.id = l.child_id
            )
            SELECT 1 FROM ancestors WHERE id = ? LIMIT 1
            """,
            (task_id, upstream_task_id),
        ).fetchone()
        if ancestor is None:
            return False, "upstream_task_id must name a real transitive ancestor of this task"
        if upstream["status"] in {"done", "archived"}:
            return False, "upstream task is already satisfied"
        updated = conn.execute(
            """
            UPDATE tasks
               SET status = 'todo', claim_lock = NULL, claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL, current_run_id = NULL,
                   consecutive_failures = 0, last_failure_error = NULL,
                   block_kind = NULL, block_recurrences = 0,
                   block_cause_key = NULL, block_upstream_task_id = NULL
             WHERE id = ? AND status IN ('blocked', 'triage')
            """,
            (task_id,),
        )
        if updated.rowcount != 1:
            return False, "task changed during recovery"
        payload = {
            "ancestor": upstream_task_id, "reason": reason, "author": author,
            "prior_status": task["status"], "status": "todo", "operator_recovery": True,
        }
        _append_event(conn, task_id, "upstream_rework_recovered", payload)
        _insert_comment(
            conn, task_id, author,
            f"Recovered legacy upstream rework wait on {upstream_task_id}: {reason}", int(time.time()),
        )
    return True, "todo"
