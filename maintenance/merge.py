#!/usr/bin/env python3
"""
Merge the dependency updates that may merge themselves, so that a person
sees only those that need one.

    maintenance/merge.py --repository OWNER/REPO [--dry-run]

Its own workflow (.github/workflows/dependencies.yml) runs it after every
CI run and once a day. Each open pull request from Dependabot, or from the
vendor binaries' workflow (a pins/ branch), is:

- merged, by rebase, when its CI run passed on its newest commit, it is up
  to date with main, and every update in it is a patch or a minor one (or
  a newer dated build of the same image, such as Ubuntu's
  resolute-20261024.1);
- asked to rebase ("@dependabot rebase") when main has moved on since;
- given its pin-age check again when that was all that failed, a day
  later: a pin a few days too young becomes old enough;
- left for a person otherwise: a major update, an action the workflows use
  (each is new code that runs with their tokens), and anything that failed.

Standard library only; GITHUB_TOKEN is the token it acts with.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

API = "https://api.github.com"

# The CI workflow, and its job that ages a pull request's pins.
CI_WORKFLOW = "ci.yml"
PIN_AGES_JOB = "Pin ages"

# How long a failed pin-age check waits before it is run again.
RETRY_AFTER = timedelta(hours=20)

# What Dependabot says to a pull request it should rebase.
REBASE = "@dependabot rebase"

# Who may have made a dependency update's commits: one with anyone else's is
# a person's change, and waits for a person.
MOVERS = {"dependabot[bot]", "github-actions[bot]"}

# Dependabot's metadata in its commit message: an update-type per update.
UPDATE_TYPE = re.compile(r"^\s*update-type:\s*(\S+)\s*$", re.MULTILINE)
VERSION = re.compile(r"^\s*dependency-version:\s*(\S+)\s*$", re.MULTILINE)

# A dated build, such as Ubuntu's resolute-20261024.1: a newer one of the
# same release looks like a major update to Dependabot, and isn't.
DATED = re.compile(r"(?:^|-)20\d{6}(?:\.\d+)?$")


class GitHubError(Exception):
    """
    GitHub refused a request; the message says which and why.
    """


Request = Callable[[str, str, Optional[dict]], Any]


def github_request(token: str) -> Request:
    """
    A function that sends one request to GitHub's API with *token*: the
    method, the path, and a JSON body or None; it returns the JSON answer.
    """

    def send(method: str, path: str, body: Optional[dict]) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{API}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "hardcode-maintenance",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise GitHubError(f"{method} {path}: {error.code} {error.read().decode('utf-8', 'replace')[:300]}") from error

        return json.loads(text) if text else None

    return send


@dataclass
class Decision:
    """
    What is done with one pull request, and why.
    """

    number: int
    action: str
    reason: str


def _when(text: str) -> datetime:
    """
    An ISO 8601 time, as GitHub writes it.
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def is_update(pull: dict) -> bool:
    """
    Whether *pull* is a dependency update: Dependabot's, or the vendor
    binaries' workflow's (its pins/ branch, in this repository).
    """
    head = pull["head"]
    same_repository = (head.get("repo") or {}).get("full_name") == pull["base"]["repo"]["full_name"]

    if pull["user"]["login"] == "dependabot[bot]":
        return True

    return same_repository and head["ref"].startswith("pins/")


def needs_a_person(pull: dict, commits: list[dict]) -> str:
    """
    Why *pull* must wait for a person, or "" when it may merge itself.
    *commits* are its commits, as GitHub lists them: the updates' metadata
    is in their messages.
    """
    if pull["head"]["ref"].startswith("dependabot/github_actions/"):
        return "it updates actions the workflows run, with their tokens"

    authors = {(commit.get("author") or {}).get("login") for commit in commits}
    if not authors <= MOVERS:
        return f"it has commits from {', '.join(sorted(str(author) for author in authors - MOVERS))}"

    text = "\n".join(commit["commit"]["message"] for commit in commits)
    types = UPDATE_TYPE.findall(text)
    versions = VERSION.findall(text)
    if not types:
        return "its commits don't say what kind of update it is"

    for index, kind in enumerate(types):
        version = versions[index] if index < len(versions) else ""

        if kind == "version-update:semver-major" and not DATED.search(version):
            return f"a major update ({version or 'version not given'})"
        if kind not in ("version-update:semver-major", "version-update:semver-minor", "version-update:semver-patch"):
            return f"an update of a kind it doesn't know: {kind}"

    return ""


class Merger:
    """
    Goes through the open pull requests of one repository, and merges,
    rebases or retries each dependency update as the module says.
    """

    def __init__(self, repository: str, request: Request, now: Optional[datetime] = None):
        self.repository = repository
        self.request = request
        self.now = now or datetime.now(timezone.utc)

    def get(self, path: str) -> Any:
        """
        A GET of *path* under the repository.
        """
        return self.request("GET", f"/repos/{self.repository}{path}", None)

    def run(self) -> list[Decision]:
        """
        Decide, and act on, every open dependency update.
        """
        decisions = []

        for pull in self.get("/pulls?state=open&per_page=100"):
            if not is_update(pull):
                continue

            # One refusal (a conflict, say) is that pull request's, not the run's.
            try:
                decisions.append(self.decide(pull))
            except GitHubError as error:
                decisions.append(Decision(pull["number"], "left", f"GitHub refused: {error}"))

        return decisions

    def decide(self, pull: dict) -> Decision:
        """
        Act on one pull request, and say what was done.
        """
        number = pull["number"]
        head = pull["head"]["sha"]

        commits = self.get(f"/pulls/{number}/commits?per_page=100")
        reason = needs_a_person(pull, commits)
        if reason:
            return Decision(number, "left", reason)

        # Up to date with main: what CI ran is what would land.
        compared = self.get(f"/compare/{pull['base']['ref']}...{head}")
        if compared.get("behind_by", 0) > 0:
            return self.rebase(pull, _when(commits[-1]["commit"]["committer"]["date"]))

        runs = self.get(f"/actions/workflows/{CI_WORKFLOW}/runs?head_sha={head}&per_page=20")["workflow_runs"]
        if not runs:
            return Decision(number, "waiting", "CI hasn't run on its newest commit")

        run = max(runs, key=lambda item: (item["created_at"], item.get("run_attempt", 1)))
        if run["status"] != "completed":
            return Decision(number, "waiting", "CI is running")

        if run["conclusion"] == "success":
            self.request(
                "PUT",
                f"/repos/{self.repository}/pulls/{number}/merge",
                {"merge_method": "rebase", "sha": head},
            )
            return Decision(number, "merged", "CI passed, and it is a patch or minor update")

        return self.retry_or_leave(number, run)

    def rebase(self, pull: dict, pushed: datetime) -> Decision:
        """
        Ask Dependabot to rebase *pull* onto main, once for each newest
        commit (*pushed* is when it was made). The vendor binaries' workflow
        makes its branch from main afresh each week, and is left to.
        """
        number = pull["number"]
        if pull["user"]["login"] != "dependabot[bot]":
            return Decision(number, "waiting", "behind main; its workflow makes it afresh")

        comments = self.get(f"/issues/{number}/comments?per_page=100")
        asked = any(comment["body"].strip() == REBASE and _when(comment["created_at"]) >= pushed for comment in comments)
        if asked:
            return Decision(number, "waiting", "behind main; Dependabot was asked to rebase it")

        self.request("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": REBASE})
        return Decision(number, "rebasing", "behind main: Dependabot asked to rebase it")

    def retry_or_leave(self, number: int, run: dict) -> Decision:
        """
        Run a failed pin-age check again once it is a day old, when that is
        the only job that failed; leave anything else for a person.
        """
        jobs = self.get(f"/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        failed = sorted({job["name"] for job in jobs if job["conclusion"] not in ("success", "skipped")})

        if failed != [PIN_AGES_JOB]:
            return Decision(number, "left", f"CI failed: {', '.join(failed) or run['conclusion']}")

        finished = _when(run["updated_at"])
        if self.now - finished < RETRY_AFTER:
            return Decision(number, "waiting", "a pin is too young; its age is checked again a day later")

        self.request("POST", f"/repos/{self.repository}/actions/runs/{run['id']}/rerun-failed-jobs", None)
        return Decision(number, "retried", "the pin-age check runs again")


def main(argv: list[str]) -> int:
    """
    The command line; see the module's docstring.
    """
    parser = argparse.ArgumentParser(prog="merge.py", description="Merge the dependency updates that may.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--dry-run", action="store_true", help="say what would be done, and do none of it")
    arguments = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("merge.py: GITHUB_TOKEN isn't set", file=sys.stderr)
        return 1

    send = github_request(token)

    def request(method: str, path: str, body: Optional[dict]) -> Any:
        # A dry run reads, and changes nothing.
        if arguments.dry_run and method != "GET":
            print(f"(dry run: {method} {path})")
            return None

        return send(method, path, body)

    merger = Merger(arguments.repository, request)

    try:
        decisions = merger.run()
    except GitHubError as error:
        print(f"merge.py: {error}", file=sys.stderr)
        return 1

    for decision in decisions:
        print(f"#{decision.number}: {decision.action}, {decision.reason}")
    if not decisions:
        print("no dependency updates are open")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
