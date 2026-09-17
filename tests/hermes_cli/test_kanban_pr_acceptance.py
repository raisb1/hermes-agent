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
from hermes_cli.kanban_pr_acceptance_store import set_pr_acceptance_policy


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            status = 200
            headers = []
            if self.path == "/graphql":
                if error := state.get("graphql_error"):
                    status, message = error
                    value = {"message": message}
                else:
                    value = state.get("graphql", {"data": {"repository": {"pullRequest": {
                        "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                        "baseRef": {"branchProtectionRule": {"requiredStatusChecks": ([] if state.get("no_required_checks") else [
                            {"context": "required", "app": {"databaseId": 1}}])}}}}}})
            elif "/rules/branches/" in self.path:
                if state.get("rules_error"):
                    status, message = state["rules_error"]
                    value = {"message": message}
                else:
                    pages = state.get("rules_pages")
                    index = 1 if "page=2" in self.path else 0
                    value = pages[index] if pages else state.get("rules", [])
                    if pages and index + 1 < len(pages):
                        headers.append(("Link", f"<http://127.0.0.1:{server.server_port}{self.path}&page=2>; rel=\"next\""))
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": state.get("check_name", "required"), "head_sha": sha,
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
                if error := state.get("pulls_error"):
                    status, message = error
                    value = {"message": message}
                else:
                    value = state.get("current", {
                        "head": {"sha": sha}, "base": {"ref": "main"}, "state": "open", "merged": False,
                    })
            else:
                self.send_error(404)
                return
            self.send_response(status)
            for name, header in headers:
                self.send_header(name, header)
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
    gh.write_text(f"#!{sys.executable}\nimport re,sys,urllib.request,urllib.error\n"
                  f"url='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\nresponses=[]\n"
                  "while True:\n"
                  " try:\n  r=urllib.request.urlopen(url); status=r.status; body=r.read().decode(); link=r.headers.get('Link','')\n"
                  " except urllib.error.HTTPError as e:\n  status=e.code; body=e.read().decode(); link=e.headers.get('Link','')\n"
                  " responses.append((status,body))\n"
                  " match=re.search(r'<([^>]+)>; rel=\"next\"',link)\n"
                  " if '--paginate' not in sys.argv or not match: break\n"
                  " url=match.group(1)\n"
                  "if '--include' in sys.argv: print('['+''.join('HTTP/1.1 %s fixture\\r\\n\\r\\n%s' % item for item in responses)+']'); sys.exit(0 if responses[-1][0] < 400 else 1)\n"
                  "elif responses[-1][0] < 400: print(responses[-1][1])\n"
                  "else: sys.stderr.write(responses[-1][1]); sys.exit(1)\n")
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


def _task(conn, task_id):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


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


def test_real_gh_include_slurp_rules_payload_accepts_required_evidence(github):
    """The local shim emits gh's bracketed --include + --slurp framing."""
    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7")

    assert receipt["ok"], receipt
    assert receipt["classification"] == "success"
    assert receipt["required"] == [{"context": "required", "app_id": 1}]


@pytest.mark.parametrize(("state", "merged", "classification"), [
    ("corrupt", "yes", "infra"),
    ("open", "yes", "infra"),
    ("closed", False, "stale"),
])
def test_final_pr_state_requires_valid_rest_state_and_merge_evidence(github, state, merged, classification):
    github["current"] = {
        "head": {"sha": github["head"]}, "base": {"ref": "main"}, "state": state, "merged": merged,
    }

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7")

    assert not receipt["ok"]
    assert receipt["classification"] == classification


@pytest.mark.parametrize("policy", ["required-checks", "local-if-no-required-checks"])
@pytest.mark.parametrize(("conclusion", "missing", "stale", "classification"), [
    ("success", True, False, "missing"),
    ("failure", False, False, "failure"),
    ("pending", False, False, "pending"),
    ("success", False, True, "stale"),
    ("success", False, False, "success"),
])
def test_ruleset_requirements_remain_mandatory_under_both_policies(
        github, policy, conclusion, missing, stale, classification):
    """Ruleset checks cannot be bypassed by the explicit no-CI policy."""
    github.update(no_required_checks=True, check_name="ruleset-required", conclusion=conclusion,
                  missing=missing, stale=stale, rules_pages=[
                      [],
                      [{"type": "required_status_checks", "parameters": {
                          "required_status_checks": [
                              {"context": "ruleset-required", "integration_id": 1},
                          ],
                      }}],
                  ])

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7", policy=policy)

    assert receipt["ok"] is (classification == "success")
    assert receipt["classification"] == classification
    assert receipt["required"] == [{"context": "ruleset-required", "app_id": 1}]
    assert receipt["checks"][0]["name"] == "ruleset-required"
    assert receipt["verification_source"] == (
        "required-checks" if classification == "success" else None
    )
    assert any("page=2" in request for request in github["requests"])


@pytest.mark.parametrize(("current", "ok", "classification"), [
    ({"head": {"sha": "a" * 40}, "base": {"ref": "other"}, "state": "open", "merged": False}, False, "stale"),
    ({"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "state": "closed", "merged": True}, True, "success"),
    ([], False, "infra"),
])
def test_final_pr_response_mismatch_closed_and_malformed_paths_fail_closed(github, current, ok, classification):
    github["current"] = current

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7")

    assert receipt["ok"] is ok
    assert receipt["classification"] == classification


@pytest.mark.parametrize(("current", "pulls_error", "classification"), [
    ({"head": {"sha": "b" * 40}, "base": {"ref": "main"}, "state": "open", "merged": False}, None, "stale"),
    ({"head": {"sha": "a" * 40}, "base": {"ref": "other"}, "state": "open", "merged": False}, None, "stale"),
    ({"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "state": "closed", "merged": False}, None, "stale"),
    ({"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "state": "corrupt", "merged": False}, None, "infra"),
    ({"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "state": "open", "merged": "no"}, None, "infra"),
    ([], None, "infra"),
    (None, (500, "GitHub temporarily unavailable"), "infra"),
])
def test_explicit_local_policy_still_requires_a_matching_final_pr_reread(
        github, current, pulls_error, classification):
    github.update(no_required_checks=True)
    if current is not None:
        github["current"] = current
    if pulls_error is not None:
        github["pulls_error"] = pulls_error

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7",
                                 policy="local-if-no-required-checks")

    assert not receipt["ok"]
    assert receipt["classification"] == classification
    assert any("/pulls/7" in request for request in github["requests"])


@pytest.mark.parametrize("fault", ["graphql", "rules"])
def test_malformed_github_api_evidence_is_infrastructure_failure(github, fault):
    if fault == "graphql":
        github["graphql"] = {"data": {"repository": {}}}
    else:
        github["rules"] = {"not": "a page"}

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7")

    assert not receipt["ok"]
    assert receipt["classification"] == "infra"


def test_missing_gh_subprocess_is_infrastructure_failure(github, monkeypatch):
    monkeypatch.setenv("PATH", "")

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7")

    assert not receipt["ok"]
    assert receipt["classification"] == "infra"


def test_policy_rejections_leave_task_identity_and_state_unchanged(github):
    contract = "https://github.com/acme/repo/pull/7"
    with connect() as conn:
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        terminal = kb.create_task(conn, title="terminal", completion_contract=contract)
        assert kb.complete_task(conn, terminal, metadata={"published_pr": contract})
        mutable = kb.create_task(conn, title="mutable", completion_contract=contract)
        tasks = {task_id: _task(conn, task_id) for task_id in (local, terminal, mutable)}
        before = {
            task_id: (task.status, task.completion_contract, task.pr_acceptance_policy)
            for task_id, task in tasks.items()
        }
        with pytest.raises(ValueError):
            set_pr_acceptance_policy(conn, local, "local-if-no-required-checks", reason="local", author="test")
        with pytest.raises(ValueError):
            set_pr_acceptance_policy(conn, terminal, "required-checks", reason="terminal", author="test")
        with pytest.raises(ValueError):
            set_pr_acceptance_policy(conn, mutable, "unknown", reason="unknown", author="test")
        with pytest.raises(ValueError):
            set_pr_acceptance_policy(conn, mutable, "required-checks", reason="   ", author="test")
        tasks = {task_id: _task(conn, task_id) for task_id in (local, terminal, mutable)}
        after = {task_id: (task.status, task.completion_contract, task.pr_acceptance_policy)
                 for task_id, task in tasks.items()}
        events = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id IN (?, ?, ?) AND kind='pr_acceptance_policy_changed'",
            (local, terminal, mutable),
        ).fetchone()[0]
    assert after == before
    assert events == 0


def test_delegated_child_cannot_mutate_pr_policy(github, monkeypatch):
    contract = "https://github.com/acme/repo/pull/7"
    with connect() as conn:
        task_id = kb.create_task(conn, title="publish", completion_contract=contract, initial_status="blocked")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")

    output = kc.run_slash(
        f'set-pr-policy {task_id} local-if-no-required-checks --reason "must not be accepted"',
    )

    with connect() as conn:
        task = _task(conn, task_id)
    assert "cannot mutate Kanban tasks" in output
    assert task.pr_acceptance_policy is None
    assert task.completion_contract == contract


def test_first_published_pr_binds_contract_and_rejects_a_sibling_pr(github):
    github["conclusion"] = "failure"
    with connect() as conn:
        task_id = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, task_id, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert not kb.complete_task(conn, task_id, metadata={"published_pr": "https://github.com/acme/repo/pull/8"})
        task = _task(conn, task_id)
    assert task.completion_contract == "https://github.com/acme/repo/pull/7"


def test_policy_change_during_acceptance_cannot_commit_stale_evidence(github):
    contract = "https://github.com/acme/repo/pull/7"
    with connect() as conn:
        task_id = kb.create_task(conn, title="race", completion_contract=contract)
        owner = kb.claim_task(conn, task_id)
        assert owner is not None

        def change_policy():
            with connect() as rival:
                assert set_pr_acceptance_policy(
                    rival, task_id, "local-if-no-required-checks", reason="new authority", author="rival",
                )

        github["race"] = change_policy
        assert not kb.complete_task(conn, task_id, expected_run_id=owner.current_run_id,
                                    metadata={"published_pr": contract})
        task = _task(conn, task_id)
        receipts = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (task_id,),
        ).fetchone()[0]
    assert task.status != "done"
    assert task.pr_acceptance_policy == "local-if-no-required-checks"
    assert receipts == 0


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


@pytest.mark.parametrize("endpoint", ["graphql", "pulls"])
def test_plan_visibility_403_from_any_non_rules_endpoint_is_infrastructure_failure(github, endpoint):
    """The narrow unavailable-rules exception never applies to unrelated endpoints."""
    error = (403, "Upgrade to GitHub Pro or make this repository public to enable this feature.")
    github["no_required_checks"] = True
    github[f"{endpoint}_error"] = error

    receipt = collect_acceptance("https://github.com/acme/repo/pull/7",
                                 "https://github.com/acme/repo/pull/7",
                                 policy="local-if-no-required-checks")

    assert not receipt["ok"]
    assert receipt["classification"] == "infra"
    if endpoint == "pulls":
        assert any("/pulls/7" in request for request in github["requests"])


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
