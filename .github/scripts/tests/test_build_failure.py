import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/build_failure.py"
spec = importlib.util.spec_from_file_location("build_failure", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class FailureTests(unittest.TestCase):
    def test_failure_is_exact_idempotent_and_metadata_only(self):
        issues = [dict(title="build-failed: v4.0.200", state="open"),
                  dict(title="build-failed: v4.0.20", state="closed"),
                  dict(title="build-failed: v4.0.20", state="open", pull_request={})]
        writes = []
        def request(method, path, data=None):
            if path == "":
                return dict(private=True)
            if path.startswith("/issues?"):
                return issues
            if method == "POST" and path == "/issues":
                writes.append(data)
                issues.append(dict(data, state="open"))
                return dict(number=4)
            raise AssertionError((method, path))
        link = "https://github.com/NKU100/cc-switch-build/actions/runs/123/job/456"
        with patch.object(helper, "request", side_effect=request):
            helper.mark_failure("v4.0.20", [link])
            helper.mark_failure("v4.0.20", [link])
        self.assertEqual(writes, [dict(title="build-failed: v4.0.20", body=link)])

    def test_invalid_version_or_job_url_cannot_write(self):
        with patch.object(helper, "request") as request:
            for version in ("main", "v1.2.3;bad", "v1.2.3\n", "v1.2.3-beta", ""):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    helper.mark_failure(version, ["https://github.com/NKU100/cc-switch-build/actions/runs/1"])
            for url in ("https://evil.test/1", "http://github.com/NKU100/cc-switch-build/actions/runs/1", "https://github.com/NKU100/cc-switch-build/actions/runs/1?secret=x"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    helper.mark_failure("v1.2.3", [url])
            request.assert_not_called()

    def test_request_failure_does_not_print_body_token_url_or_headers(self):
        public = io.StringIO()
        with patch.dict(os.environ, dict(SRC_TOKEN="synthetic-canary", PACKAGE_VERSION="v4.0.20", FAILURE_JOB_URL="https://github.com/NKU100/cc-switch-build/actions/runs/1"), clear=True), patch.object(helper, "build_opener", side_effect=RuntimeError("private body synthetic-canary")), contextlib.redirect_stdout(public), contextlib.redirect_stderr(public):
            self.assertEqual(helper.main(["failure"]), 1)
        self.assertEqual(public.getvalue(), "")

    def test_validate_outputs_only_strict_version(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "metadata"
            with patch.dict(os.environ, dict(PACKAGE_VERSION="v4.0.20", GITHUB_OUTPUT=str(output)), clear=True):
                self.assertEqual(helper.main(["validate"]), 0)
            self.assertEqual(output.read_text(), "version=v4.0.20\n")

    def test_failed_job_metadata_pagination_contains_only_failed_links(self):
        first = [dict(id=i + 1, conclusion="success", html_url="https://irrelevant.test") for i in range(100)]
        second = [dict(id=101, conclusion="failure", html_url="private-body-fixture"),
                  dict(id=102, conclusion="skipped"), dict(id=103, conclusion="timed_out")]
        calls = []
        def http(method, url, token, data=None):
            calls.append((method, url, token))
            return dict(jobs=first if parse_qs(urlsplit(url).query)["page"] == ["1"] else second)
        with patch.dict(os.environ, {"PUBLIC_TOKEN": "fixture-public-token"}, clear=True), patch.object(helper, "_http", side_effect=http):
            links = helper.failed_jobs("123")
        self.assertEqual(links, ["https://github.com/NKU100/cc-switch-build/actions/runs/123/job/101", "https://github.com/NKU100/cc-switch-build/actions/runs/123/job/103"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0] == "GET" and call[1].startswith("https://api.github.com/repos/NKU100/cc-switch-build/") for call in calls))

    def test_failure_issue_pagination_checks_exact_title_on_later_page(self):
        writes = []
        def request(method, path, data=None):
            if path == "":
                return dict(private=True)
            if method == "POST":
                writes.append(data)
                return {}
            if parse_qs(urlsplit(path).query)["page"] == ["1"]:
                return [dict(title="irrelevant", state="open") for _ in range(100)]
            return [dict(title="build-failed: v4.0.20", state="open")]
        with patch.object(helper, "request", side_effect=request):
            helper.mark_failure("v4.0.20", ["https://github.com/NKU100/cc-switch-build/actions/runs/123/job/456"])
        self.assertEqual(writes, [])

    def test_failure_before_private_checkout_marks_issue_without_package_scripts(self):
        writes = []
        def request(method, path, data=None):
            if path == "":
                return dict(private=True)
            if method == "GET":
                return []
            writes.append(data)
            return dict(number=1)
        with patch.dict(os.environ, dict(PACKAGE_VERSION="v4.0.20", PUBLIC_RUN_ID="123"), clear=True), patch.object(helper, "_http", return_value=dict(jobs=[dict(id=1, conclusion="failure")])), patch.object(helper, "request", side_effect=request):
            self.assertEqual(helper.main(["failure"]), 0)
        self.assertEqual(writes, [dict(title="build-failed: v4.0.20", body="https://github.com/NKU100/cc-switch-build/actions/runs/123/job/1")])


class WorkflowTests(unittest.TestCase):
    def workflow(self):
        path = ROOT / ".github/workflows/build.yml"
        try:
            import yaml
        except ImportError:
            # macOS provides Psych; do not install dependencies or contact a registry.
            return json.loads(subprocess.check_output(["ruby", "-ryaml", "-rjson", "-e",
                                                       "puts JSON.generate(YAML.load_file(ARGV[0]))", str(path)]))
        return yaml.safe_load(path.read_text())

    def test_run_name_matches_private_dispatch_recovery_identity(self):
        self.assertEqual(self.workflow()["run-name"], "private-build ${{ inputs.version }}")

    def test_five_platform_dependency_and_public_privacy_contract(self):
        flow = self.workflow()
        jobs = flow["jobs"]
        platforms = {"windows-x64": "windows-2022", "windows-arm64": "windows-11-arm",
                     "linux-x64": "ubuntu-22.04", "linux-arm64": "ubuntu-22.04-arm", "macos-universal": "macos-14"}
        self.assertEqual(flow["permissions"], {"contents": "read"})
        self.assertEqual(flow["concurrency"], {"group": "build-${{ inputs.version }}", "cancel-in-progress": False})
        self.assertEqual(jobs["publish"]["concurrency"], {"group": "private-release-publication", "cancel-in-progress": False, "queue": "max"})
        for platform, runner in platforms.items():
            job = jobs[platform]
            with self.subTest(platform=platform):
                self.assertEqual(job["runs-on"], runner)
            self.assertEqual(job["needs"], ["prepare"])
            self.assertIn("skip-build", job["if"])
            checkout = job["steps"][0]
            self.assertEqual(checkout["with"]["repository"], "NKU100/cc-switch-src")
            self.assertIs(checkout["with"]["persist-credentials"], False)
            self.assertNotIn("path", checkout["with"])
            action = job["steps"][1]
            self.assertEqual(action["uses"], "./.github/actions/package")
            self.assertEqual(action["with"]["platform"], platform)
        self.assertEqual(set(jobs["publish"]["needs"]), set(platforms) | {"prepare"})
        self.assertIn("always()", jobs["publish"]["if"])
        self.assertIn("skip-build", jobs["publish"]["if"])
        self.assertIn("always()", jobs["failure"]["if"])
        self.assertEqual(set(jobs["failure"]["needs"]), set(platforms) | {"prepare", "publish"})
        for name, job in jobs.items():
            for step in job["steps"]:
                uses = step.get("uses", "")
                self.assertFalse("artifact" in uses.lower() or "cache" in uses.lower())
                if name in platforms:
                    self.assertNotIn("run", step)
        prepare = jobs["prepare"]
        self.assertIn("version", prepare["outputs"])
        self.assertIn("release-id", prepare["outputs"])
        self.assertIn("skip-build", prepare["outputs"])

    def test_global_job_groups_retain_bounded_pending_queue(self):
        flow = self.workflow()
        self.assertEqual(flow["concurrency"], {"group": "build-${{ inputs.version }}", "cancel-in-progress": False})
        for job, group in (("publish", "private-release-publication"), ("failure", "private-build-failure-marker")):
            with self.subTest(job=job):
                self.assertEqual(flow["jobs"][job]["concurrency"], {"group": group, "cancel-in-progress": False, "queue": "max"})

    def test_keepalive_executes_against_public_checkout(self):
        step = next(s for s in self.workflow()["jobs"]["publish"]["steps"] if s.get("id") == "keepalive")
        self.assertEqual(step["working-directory"], "public-control")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote, public, private = root / "remote.git", root / "public", root / "private"
            def git(*args, cwd=None):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
            git("init", "--bare", "--initial-branch=main", str(remote))
            git("clone", str(remote), str(public))
            git("config", "user.name", "Fixture", cwd=public)
            git("config", "user.email", "fixture@example.test", cwd=public)
            (public / "control.txt").write_text("public fixture")
            git("add", "control.txt", cwd=public)
            git("commit", "-m", "Fixture", cwd=public)
            git("push", "origin", "main", cwd=public)
            git("init", str(private))
            (private / "source-canary.txt").write_text("private fixture")
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=public,
                                    env=dict(os.environ, BUILD_VERSION="v4.0.20", PUBLIC_DEFAULT_BRANCH="main"), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(git("show", "main:last-built-version.txt", cwd=remote).strip(), "v4.0.20")
            self.assertEqual(set(git("ls-tree", "--name-only", "main", cwd=remote).splitlines()), {"control.txt", "last-built-version.txt"})
            self.assertEqual((private / "source-canary.txt").read_text(), "private fixture")


if __name__ == "__main__":
    unittest.main()
