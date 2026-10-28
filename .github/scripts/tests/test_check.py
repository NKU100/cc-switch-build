import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "check.py"
spec = importlib.util.spec_from_file_location("check", SCRIPT)
check_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_module)


def state(**changes):
    value = dict(version="v4.0.20", published=False, tagExists=False,
                 syncActive=False, buildActive=False, syncFailed=False,
                 syncHeld=False, buildFailed=False)
    return {**value, **changes}


class FakeApi:
    def __init__(self, *, latest, tag=False, used=0, other_used=0, budget_issue=False,
                 billing_error=None, second_sync_active=False, auth_fence=None):
        self.calls = []
        self.latest = latest
        self.tag = tag
        self.used = used
        self.other_used = other_used
        self.month = None
        self.budget_issue = budget_issue
        self.billing_error = billing_error
        self.second_sync_active = second_sync_active
        self.auth_fence = auth_fence
        self.state_reads = 0
        self.issues = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/dispatches"):
            return None
        if "billing/usage" in path:
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            self.month = (int(query["year"][0]), int(query["month"][0]))
            if self.billing_error:
                raise check_module.GithubApiError(self.billing_error)
            return {"usageItems": [
                {"product": "Actions", "sku": "Actions Linux", "quantity": self.used, "unitType": "minutes", "repositoryName": "NKU100/cc-switch-src"},
                {"product": "Actions", "sku": "Actions Linux", "quantity": self.other_used, "unitType": "minutes", "repositoryName": "NKU100/other"},
            ]}
        if path.endswith("/releases/latest"):
            return {"tag_name": self.latest, "draft": False, "prerelease": False}
        if "/releases/tags/" in path:
            return None
        if path.endswith("/git/ref/tags/" + self.latest):
            return {"ref": "refs/tags/" + self.latest} if self.tag else None
        if path.endswith("/actions/permissions"):
            return {"enabled": True}
        if path.endswith("/repos/NKU100/cc-switch-build"):
            return {"default_branch": "master"}
        if method == "PATCH" and "/issues/" in path:
            self.calls[-1] = (method, path, body)
            return {}
        raise AssertionError((method, path, body))

    def pages(self, path):
        self.calls.append(("PAGES", path, None))
        if path.startswith("/repos/farion1231/cc-switch/releases"):
            return [{"tag_name": self.latest, "draft": False, "prerelease": False}]
        if "/issues?" in path:
            issues = []
            if self.budget_issue:
                issues.append({"number": 100, "title": "budget", "state": "open", "pull_request": None})
            if self.auth_fence is not None:
                import json
                body = self.auth_fence if isinstance(self.auth_fence, str) else json.dumps(self.auth_fence)
                issues.append({"number": 101, "title": "sync-auth-state", "state": "open", "body": body, "pull_request": None})
            return issues
        if path.endswith("/runs"):
            if "cc-switch-src" in path:
                self.state_reads += 1
                if self.second_sync_active and self.state_reads > 1:
                    return [{"status": "in_progress", "display_title": "private-sync v4.0.20"}]
            return []
        raise AssertionError(path)

    def ensure_issue(self, repo, title, body):
        self.calls.append(("ISSUE", repo, (title, body)))
        self.issues.append((repo, title, body))
        self.budget_issue = True
        return 100


def fixture_api(**kwargs):
    return FakeApi(**kwargs)


class CheckDecisionTests(unittest.TestCase):
    def test_state_table(self):
        decide = check_module.decide
        self.assertEqual(decide(state(published=True), 0)["action"], "noop")
        self.assertEqual(decide(state(tagExists=True), 1600)["action"], "build")
        self.assertEqual(decide(state(), 1599)["action"], "sync")
        for field in ("syncFailed", "syncHeld", "syncActive"):
            self.assertEqual(decide(state(**{field: True}), 0)["action"], "noop", field)
        self.assertEqual(decide(state(tagExists=True, buildFailed=True), 0)["action"], "noop")
        self.assertEqual(decide(state(tagExists=True, buildActive=True), 0)["action"], "noop")
        for status in ("queued", "waiting", "in_progress"):
            parsed = check_module.release_state("v4.0.20", None, None, [], [], [{"status": status, "display_title": "private-sync v4.0.2"}], [])
            self.assertTrue(parsed["syncActive"], status)
        self.assertTrue(check_module.release_state("v4.0.20", None, None, [], [], [], [{"status": "waiting", "display_title": "private-build v4.0.20"}])["buildActive"])
        self.assertFalse(check_module.release_state("v4.0.20", None, None, [], [], [], [{"status": "waiting", "display_title": "private-build v4.0.2"}])["buildActive"])
        with self.assertRaises(ValueError):
            check_module.release_state("v4.0.20", None, None, [], [], [{"status": "unknown", "display_title": "private-sync v4.0.20"}], [])

    def test_budget_exact_threshold_and_next_month(self):
        api = fixture_api(latest="v4.0.20", used=1599, budget_issue=True)
        first = check_module.check(api, datetime(2026, 10, 31, 23, tzinfo=timezone.utc), 1600, rollout_enabled=True, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400, dry_run=False)
        self.assertEqual(first["action"], "sync")
        self.assertIn(("PATCH", "/repos/NKU100/cc-switch-src/issues/100", {"state": "closed"}), api.calls)
        self.assertTrue(any(call[0] == "POST" and call[1].endswith("/sync.yml/dispatches") for call in api.calls))
        exact_api = fixture_api(latest="v4.0.20", used=1600)
        exact = check_module.check(exact_api, datetime(2026, 10, 31, tzinfo=timezone.utc), 1600, rollout_enabled=True, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400, dry_run=False)
        self.assertEqual(exact["action"], "budget")
        self.assertEqual([issue[1] for issue in exact_api.issues], ["budget"])
        self.assertFalse(any(call[0] == "POST" and "dispatches" in call[1] for call in exact_api.calls))
        next_month_api = fixture_api(latest="v4.0.20", used=0, budget_issue=True)
        next_month = check_module.check(next_month_api, datetime(2026, 11, 1, tzinfo=timezone.utc), 1600, rollout_enabled=True, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400, dry_run=False)
        self.assertEqual(next_month["action"], "sync")
        self.assertEqual(next_month_api.month, (2026, 11))
        self.assertIn(("PATCH", "/repos/NKU100/cc-switch-src/issues/100", {"state": "closed"}), next_month_api.calls)

    def test_budget_does_not_block_public_build(self):
        result = check_module.check(fixture_api(latest="v4.0.20", tag=True, used=1600), datetime(2026, 10, 1, tzinfo=timezone.utc), 1600, rollout_enabled=True)
        self.assertEqual(result["action"], "build")

    def test_shared_account_reserve_holds_sync(self):
        api = fixture_api(latest="v4.0.20", used=5, other_used=100)
        result = check_module.check(api, datetime(2026, 10, 1, tzinfo=timezone.utc), 1600,
                                    rollout_enabled=True, zero_paid_confirmed=True,
                                    account_free_minutes=2000, account_reserve_minutes=1900,
                                    dry_run=False)
        self.assertEqual(result["action"], "budget")
        self.assertEqual(result["reason"], "shared_free_quota_reserve_reached")
        self.assertFalse(any(call[0] == "POST" and "dispatches" in call[1] for call in api.calls))
        self.assertEqual([issue[1] for issue in api.issues], ["budget"])

    def test_billing_filters_repository_and_units(self):
        report = {"usageItems": [
            {"product": "Actions", "sku": "Actions Linux", "quantity": 10, "unitType": "minutes", "repositoryName": "NKU100/cc-switch-src"},
            {"product": "Actions", "sku": "Actions Windows", "quantity": 20, "unitType": "minutes", "repositoryName": "NKU100/cc-switch-src"},
            {"product": "Actions", "sku": "Actions macOS", "quantity": 30, "unitType": "minutes", "repositoryName": "NKU100/cc-switch-src"},
            {"product": "Actions", "sku": "Actions Linux", "quantity": 900, "unitType": "minutes", "repositoryName": "NKU100/other"},
            {"product": "Copilot", "sku": "Copilot", "quantity": 900, "unitType": "seats", "repositoryName": "NKU100/cc-switch-src"},
        ]}
        self.assertEqual(check_module.parse_actions_minutes(report, "NKU100/cc-switch-src"), 60)

    def test_billing_unavailable_or_unknown_is_not_zero(self):
        for status in (401, 403, 404, 429):
            self.assertEqual(check_module.GithubApiError(status).status, status)
            api = fixture_api(latest="v4.0.20", billing_error=status)
            with self.assertRaises(check_module.GithubApiError):
                check_module.check(api, datetime(2026, 10, 1, tzinfo=timezone.utc), 1600, rollout_enabled=True, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400, dry_run=False)
            self.assertFalse(any(call[0] == "POST" and "dispatches" in call[1] for call in api.calls))
        for row in (
            {"product": "Actions", "sku": "unknown", "quantity": 1, "unitType": "minutes", "repositoryName": "NKU100/cc-switch-src"},
            {"product": "Actions", "sku": "Actions Linux", "unitType": "minutes", "repositoryName": "NKU100/cc-switch-src"},
        ):
            with self.assertRaises(ValueError):
                check_module.parse_actions_minutes({"usageItems": [row]}, "NKU100/cc-switch-src")

    def test_latest_only_and_invalid_version(self):
        releases = [
            {"tag_name": "v9.0.0-rc.1", "draft": False, "prerelease": True},
            {"tag_name": "v4.0.21", "draft": True, "prerelease": False},
            {"tag_name": "v4.0.20", "draft": False, "prerelease": False},
            {"tag_name": "v4.0.19", "draft": False, "prerelease": False},
        ]
        self.assertEqual(check_module.latest_formal_release(releases)["tag_name"], "v4.0.20")
        with self.assertRaises(ValueError):
            check_module.validate_version("v4.0.20; rm -rf /")

    def test_issue_filter_is_exact(self):
        issues = [{"number": 2, "title": "sync-failed: v4.0.2", "state": "open", "pull_request": None}]
        self.assertIsNone(check_module.exact_open_issue(issues, "sync-failed: v4.0.20"))

    def test_no_duplicate_dispatch_after_second_read(self):
        api = fixture_api(latest="v4.0.20", used=1, second_sync_active=True)
        result = check_module.check(api, datetime(2026, 10, 1, tzinfo=timezone.utc), 1600, rollout_enabled=True, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400, dry_run=False)
        self.assertNotEqual(result["action"], "sync")
        self.assertFalse(any(call[0] == "POST" and "dispatches" in call[1] for call in api.calls))

    def test_private_auth_fence_blocks_sync_but_not_tag_build(self):
        api = fixture_api(latest="v4.0.20", auth_fence={"schemaVersion": 1, "state": "pending", "transactionId": "a" * 32, "version": "v4.0.20"})
        self.assertNotEqual(check_module.check(api, datetime(2026, 10, 1, tzinfo=timezone.utc), 1600, rollout_enabled=True, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400)["action"], "sync")
        api = fixture_api(latest="v4.0.20", tag=True, auth_fence={"malformed": True})
        self.assertEqual(check_module.check(api, datetime(2026, 10, 1, tzinfo=timezone.utc), 1600, rollout_enabled=True)["action"], "build")

    def test_rollout_default_off_and_manual_dry_run(self):
        api = fixture_api(latest="v4.0.20", used=1)
        decision = check_module.check(api, datetime(2026, 10, 1, tzinfo=timezone.utc), 1600, rollout_enabled=False, zero_paid_confirmed=True, account_free_minutes=2000, account_reserve_minutes=400)
        self.assertEqual(decision["action"], "sync")
        self.assertTrue(api.calls)
        self.assertFalse(any(call[0] == "POST" and "dispatches" in call[1] for call in api.calls))


if __name__ == "__main__":
    unittest.main()
