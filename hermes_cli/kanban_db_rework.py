"""Audited reopening of completed implementation cards for same-card rework."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

from hermes_cli.kanban_db import (
    _append_event,
    _insert_comment,
    _json_dict,
    _latest_event,
    _landing_status_after_parents,
    _nonblank_str,
    _row_get,
    _terminate_reclaimed_worker,
    invalidate_descendants_for_parent_reopen,
    redact_review_value,
)
from hermes_cli.kanban_db_connect import write_txn


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
    terminations: list[tuple[Optional[int], Optional[str]]] = []

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

        review_event = _latest_event(conn, task_id, "review_requested")
        implementer = _nonblank_str(
            _json_dict(_row_get(review_event, "payload")).get("implementer"),
        )
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
                   current_run_id = NULL,
                   consecutive_failures = 0,
                   last_failure_error = NULL,
                   block_kind = NULL,
                   block_recurrences = 0
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

    for worker_pid, claim_lock in terminations:
        _terminate_reclaimed_worker(worker_pid, claim_lock)
    return True, implementer
