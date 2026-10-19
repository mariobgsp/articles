"""The only module in the package that speaks HTTP.

Invariant: the single write in the whole pipeline is a POST/PATCH against the issues API on
the drift issue, and it happens only when the report's content hash differs from the hash
already written into the issue body. There is no second writer and no state outside that
body.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .drift import DriftReport
from .report import MARKER, merge_check_run

TITLE_PREFIX = "Article drift:"
DEFAULT_API_BASE = "https://api.github.com"
ALLOWED_SCHEMES = frozenset({"https", "http"})
CLEARTEXT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
LABEL = "articles-drift"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 20
MAX_ERROR_CHARS = 300


class GitHubError(RuntimeError):
    """An API call failed, with the method, the URL and the response body."""


def _as_int(value: object) -> int:
    """The API's number field, coerced without trusting the JSON type it arrives in."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _entry(payload: object) -> Mapping[str, object]:
    """One JSON object from the API, or a loud failure if the payload is not one."""
    if isinstance(payload, Mapping):
        return payload
    raise GitHubError(f"expected a JSON object from the issues API, got {type(payload).__name__}")


def _issue(entry: Mapping[str, object]) -> Issue:
    state = "closed" if entry.get("state") == "closed" else "open"
    return Issue(
        number=_as_int(entry.get("number")),
        state=state,
        title=str(entry.get("title") or ""),
        body=str(entry.get("body") or ""),
    )


@dataclass(frozen=True)
class Issue:
    """One issue as the API reports it, reduced to what the upsert needs."""

    number: int
    state: Literal["open", "closed"]
    title: str
    body: str


@dataclass(frozen=True)
class UpsertOutcome:
    """What the run actually did to the issue, so a log says it without reading the diff."""

    action: Literal["skip", "update", "open", "reopen", "close"]
    issue_number: int | None
    detail: str


class GitHubAPI:
    """The issue API over stdlib `urllib.request`.

    Invariant: 5xx responses are retried twice, a rate-limited 403 fails immediately with
    the reset time instead of looping, and every other status raises with the body so a
    failure is never mistaken for "nothing changed".
    """

    def __init__(self, token: str, base: str | None = None) -> None:
        """Hold the credential and pin the transport to a scheme this program intended.

        Invariant: `file:` and every other custom scheme is refused before any request, so a
        base URL out of the environment cannot turn the one HTTP path into a file read. Plain
        http is allowed only for a loopback host, which is the selftest's own fixture server.
        """
        self.token = token
        self.base = (base or DEFAULT_API_BASE).rstrip("/")
        split = urllib.parse.urlsplit(self.base)
        if split.scheme not in ALLOWED_SCHEMES or (split.scheme == "http" and split.hostname not in CLEARTEXT_HOSTS):
            raise GitHubError(f"refusing API base {self.base!r}: only https, or http on a loopback host, is allowed")

    def list_issues(self, repo: str) -> tuple[Issue, ...]:
        """Every issue in the repository, open and closed, with no cap on the page count.

        Invariant: pull requests are filtered out by their `pull_request` key, so a PR that
        quotes the marker cannot be mistaken for the drift issue.
        """
        issues: list[Issue] = []
        page = 1
        while True:
            payload = self._request("GET", f"/repos/{repo}/issues?state=all&per_page=100&page={page}")
            if not isinstance(payload, list) or not payload:
                break
            for entry in payload:
                if "pull_request" in entry:
                    continue
                issues.append(_issue(entry))
            page += 1
        return tuple(issues)

    def create_issue(self, repo: str, title: str, body: str, labels: Sequence[str]) -> Issue:
        payload = self._request(
            "POST",
            f"/repos/{repo}/issues",
            {"title": title, "body": body, "labels": list(labels)},
        )
        return _issue(_entry(payload))

    def update_issue(
        self,
        repo: str,
        number: int,
        *,
        body: str | None = None,
        state: Literal["open", "closed"] | None = None,
        title: str | None = None,
    ) -> Issue:
        payload: dict[str, str] = {}
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if title is not None:
            payload["title"] = title
        return _issue(_entry(self._request("PATCH", f"/repos/{repo}/issues/{number}", payload)))

    def create_comment(self, repo: str, number: int, body: str) -> None:
        self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def _request(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> object:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
        # The scheme is checked in __init__: https, or http on a loopback host.
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", "articles-ci")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        last: Exception | None = None
        for attempt in range(3):
            try:
                # The base URL passed the scheme check in __init__ (https, or http on loopback).
                # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body.strip() else {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:MAX_ERROR_CHARS]
                if error.code == 403 and error.headers.get("X-RateLimit-Remaining") == "0":
                    raise GitHubError(
                        f"{method} {url} rate limited until {error.headers.get('X-RateLimit-Reset')}"
                    ) from error
                if 500 <= error.code < 600 and attempt < 2:
                    last = GitHubError(f"{method} {url} -> {error.code}: {detail}")
                    continue
                raise GitHubError(f"{method} {url} -> {error.code}: {detail}") from error
            except urllib.error.URLError as error:
                if attempt < 2:
                    last = GitHubError(f"{method} {url} failed: {error.reason}")
                    continue
                raise GitHubError(f"{method} {url} failed: {error.reason}") from error
        raise last or GitHubError(f"{method} {url} failed")


def find_marker_issue(api: GitHubAPI, repo: str, marker: str) -> tuple[Issue | None, tuple[str, ...]]:
    """The lowest-numbered issue carrying the marker, plus a complaint per extra match.

    Invariant: the search covers closed issues too - a closed marker issue is reopened rather
    than duplicated - and a hand-copied duplicate is surfaced instead of silently ignored.
    """
    matches = [issue for issue in api.list_issues(repo) if marker in issue.body]
    matches.sort(key=lambda issue: issue.number)
    if not matches:
        return None, ()
    complaints = tuple(
        f"issue #{issue.number} also carries the drift marker; only #{matches[0].number} is updated"
        for issue in matches[1:]
    )
    return matches[0], complaints


def upsert(api: GitHubAPI, repo: str, report: DriftReport) -> UpsertOutcome:
    """Exactly one drift issue, forever.

    Invariant: drift and no marker issue creates one; drift or an unchecked article keeps the
    issue open by patching its body, and only when `content_hash` changed; everything current
    comments once and closes. Two runs in a row therefore perform at most one write in total,
    and the issue is open iff something needs a human.
    """
    issue, complaints = find_marker_issue(api, repo, MARKER)
    decision = merge_check_run(issue.body if issue else "", report)
    title = report_title(report)
    note = f"; {len(complaints)} duplicate marker issue(s): {complaints[0]}" if complaints else ""
    if decision.action == "skip":
        return UpsertOutcome("skip", issue.number if issue else None, f"content unchanged, no request{note}")

    if decision.action == "open" or issue is None:
        created = api.create_issue(repo, title, decision.body, (LABEL,))
        return UpsertOutcome("open", created.number, f"opened issue #{created.number}{note}")

    if decision.action == "update":
        if issue.state == "closed":
            api.update_issue(repo, issue.number, body=decision.body, state="open", title=title)
            return UpsertOutcome("reopen", issue.number, f"reopened issue #{issue.number}{note}")
        api.update_issue(repo, issue.number, body=decision.body, title=title)
        return UpsertOutcome("update", issue.number, f"updated body of issue #{issue.number}{note}")

    api.update_issue(repo, issue.number, body=decision.body, state="closed", title=title)
    api.create_comment(
        repo,
        issue.number,
        f"All {report.totals['articles']} pins are current as of {report.generated_at} ({report.content_hash}).",
    )
    return UpsertOutcome("close", issue.number, f"closed issue #{issue.number}: every pin is current{note}")


def report_title(report: DriftReport) -> str:
    """The one issue title, which always states how many upstreams moved.

    Invariant: the title and the body's first heading are built from the same two counts, so
    they cannot disagree about which run this is.
    """
    return (
        f"{TITLE_PREFIX} {report.totals['drifted']} of {report.totals['articles']} upstreams "
        "moved past their recorded pins"
    )


