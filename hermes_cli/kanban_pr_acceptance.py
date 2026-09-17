"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured ownership snapshot under its lock.
"""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
_HTTP_STATUS = re.compile(r"HTTP/\S+ ([1-5][0-9]{2})\b")
_RULES_PLAN_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."
REQUIRED_CHECKS_POLICY = "required-checks"
LOCAL_IF_NO_REQUIRED_CHECKS_POLICY = "local-if-no-required-checks"
VALID_PR_ACCEPTANCE_POLICIES = frozenset({REQUIRED_CHECKS_POLICY, LOCAL_IF_NO_REQUIRED_CHECKS_POLICY})


def effective_policy(value: str | None) -> str:
    """Resolve the nullable storage field; unexpected persisted values fail closed."""
    if value is None:
        return REQUIRED_CHECKS_POLICY
    if value not in VALID_PR_ACCEPTANCE_POLICIES:
        raise ValueError("invalid persisted PR acceptance policy")
    return value


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _rules_api(endpoint: str):
    """Read rules with an HTTP status line so only one documented 403 is special.

    ``gh`` normally collapses every REST failure to process exit 1. ``--include``
    keeps the status and JSON error body together, allowing this narrow exception
    without trusting arbitrary stderr text.
    """
    command = ["gh", "api", endpoint, "--hostname", "github.com", "--paginate", "--slurp", "--include"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=False)
    output = result.stdout.replace("\r\n", "\n")
    if not output.startswith("["):
        raise ValueError("GitHub rules response lacked gh slurp framing")
    decoder = json.JSONDecoder()
    packets: list[tuple[int, object]] = []
    position = 1
    while True:
        status_match = _HTTP_STATUS.match(output, position)
        separator = output.find("\n\n", position)
        if not status_match or separator < 0:
            raise ValueError("GitHub rules response lacked structured HTTP evidence")
        try:
            value, position = decoder.raw_decode(output, separator + 2)
        except json.JSONDecodeError:
            raise ValueError("GitHub rules response was malformed") from None
        packets.append((int(status_match.group(1)), value))
        while position < len(output) and output[position].isspace():
            position += 1
        if position < len(output) and output[position] == "]":
            position += 1
            while position < len(output) and output[position].isspace():
                position += 1
            if position == len(output):
                break
            raise ValueError("GitHub rules response had malformed gh slurp framing")
        if position < len(output) and output[position] == ",":
            position += 1
        if position >= len(output) or not output.startswith("HTTP", position):
            raise ValueError("GitHub rules response had malformed gh slurp framing")
    if len(packets) == 1 and packets[0][0] == 403 and isinstance(packets[0][1], dict) and packets[0][1].get("message") == _RULES_PLAN_MESSAGE:
        return None
    if result.returncode or any(status != 200 for status, _ in packets):
        raise ValueError("GitHub rules endpoint failed")
    pages = [value for _, value in packets]
    if not all(isinstance(page, list) for page in pages):
        raise ValueError("GitHub rules response was malformed")
    return pages


def _required_from_rules(pages: list) -> set[tuple[str, int | None]]:
    required: set[tuple[str, int | None]] = set()
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("GitHub rules pagination was malformed")
        for rule in page:
            if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
                raise ValueError("GitHub rules response was malformed")
            if rule["type"] != "required_status_checks":
                continue
            parameters = rule.get("parameters")
            checks = parameters.get("required_status_checks") if isinstance(parameters, dict) else None
            if not isinstance(checks, list):
                raise ValueError("GitHub ruleset requirements were malformed")
            for check in checks:
                context = check.get("context") if isinstance(check, dict) else None
                app_id = check.get("integration_id") if isinstance(check, dict) else None
                if not isinstance(context, str) or not context or app_id is not None and not isinstance(app_id, int):
                    raise ValueError("GitHub ruleset requirements were malformed")
                required.add((context, app_id))
    return required


def _read_pr(repo: str, number: int) -> tuple[str, str, set[tuple[str, int | None]]]:
    owner, name = repo.split("/")
    query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state
        baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
            json.dumps(owner), json.dumps(name), number)
    value = _api("graphql", query=query)
    try:
        pr = value["data"]["repository"]["pullRequest"]
        sha, branch, state = pr["headRefOid"], pr["baseRefName"], pr["state"]
    except (KeyError, TypeError):
        raise ValueError("GitHub PR response was malformed") from None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("PR current head is unavailable")
    if not isinstance(branch, str) or not branch or state not in {"OPEN", "MERGED"}:
        raise ValueError("PR is closed or current head is unavailable")
    protection = (pr.get("baseRef") or {}).get("branchProtectionRule") if isinstance(pr, dict) else None
    checks = protection.get("requiredStatusChecks", []) if isinstance(protection, dict) else []
    if not isinstance(checks, list):
        raise ValueError("GitHub branch protection response was malformed")
    required: set[tuple[str, int | None]] = set()
    for check in checks:
        context = check.get("context") if isinstance(check, dict) else None
        app = check.get("app") if isinstance(check, dict) else None
        app_id = app.get("databaseId") if isinstance(app, dict) else None
        if not isinstance(context, str) or not context or app_id is not None and not isinstance(app_id, int):
            raise ValueError("GitHub branch protection response was malformed")
        required.add((context, app_id))
    return sha, branch, required


def _current_pr_matches(current, sha: str, branch: str) -> bool:
    try:
        current_sha = current["head"]["sha"]
        current_branch = current["base"]["ref"]
        state = current["state"]
        merged = current["merged"]
    except (KeyError, TypeError):
        raise ValueError("GitHub final PR response was malformed") from None
    if (not isinstance(current_sha, str) or not isinstance(current_branch, str)
            or state not in {"open", "closed"} or not isinstance(merged, bool)):
        raise ValueError("GitHub final PR response was malformed")
    if state == "open" and merged:
        raise ValueError("GitHub final PR response was malformed")
    return current_sha == sha and current_branch == branch and (state == "open" or merged)


def _check_pages(pages, sha: str) -> list[dict]:
    if not isinstance(pages, list) or not pages or not all(isinstance(page, dict) for page in pages):
        raise ValueError("GitHub check-run pagination was malformed")
    if not isinstance(pages[0].get("total_count"), int):
        raise ValueError("GitHub check-run pagination was malformed")
    runs: list[dict] = []
    for page in pages:
        values = page.get("check_runs")
        if not isinstance(values, list) or not all(isinstance(run, dict) for run in values):
            raise ValueError("GitHub check-run pagination was malformed")
        runs.extend(values)
    ids = {run.get("id") for run in runs}
    if None in ids or len(ids) != len(runs) or len(ids) != pages[0]["total_count"]:
        raise ValueError("Incomplete check-run pagination")
    return runs


def collect_acceptance(contract: str, published_pr: str | None, *, policy: str | None = None) -> dict:
    """Collect current PR evidence without mutating durable task state."""
    receipt = {
        "ok": False, "classification": "missing", "head_sha": None, "pr_url": published_pr,
        "checks": [], "required": [], "policy": None, "ruleset_unavailable": False,
        "verification_source": None,
        "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                    "Use kanban_block if human input is needed; receipts remain on the task event log.",
    }
    try:
        policy = effective_policy(policy)
        receipt["policy"] = policy
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        sha, branch, required = _read_pr(repo, number)
        receipt["head_sha"] = sha
        rules = _rules_api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100")
        if rules is None:
            receipt["ruleset_unavailable"] = True
        else:
            required.update(_required_from_rules(rules))
        receipt["required"] = [{"context": context, "app_id": app_id}
                               for context, app_id in sorted(required, key=str)]

        if not required:
            current = _api(f"repos/{repo}/pulls/{number}")
            if not _current_pr_matches(current, sha, branch):
                receipt.update(classification="stale", detail="PR head/base/state changed while collecting evidence; retry.")
                return receipt
            if policy == LOCAL_IF_NO_REQUIRED_CHECKS_POLICY:
                receipt.update(ok=True, classification="success", verification_source="declared-local",
                               detail="No repository-required checks are configured; declared local verification is authoritative.")
                return receipt
            receipt.update(classification="no-required-checks",
                           detail="No repository-required checks are configured.",
                           recovery="Document authoritative local verification, then explicitly opt in with "
                                    "`hermes kanban set-pr-policy TASK_ID local-if-no-required-checks --reason \"...\"` "
                                    "and retry completion. This does not run local verification automatically.")
            return receipt

        runs = _check_pages(_api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True), sha)
        status_pages = _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True)
        if not isinstance(status_pages, list) or not all(isinstance(page, list) for page in status_pages):
            raise ValueError("GitHub status pagination was malformed")
        statuses = [{**status, "sha": sha} for page in status_pages for status in page if isinstance(status, dict)]
        if sum(len(page) for page in status_pages) != len(statuses):
            raise ValueError("GitHub status pagination was malformed")
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [run for run in runs if run.get("name") == context and
                        (app_id in (None, -1) or (run.get("app") or {}).get("id") == app_id)]
            legacy = [status for status in statuses if status.get("context") == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda status: status.get("id", -1))] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check.get("state")
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check.get("id"),
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        current = _api(f"repos/{repo}/pulls/{number}")
        if not _current_pr_matches(current, sha, branch):
            receipt.update(classification="stale", detail="PR head/base/state changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((outcome for outcome in outcomes if outcome != "success"), "success")
        receipt["ok"] = receipt["classification"] == "success"
        if receipt["ok"]:
            receipt["verification_source"] = "required-checks"
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")