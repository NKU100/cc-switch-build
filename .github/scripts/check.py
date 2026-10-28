"""Public release metadata checker. This module never checks out private code."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Literal, TypedDict


UPSTREAM = "farion1231/cc-switch"
SOURCE = "NKU100/cc-switch-src"
BUILD = "NKU100/cc-switch-build"
VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
ACTIVE = {"queued", "waiting", "in_progress", "requested", "pending"}
TERMINAL = {"completed"}
AUTH_FENCE_TITLE = "sync-auth-state"


class Decision(TypedDict):
    action: Literal["noop", "sync", "build", "budget"]
    reason: str
    version: str


class ReleaseState(TypedDict):
    version: str
    published: bool
    tagExists: bool
    syncActive: bool
    buildActive: bool
    syncFailed: bool
    syncHeld: bool
    buildFailed: bool


class GithubApiError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub API request failed with HTTP {status}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class GithubApi:
    def __init__(
        self,
        token: str,
        api_base: str = "https://api.github.com",
        *,
        build_dispatch_token: str | None = None,
    ):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._build_dispatch_token = build_dispatch_token

    def request(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        build_dispatch = (
            method == "POST"
            and path == "/repos/NKU100/cc-switch-build/actions/workflows/build.yml/dispatches"
        )
        token = self._token
        if build_dispatch:
            if not self._build_dispatch_token:
                raise ValueError("Build dispatch token is not configured")
            token = self._build_dispatch_token
        url = self._api_base + path if path.startswith("/") else self._api_base + "/" + path
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=30) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            raise GithubApiError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("GitHub API transport failed") from None
        if not data:
            return None
        try:
            result = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("GitHub API returned invalid JSON") from None
        if not isinstance(result, (dict, list)):
            raise ValueError("GitHub API returned an unexpected JSON value")
        return result

    def pages(self, path: str) -> list[dict]:
        result: list[dict] = []
        separator = "&" if "?" in path else "?"
        next_path = f"{path}{separator}per_page=100&page=1"
        while next_path:
            response = self.request("GET", next_path)
            page = response.get("workflow_runs") if isinstance(response, dict) else response
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise ValueError("GitHub API pagination returned an invalid page")
            result.extend(page)
            if len(page) < 100:
                break
            query = urllib.parse.urlsplit(next_path)
            params = urllib.parse.parse_qs(query.query)
            params["page"] = [str(int(params.get("page", ["1"])[0]) + 1)]
            next_path = urllib.parse.urlunsplit(("", "", query.path, urllib.parse.urlencode(params, doseq=True), ""))
        return result

    def ensure_issue(self, repo: str, title: str, body: str) -> int:
        issues = self.pages(f"/repos/{repo}/issues?state=open")
        issue = exact_open_issue(issues, title)
        if issue:
            return issue["number"]
        created = self.request("POST", f"/repos/{repo}/issues", {"title": title, "body": body})
        if not isinstance(created, dict) or type(created.get("number")) is not int:
            raise ValueError("GitHub API did not confirm issue creation")
        return created["number"]


def validate_version(version: str) -> str:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("Release version must match vX.Y.Z")
    return version


def decide(state: ReleaseState, used_minutes: float, limit_minutes: float = 1600) -> Decision:
    expected_keys = {"version", "published", "tagExists", "syncActive", "buildActive", "syncFailed", "syncHeld", "buildFailed"}
    if not isinstance(state, dict) or set(state) != expected_keys:
        raise ValueError("Release state has an unexpected schema")
    version = validate_version(state["version"])
    for key in ("published", "tagExists", "syncActive", "buildActive", "syncFailed", "syncHeld", "buildFailed"):
        if type(state[key]) is not bool:
            raise ValueError(f"Invalid release state: {key}")
    if type(used_minutes) not in (int, float) or not math.isfinite(used_minutes) or used_minutes < 0:
        raise ValueError("Used Actions minutes must be finite and nonnegative")
    if type(limit_minutes) not in (int, float) or not math.isfinite(limit_minutes) or limit_minutes <= 0:
        raise ValueError("Monthly Actions minute limit must be finite and positive")
    if state["published"]:
        action, reason = "noop", "formal_release_exists"
    elif state["tagExists"]:
        if state["buildActive"] or state["buildFailed"]:
            action, reason = "noop", "build_active_or_held"
        else:
            action, reason = "build", "accepted_tag_needs_release"
    elif state["syncActive"] or state["syncFailed"] or state["syncHeld"]:
        action, reason = "noop", "sync_active_or_held"
    elif used_minutes >= limit_minutes:
        action, reason = "budget", "source_monthly_limit_reached"
    else:
        action, reason = "sync", "latest_release_not_synced"
    return {"action": action, "reason": reason, "version": version}


def _parse_actions_minutes(report: dict, repo: str | None) -> float:
    if not isinstance(report, dict) or "usageItems" not in report:
        raise ValueError("Billing report is missing usageItems")
    rows = report["usageItems"]
    if not isinstance(rows, list):
        raise ValueError("Billing usageItems must be a list")
    total = 0.0
    minute_skus = {
        "Actions Linux",
        "Actions Linux ARM",
        "Actions Windows",
        "Actions Windows ARM",
        "Actions macOS",
        "Actions macOS 3-core",
    }
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("product"), str):
            raise ValueError("Billing row has invalid schema")
        if row["product"].casefold() != "actions":
            continue
        if not isinstance(row.get("repositoryName"), str):
            raise ValueError("Actions billing row is missing repositoryName")
        sku = row.get("sku")
        unit_type = row.get("unitType")
        is_storage = sku == "Actions storage" and unit_type == "GigabyteHours"
        if not is_storage and (sku not in minute_skus or unit_type not in ("minutes", "Minutes")):
            raise ValueError("Unknown Actions billing SKU or unitType")
        quantity = row.get("quantity")
        if type(quantity) not in (int, float) or not math.isfinite(quantity) or quantity < 0:
            raise ValueError("Actions billing quantity must be a nonnegative number")
        if is_storage or (
            repo is not None
            and row["repositoryName"] not in (repo, repo.rsplit("/", 1)[-1])
        ):
            continue
        total += float(quantity)
    return total


def parse_actions_minutes(report: dict, repo: str) -> float:
    return _parse_actions_minutes(report, repo)


def latest_formal_release(releases: list[dict]) -> dict:
    if not isinstance(releases, list):
        raise ValueError("Upstream releases response is invalid")
    for release in releases:
        if not isinstance(release, dict) or type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise ValueError("Upstream release record is invalid")
        if release["draft"] or release["prerelease"]:
            continue
        validate_version(release.get("tag_name"))
        return release
    raise ValueError("No latest formal upstream release exists")


def exact_open_issue(issues: list[dict], title: str) -> dict | None:
    if not isinstance(issues, list):
        raise ValueError("Issues response is invalid")
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("title"), str) or issue.get("state") not in ("open", "closed"):
            raise ValueError("Issue record is invalid")
        if issue.get("pull_request") is not None:
            continue
        if issue["title"] == title and issue["state"] == "open":
            if type(issue.get("number")) is not int:
                raise ValueError("Issue number is invalid")
            return issue
    return None


def _optional(api: GithubApi, path: str) -> dict | list | None:
    try:
        return api.request("GET", path)
    except GithubApiError as error:
        if error.status == 404:
            return None
        raise


def _active_run(run: dict, version: str | None = None) -> bool:
    status = run.get("status")
    title = run.get("display_title")
    if not isinstance(status, str) or status not in ACTIVE | TERMINAL or not isinstance(title, str):
        raise ValueError("Workflow run metadata is invalid")
    if status in TERMINAL:
        return False
    if version is None:
        return True
    return title == f"private-build {version}"


def _auth_fence_active(issues: list[dict]) -> bool:
    for issue in issues:
        if issue.get("state") != "open" or issue.get("pull_request") is not None or issue.get("title") != AUTH_FENCE_TITLE:
            continue
        return True
    return False


def release_state(version: str, private_release: dict | None, tag: dict | None,
                  issues: list[dict], build_issues: list[dict],
                  sync_runs: list[dict], build_runs: list[dict]) -> ReleaseState:
    version = validate_version(version)
    if private_release is not None and (
        private_release.get("tag_name") != version
        or type(private_release.get("draft")) is not bool
        or type(private_release.get("prerelease")) is not bool
    ):
        raise ValueError("Private release metadata is invalid")
    if tag is not None and tag.get("ref") != f"refs/tags/{version}":
        raise ValueError("Private tag metadata is invalid")
    return {
        "version": version,
        "published": bool(private_release and private_release.get("draft") is False and private_release.get("prerelease") is False),
        "tagExists": tag is not None,
        "syncActive": any(_active_run(run) for run in sync_runs),
        "buildActive": any(_active_run(run, version) for run in build_runs),
        "syncFailed": exact_open_issue(issues, f"sync-failed: {version}") is not None,
        "syncHeld": exact_open_issue(issues, f"sync-held: {version}") is not None or _auth_fence_active(issues),
        "buildFailed": exact_open_issue(build_issues, f"build-failed: {version}") is not None,
    }


def _read_state(api: GithubApi, version: str) -> tuple[ReleaseState, list[dict]]:
    published_result = _optional(api, f"/repos/{SOURCE}/releases/tags/{version}")
    if published_result is not None and not isinstance(published_result, dict):
        raise ValueError("Private release response is invalid")
    tag = _optional(api, f"/repos/{SOURCE}/git/ref/tags/{version}")
    if tag is not None and not isinstance(tag, dict):
        raise ValueError("Private tag response is invalid")
    issues = api.pages(f"/repos/{SOURCE}/issues?state=open")
    build_issues = api.pages(f"/repos/{BUILD}/issues?state=open")
    sync_runs = api.pages(f"/repos/{SOURCE}/actions/workflows/sync.yml/runs")
    build_runs = api.pages(f"/repos/{BUILD}/actions/workflows/build.yml/runs")
    for runs in (sync_runs, build_runs):
        if any(not isinstance(run, dict) for run in runs):
            raise ValueError("Workflow runs response is invalid")
    state = release_state(version, published_result, tag, issues, build_issues, sync_runs, build_runs)
    return state, issues


def _billing(api: GithubApi, now: datetime) -> tuple[float, float]:
    if now.tzinfo is None:
        raise ValueError("Checker clock must be timezone-aware")
    current = now.astimezone(timezone.utc)
    report = api.request("GET", f"/users/NKU100/settings/billing/usage?year={current.year}&month={current.month}")
    return parse_actions_minutes(report, SOURCE), _parse_actions_minutes(report, None)


def check(
    api: GithubApi,
    now: datetime,
    limit_minutes: float,
    *,
    rollout_enabled: bool = False,
    zero_paid_confirmed: bool = False,
    account_free_minutes: float | None = None,
    account_reserve_minutes: float | None = None,
    dry_run: bool = True,
) -> Decision:
    if not rollout_enabled and not dry_run:
        return {"action": "noop", "reason": "automation_rollout_disabled", "version": ""}
    if type(limit_minutes) not in (int, float) or not math.isfinite(limit_minutes) or limit_minutes <= 0:
        raise ValueError("Monthly source minute limit is invalid")
    upstream = api.pages(f"/repos/{UPSTREAM}/releases")
    release = latest_formal_release(upstream)
    version = validate_version(release["tag_name"])
    build_repo = api.request("GET", f"/repos/{BUILD}")
    if not isinstance(build_repo, dict) or not isinstance(build_repo.get("default_branch"), str):
        raise ValueError("Public build default branch metadata is unavailable")
    branch = build_repo["default_branch"]
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("-"):
        raise ValueError("Public build default branch metadata is invalid")
    state, issues = _read_state(api, version)
    if state["published"]:
        return decide(state, 0, limit_minutes)
    if state["tagExists"]:
        decision = decide(state, 0, limit_minutes)
        if decision["action"] == "build" and not dry_run:
            fresh, _ = _read_state(api, version)
            if decide(fresh, 0, limit_minutes)["action"] == "build":
                api.request("POST", f"/repos/{BUILD}/actions/workflows/build.yml/dispatches", {"ref": branch, "inputs": {"version": version}})
        return decision
    try:
        used, account_used = _billing(api, now)
    except Exception:
        if not dry_run:
            api.ensure_issue(
                SOURCE,
                "budget",
                "Unable to confirm current-month private source Actions usage. Sync dispatch is held until billing metadata and quota can be verified.",
            )
        raise
    decision = decide(state, used, limit_minutes)
    budget_issue = exact_open_issue(issues, "budget")
    if used >= limit_minutes:
        if not dry_run:
            api.ensure_issue(SOURCE, "budget", "Private source Actions usage reached the configured monthly minute limit. Sync dispatch is held until the UTC month changes and usage is checked again.")
        return decision
    if account_free_minutes is None or account_reserve_minutes is None:
        return {"action": "noop", "reason": "shared_free_quota_gate_unconfirmed", "version": version}
    if (
        type(account_free_minutes) not in (int, float)
        or not math.isfinite(account_free_minutes)
        or type(account_reserve_minutes) not in (int, float)
        or not math.isfinite(account_reserve_minutes)
        or account_free_minutes <= 0
        or account_reserve_minutes < 0
        or account_reserve_minutes >= account_free_minutes
    ):
        raise ValueError("Shared Actions free quota settings are invalid")
    if account_used + account_reserve_minutes >= account_free_minutes:
        if not dry_run:
            api.ensure_issue(SOURCE, "budget", "Shared account Actions usage has reached the configured free quota reserve. Sync dispatch is held until the next UTC month and a fresh usage check.")
        return {"action": "budget", "reason": "shared_free_quota_reserve_reached", "version": version}
    if budget_issue is not None and not dry_run:
        api.request("PATCH", f"/repos/{SOURCE}/issues/{budget_issue['number']}", {"state": "closed"})
    if decision["action"] == "sync":
        fence_blocked = _auth_fence_active(issues)
        if fence_blocked:
            return {"action": "noop", "reason": "sync_auth_state_fence_open", "version": version}
        if not zero_paid_confirmed:
            return {"action": "noop", "reason": "zero_paid_budget_gate_unconfirmed", "version": version}
        if not dry_run:
            fresh, fresh_issues = _read_state(api, version)
            if _auth_fence_active(fresh_issues) or decide(fresh, used, limit_minutes)["action"] != "sync":
                return {"action": "noop", "reason": "state_changed_before_dispatch", "version": version}
            if not zero_paid_confirmed:
                return {"action": "noop", "reason": "zero_paid_budget_gate_unconfirmed", "version": version}
            api.request("POST", f"/repos/{SOURCE}/actions/workflows/sync.yml/dispatches", {"ref": "main", "inputs": {"version": version}})
    return decision


def main() -> int:
    token = os.environ.get("SRC_TOKEN")
    if not token:
        print(json.dumps({"action": "noop", "reason": "billing_or_metadata_unavailable", "version": ""}))
        return 1
    try:
        limit = float(os.environ["SYNC_MONTHLY_MINUTE_LIMIT"])
        api = GithubApi(token, build_dispatch_token=os.environ.get("BUILD_DISPATCH_TOKEN"))
        result = check(
            api,
            datetime.now(timezone.utc),
            limit,
            rollout_enabled=os.environ.get("CHECK_AUTOMATION_ENABLED", "false").lower() == "true",
            zero_paid_confirmed=os.environ.get("ZERO_PAID_BUDGET_CONFIRMED", "false").lower() == "true",
            account_free_minutes=_optional_float(os.environ.get("ACCOUNT_FREE_ACTIONS_MINUTES")),
            account_reserve_minutes=_optional_float(os.environ.get("ACCOUNT_ACTIONS_MINUTE_RESERVE")),
            dry_run=os.environ.get("CHECK_DRY_RUN", "true").lower() == "true",
        )
    except Exception:
        print(json.dumps({"action": "noop", "reason": "billing_or_metadata_unavailable", "version": ""}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError("Configured minute gate is invalid") from None


if __name__ == "__main__":
    raise SystemExit(main())
