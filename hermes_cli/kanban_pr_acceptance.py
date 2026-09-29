"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured ownership snapshot under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
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


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _rules_api(endpoint: str, *, profile_home: str | None = None):
    """Read rules with an HTTP status line so only one documented 403 is special.

    ``gh`` normally collapses every REST failure to process exit 1. ``--include``
    keeps the status and JSON error body together, allowing this narrow exception
    without trusting arbitrary stderr text.

    The documented plan-gated 403 ("Upgrade to GitHub Pro...") is checked BEFORE
    any auth classification: on a private free-tier repo it means "rulesets are
    unreadable by anyone", a real product limit to record as
    ``ruleset_unavailable``, not this profile's login failing. Every other 401/
    403/404 IS an identity failure and raises :class:`_GateAuthError`.
    """
    command = ["gh", "api", endpoint, "--hostname", "github.com", "--paginate", "--slurp", "--include"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30,
                            check=False, env=_gh_env(profile_home))
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
    denied = next((status for status, _ in packets if status in {401, 403, 404}), None)
    if denied is not None:
        raise _GateAuthError(f"HTTP {denied} on {endpoint.split('?')[0]}")
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


def _read_pr(repo: str, number: int, *,
             profile_home: str | None = None) -> tuple[str, str, set[tuple[str, int | None]]]:
    owner, name = repo.split("/")
    query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state
        baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
            json.dumps(owner), json.dumps(name), number)
    value = _api("graphql", query=query, profile_home=profile_home)
    try:
        repository = value["data"]["repository"]
    except (KeyError, TypeError):
        raise ValueError("GitHub PR response was malformed") from None
    if repository is None:
        # A private repo the login cannot read resolves to null, not an error.
        raise _GateAuthError(f"HTTP 404 on graphql {repo}")
    try:
        pr = repository["pullRequest"]
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


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None, *, policy: str | None = None) -> dict:
    """Collect current PR evidence without mutating durable task state.

    ``assignee`` selects whose ``gh`` login reads the evidence (see :func:`_gh_env`);
    ``policy`` decides whether a repo with no required checks can be accepted on
    declared local verification (see :func:`effective_policy`).
    """
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
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        sha, branch, required = _read_pr(repo, number, profile_home=profile_home)
        receipt["head_sha"] = sha
        rules = _rules_api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                           profile_home=profile_home)
        if rules is None:
            receipt["ruleset_unavailable"] = True
        else:
            required.update(_required_from_rules(rules))
        receipt["required"] = [{"context": context, "app_id": app_id}
                               for context, app_id in sorted(required, key=str)]

        if not required:
            current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
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

        runs = _check_pages(_api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                                 paginate=True, profile_home=profile_home), sha)
        status_pages = _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                            paginate=True, profile_home=profile_home)
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
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if not _current_pr_matches(current, sha, branch):
            receipt.update(classification="stale", detail="PR head/base/state changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((outcome for outcome in outcomes if outcome != "success"), "success")
        receipt["ok"] = receipt["classification"] == "success"
        if receipt["ok"]:
            receipt["verification_source"] = "required-checks"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
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
