"""Durable provenance checks for PR-backed same-card Kanban review."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional


_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/\d+")

REVIEW_GATE_RECOVERY = (
    'request same-card kanban_request_review(reviewer="reviewer") and wait '
    "for a distinct reviewer to claim and approve it"
)


def _profile(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(value)


def _payload(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None or not row["payload"]:
        return None
    try:
        decoded = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def is_pr_backed_contract(contract: Any) -> bool:
    return isinstance(contract, str) and bool(_REPO.fullmatch(contract) or _PR.fullmatch(contract))


def _review_events(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, run_id, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested' ORDER BY id",
        (task_id,),
    ).fetchall()


def _durable_implementer(conn: sqlite3.Connection, task_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return the immutable original implementer, or a fail-closed reason."""
    events = _review_events(conn, task_id)
    if not events:
        return None, None
    first = _payload(events[0])
    implementer = _profile(first.get("implementer")) if first is not None else None
    if implementer is None:
        return None, "recorded review lifecycle has no valid durable implementer provenance"
    return implementer, None


def _active_run_profile(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> Optional[str]:
    if run_id is None:
        return None
    row = conn.execute(
        "SELECT profile FROM task_runs WHERE id = ? AND task_id = ?",
        (int(run_id), task_id),
    ).fetchone()
    return _profile(row["profile"]) if row else None


def review_request_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    contract: Any,
    current_run_id: Optional[int],
    reviewer: Any,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Validate and normalize a PR review handoff before it is persisted.

    Non-PR tasks retain legacy optional-reviewer behavior. PR-backed tasks
    acquire immutable implementer provenance from the first implementation run;
    every handoff needs a distinct reviewer.
    """
    if not is_pr_backed_contract(contract):
        return None, _profile(reviewer), None

    original, malformed_reason = _durable_implementer(conn, task_id)
    actor = _active_run_profile(conn, task_id, current_run_id)
    if malformed_reason is not None:
        return None, None, malformed_reason
    if original is None:
        if actor is None:
            return None, None, "PR-backed review requires a claimed implementer run with durable profile provenance"
        original = actor
    elif actor is None:
        return None, None, "PR-backed re-review requires a claimed implementation run with durable profile provenance"

    normalized_reviewer = _profile(reviewer)
    if normalized_reviewer is None or normalized_reviewer in {original, actor}:
        return None, None, "PR-backed review requires a nonblank distinct reviewer profile"
    return original, normalized_reviewer, None


def _review_required(conn: sqlite3.Connection, task_id: str, contract: Any) -> bool:
    if not is_pr_backed_contract(contract):
        return False
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind IN ('review_requested', 'changes_requested') "
        "UNION ALL "
        "SELECT 1 FROM task_runs WHERE task_id = ? "
        "AND outcome IN ('review_requested', 'changes_requested') "
        "LIMIT 1",
        (task_id, task_id),
    ).fetchone() is not None


def completion_gate_reason(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_run_id: Optional[int],
) -> Optional[str]:
    """Return the actionable rejection reason, or ``None`` when completion is valid.

    The caller invokes this once before external acceptance collection and again
    while holding the final completion transaction.
    """
    task = conn.execute(
        "SELECT status, current_run_id, completion_contract FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None or not _review_required(conn, task_id, task["completion_contract"]):
        return None

    original, malformed_reason = _durable_implementer(conn, task_id)
    if malformed_reason is not None or original is None:
        return f"same-card review approval refused: {malformed_reason or 'missing durable implementer provenance'}; {REVIEW_GATE_RECOVERY}"
    if expected_run_id is None:
        return f"same-card review approval refused: a guarded PR task cannot be manually completed without its reviewer run; {REVIEW_GATE_RECOVERY}"
    if task["status"] != "running" or task["current_run_id"] != int(expected_run_id):
        return f"same-card review approval refused: completion must come from the current claimed reviewer run; {REVIEW_GATE_RECOVERY}"

    run_id = int(expected_run_id)
    claimed = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'claimed' "
        "AND run_id = ? ORDER BY id DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    claimed_payload = _payload(claimed)
    if claimed is None or claimed_payload is None or claimed_payload.get("source_status") != "review":
        return f"same-card review approval refused: current run was not claimed from review; {REVIEW_GATE_RECOVERY}"

    boundaries = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS id FROM task_events WHERE task_id = ? "
        "AND kind IN ('changes_requested', 'rework_requested')",
        (task_id,),
    ).fetchone()
    boundary_id = int(boundaries["id"] or 0)
    handoff = conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    handoff_payload = _payload(handoff)
    reviewer = _profile(handoff_payload.get("reviewer")) if handoff_payload is not None else None
    handoff_implementer = _profile(handoff_payload.get("implementer")) if handoff_payload is not None else None
    if (
        handoff is None
        or int(handoff["id"]) <= boundary_id
        or int(claimed["id"]) <= int(handoff["id"])
        or handoff["run_id"] is None
        or reviewer is None
        or handoff_implementer != original
        or reviewer == original
    ):
        return f"same-card review approval refused: no fresh valid reviewer handoff follows the latest rework boundary; {REVIEW_GATE_RECOVERY}"

    reviewer_run = _active_run_profile(conn, task_id, run_id)
    if reviewer_run is None or reviewer_run != reviewer or reviewer_run == original:
        return f"same-card review approval refused: current reviewer provenance is missing, stale, or self-reviewing; {REVIEW_GATE_RECOVERY}"
    return None
