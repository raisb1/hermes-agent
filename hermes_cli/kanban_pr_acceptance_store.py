"""Persist acceptance with the same ownership snapshot as the terminal write."""
from __future__ import annotations

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pr_acceptance import (
    VALID_PR_ACCEPTANCE_POLICIES,
    _PR,
    _REPO,
    collect_acceptance,
    effective_policy,
)


def _snapshot(conn, task_id):
    row = conn.execute(
        "SELECT current_run_id, status, completion_contract, pr_acceptance_policy FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    return tuple(row) if row else None


def set_pr_acceptance_policy(conn, task_id: str, policy: str, *, reason: str, author: str) -> bool:
    """Set the audited per-task policy without changing contract or lifecycle ownership."""
    from hermes_cli.kanban_db import _append_event

    if policy not in VALID_PR_ACCEPTANCE_POLICIES:
        raise ValueError(f"pr acceptance policy must be one of {sorted(VALID_PR_ACCEPTANCE_POLICIES)}")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("--reason must be nonblank")
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, completion_contract, pr_acceptance_policy FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        if row["status"] in {"done", "archived"}:
            raise ValueError("cannot set a PR acceptance policy on a terminal task")
        contract = row["completion_contract"]
        if not isinstance(contract, str) or contract == "local-only" or not (_REPO.fullmatch(contract) or _PR.fullmatch(contract)):
            raise ValueError("PR acceptance policy is only valid for a PR-backed task")
        old = effective_policy(row["pr_acceptance_policy"])
        conn.execute("UPDATE tasks SET pr_acceptance_policy=? WHERE id=?", (policy, task_id))
        _append_event(conn, task_id, "pr_acceptance_policy_changed", {
            "old_policy": old, "new_policy": policy, "author": author, "reason": reason,
        })
    return True


def prepare_acceptance(conn, task_id, expected_run_id, metadata):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    run_id, status, contract, policy = snapshot
    if not contract or contract == "local-only":
        return None
    if status not in {"running", "ready", "blocked", "review"} or (expected_run_id is not None and run_id != expected_run_id):
        return False
    published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
    match = _PR.fullmatch(published_pr) if isinstance(published_pr, str) else None
    # Publication binds once. Retrying cannot replace the task's PR with a green sibling.
    if match and contract == match[1]:
        with write_txn(conn):
            if _snapshot(conn, task_id) != snapshot:
                return False
            conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (published_pr, task_id))
        snapshot = (run_id, status, published_pr, policy)
        contract = published_pr
    return snapshot, collect_acceptance(str(contract), published_pr, policy=policy)


def record_acceptance(conn, task_id, acceptance):
    """Called under complete_task's write_txn, before its terminal UPDATE."""
    from hermes_cli.kanban_db import _append_event
    snapshot, receipt = acceptance
    if _snapshot(conn, task_id) != snapshot:
        return False
    _append_event(conn, task_id, "pr_acceptance", receipt, run_id=snapshot[0])
    if not receipt["ok"]:
        detail = f"PR acceptance {receipt['classification']}: {receipt.get('detail', '')} {receipt['recovery']}"
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (detail, task_id))
    return receipt["ok"]
