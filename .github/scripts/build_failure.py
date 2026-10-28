"""Public control metadata only: input validation and a private failure marker."""

import argparse
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Metadata redirect refused")


def _http(method, url, token, data=None):
    try:
        if not token:
            raise ValueError
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "build-control"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                      headers=headers, method=method)
        with build_opener(NoRedirect()).open(req, timeout=60) as response:
            return json.loads(response.read())
    except HTTPError as error:
        error.close()
        raise RuntimeError("Build metadata request failed") from None
    except Exception:
        raise RuntimeError("Build metadata request failed") from None


def request(method, path, data=None):
    return _http(method, "https://api.github.com/repos/NKU100/cc-switch-src" + path,
                 os.environ.get("SRC_TOKEN", ""), data)


def validate_version(version):
    if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid build version")


def mark_failure(version, links):
    validate_version(version)
    if not links or any(not re.fullmatch(r"https://github\.com/NKU100/cc-switch-build/actions/runs/[0-9]+(?:/job/[0-9]+)?", link) for link in links):
        raise ValueError("Invalid public failure job URL")
    if request("GET", "").get("private") is not True:
        raise RuntimeError("Failure destination must be private")
    title = "build-failed: " + version
    for page in range(1, 1001):
        issues = request("GET", "/issues?state=open&per_page=100&page=" + str(page))
        if not isinstance(issues, list):
            raise RuntimeError("Invalid issue metadata")
        if any(i.get("title") == title and i.get("state") == "open" and "pull_request" not in i for i in issues):
            return
        if len(issues) < 100:
            request("POST", "/issues", dict(title=title, body="\n".join(links)))
            return
    raise RuntimeError("Issue pagination incomplete")


def failed_jobs(run_id):
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("Invalid public run ID")
    links = []
    for page in range(1, 1001):
        result = _http("GET", "https://api.github.com/repos/NKU100/cc-switch-build/actions/runs/" + run_id + "/jobs?filter=latest&per_page=100&page=" + str(page), os.environ.get("PUBLIC_TOKEN", ""))
        jobs = result.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("Invalid public jobs metadata")
        for job in jobs:
            if job.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
                job_id = job.get("id")
                if type(job_id) is not int or job_id <= 0:
                    raise ValueError("Invalid failed job ID")
                links.append("https://github.com/NKU100/cc-switch-build/actions/runs/" + run_id + "/job/" + str(job_id))
        if len(jobs) < 100:
            if not links:
                raise ValueError("No failed jobs confirmed")
            return links
    raise ValueError("Public job pagination incomplete")


def main(argv=None):
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("mode", choices=("validate", "failure"))
        args = parser.parse_args(argv)
        version = os.environ.get("PACKAGE_VERSION", "")
        validate_version(version)
        if args.mode == "validate":
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write("version=" + version + "\n")
        else:
            link = os.environ.get("FAILURE_JOB_URL")
            mark_failure(version, [link] if link else failed_jobs(os.environ.get("PUBLIC_RUN_ID", "")))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
