"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_pr_acceptance import collect_acceptance


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            status = 200
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": ([] if state.get("no_required_checks") else [
                        {"context": "required", "app": {"databaseId": 1}}])}}}}}}
            elif "/rules/branches/" in self.path:
                if state.get("rules_error"):
                    status, message = state["rules_error"]
                    value = {"message": message}
                else:
                    value = state.get("rules", [[]])
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request,urllib.error\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "try:\n r=urllib.request.urlopen(u); status=r.status; body=r.read().decode()\n"
                  "except urllib.error.HTTPError as e:\n status=e.code; body=e.read().decode()\n"
                  "if '--include' in sys.argv: print(f'HTTP/1.1 {status} fixture\\n\\n'+body)\n"
                  "elif status < 400: print(body)\n"
                  "else: sys.stderr.write(body); sys.exit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


def test_no_required_checks_needs_explicit_audited_local_policy(github):
    """No configured CI never silently turns a PR task into local-only work."""
    github["no_required_checks"] = True

    strict = collect_acceptance(
        "https://github.com/acme/repo/pull/7",
        "https://github.com/acme/repo/pull/7",
        policy="required-checks",
    )
    local = collect_acceptance(
        "https://github.com/acme/repo/pull/7",
        "https://github.com/acme/repo/pull/7",
        policy="local-if-no-required-checks",
    )

    assert not strict["ok"]
    assert strict["classification"] == "no-required-checks"
    assert "set-pr-policy" in strict["recovery"]
    assert local["ok"]
    assert local["verification_source"] == "declared-local"


@pytest.mark.parametrize("status,message,recognized", [
    (403, "Upgrade to GitHub Pro or make this repository public to enable this feature.", True),
    (403, "Resource not accessible by integration", False),
    (401, "Upgrade to GitHub Pro or make this repository public to enable this feature.", False),
])
def test_only_exact_rules_plan_response_is_sanitized_as_unavailable(github, status, message, recognized):
    github.update(no_required_checks=True, rules_error=(status, message))
    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7",
                                 policy="required-checks")
    if recognized:
        assert receipt["classification"] == "no-required-checks"
        assert receipt["ruleset_unavailable"] is True
    else:
        assert receipt["classification"] == "infra"


def test_cli_policy_round_trip_keeps_blocked_exact_pr_identity(github):
    """Operators can audit/revert policy without repurposing completion-contract editing."""
    contract = "https://github.com/acme/repo/pull/7"
    with connect() as conn:
        tid = kb.create_task(conn, title="blocked publish", completion_contract=contract,
                             initial_status="blocked")

    assert "Set PR acceptance policy" in kc.run_slash(
        f'set-pr-policy {tid} local-if-no-required-checks --reason "local test log is authoritative"')
    shown = json.loads(kc.run_slash(f"show {tid} --json"))
    assert shown["task"]["pr_acceptance_policy"] == "local-if-no-required-checks"
    assert shown["task"]["completion_contract"] == contract
    assert "Set PR acceptance policy" in kc.run_slash(
        f'set-pr-policy {tid} required-checks --reason "restore CI requirement"')
    with connect() as conn:
        events = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance_policy_changed'", (tid,))]
        task = kb.get_task(conn, tid)
    assert [event["new_policy"] for event in events] == ["local-if-no-required-checks", "required-checks"]
    assert all(event["author"] for event in events)
    assert task is not None
    assert task.status == "blocked"
    assert task.completion_contract == contract
