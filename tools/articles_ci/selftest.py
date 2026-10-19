"""The offline proof: a throwaway world where both halves run for real.

Invariant: the fixture changes URLs, never code paths. `ARTICLES_CI_GIT_BASE` points at a
local bare repository and `ARTICLES_CI_API_BASE` at a loopback `http.server`, so the
production pipeline runs end to end - including the issue write - with no external host, no
token of value, and no test-only branch anywhere in the package.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .anchor import looks_like_path
from .drift import DriftReport, healthy, unhealthy
from .drift import run as drift_run
from .git import run_git
from .parse import Article, discover, parse_refs, parse_sections
from .report import MAX_COMMITS_IN_BODY, to_json, to_markdown
from .validate import check_markdownlint, image_path, validate_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT_IDENTITY = ("articles-ci selftest", "selftest@articles-ci.invalid")
CLI_REPO = "mariobgsp/articles"
UNKNOWN_PIN = "deadbee"
SELFTEST_TOKEN = os.environ.get("ARTICLES_CI_SELFTEST_TOKEN") or "selftest-token"  # loopback fixture only
NUMBERED_MENTION_RE = re.compile(r"^[A-Za-z0-9_./-]+:\d+(?:-\d+)?$")
ADDED_CODE_DIR = "ci-fixture"
SCENARIOS = ("current", "drifted", "unreachable", "malformed")


@dataclass(frozen=True)
class Fixture:
    """A throwaway world: local bare upstreams and copies of the five real articles.

    Invariant: everything lives under `root` (a temporary directory), so a selftest run
    never writes inside the real repository and never leaves a mirror behind. The loopback
    API base is not carried here: each case starts its own server and hands the base to
    the environment, because the case also needs the server's request log.
    """

    root: Path
    git_base: str
    articles_dir: Path
    upstream_tips: Mapping[str, str]
    pins: Mapping[str, str]


@dataclass(frozen=True)
class FakeGitHub:
    """The loopback GitHub API's memory.

    Invariant: `requests` records every method, target and body, which is how "no request
    left 127.0.0.1" is proved rather than assumed.
    """

    issues: list[dict[str, Any]]
    comments: list[str]
    requests: list[dict[str, str | None]]
    writes: int


class _LoopbackServer(ThreadingHTTPServer):
    """A loopback server that never holds the run open and never prints a client hangup.

    Invariant: threads are daemons and connection resets from the client are ignored, so a
    selftest run cannot be kept alive by a socket the checks already finished with.
    """

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not issubclass(sys.exc_info()[0] or Exception, (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


@dataclass(frozen=True)
class Case:
    """One named proof, with the assertion message a failure prints."""

    name: str
    run: Callable[[], str]


def build_fixture(root: Path, *, scenario: str) -> Fixture:
    """Build one throwaway world.

    Scenarios: "current" (every pin at its tip), "drifted" (one upstream extended, one with
    a rewritten history, one whose pin was deleted), "unreachable" (one remote is a
    directory that is not a repository), "malformed" (one closing pin loses its sha).
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    articles_dir = root / "articles"
    articles_dir.mkdir(parents=True)
    _copy_skeleton(articles_dir)
    inventory = discover(articles_dir)
    if not inventory.articles:
        raise RuntimeError("the fixture copied no articles; run the selftest from the repository root")
    _materialise_images(articles_dir, inventory.articles)

    git_base = root / "git"
    ordered = sorted(inventory.articles, key=lambda item: item.path)
    drifted, diverged, unknown = _scenario_roles(ordered)
    tips: dict[str, str] = {}
    pins: dict[str, str] = {}
    for index, article in enumerate(ordered):
        branch = "master" if index == 0 else "main"
        role = {drifted: "drifted", diverged: "diverged", unknown: "pin-unknown"}.get(index, "current")
        if scenario == "unreachable" and index == 0:
            # The unreachable remote is one that was never built: a path that does not exist is
            # exactly how an unreachable upstream looks from git, and nothing ever resolves its
            # pin, so the short stand-in sha can never be compared with a real tip.
            tips[article.slug] = UNKNOWN_PIN
            pins[article.slug] = UNKNOWN_PIN
            continue
        tip, pin = _build_upstream(git_base, article, branch, scenario=scenario, role=role)
        tips[article.slug] = tip
        pins[article.slug] = pin
    _rewrite_pins(articles_dir, ordered, pins)
    if scenario == "malformed":
        _break_pin(articles_dir, ordered[0])
    _init_article_repo(articles_dir)

    return Fixture(
        root=root,
        git_base=str(git_base),
        articles_dir=articles_dir,
        upstream_tips=tips,
        pins=pins,
    )


def start_fake_api(state: dict[str, Any]) -> tuple[str, Callable[[], None]]:
    """A loopback GitHub API with the four endpoints `issues.py` uses.

    Invariant: it records every request target and body, and keeps issue state in memory,
    so idempotency is a write count and offline-ness is a log, not an inference.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class names it `format`
            return

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data: dict[str, Any] = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise AssertionError(f"the fixture server read no JSON body: {error}") from error
            return data

        def _send(self, status: int, body: object) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _log(self, method: str, payload: dict[str, Any] | None) -> None:
            state["requests"].append(
                {"method": method, "target": self.path, "body": json.dumps(payload) if payload else None}
            )

        def do_GET(self) -> None:
            self._log("GET", None)
            match = re.match(r"^/repos/[^/]+/[^/]+/issues(?:\?(?P<query>.*))?$", self.path)
            if match:
                query = urllib.parse.parse_qs(match.group("query") or "")
                page = int(query.get("page", ["1"])[0])
                per_page = int(query.get("per_page", ["30"])[0])
                issues: list[dict[str, Any]] = state["issues"]
                start = (page - 1) * per_page
                self._send(200, issues[start : start + per_page])
                return
            self._send(404, {"message": "not found"})

        def do_POST(self) -> None:
            payload = self._payload()
            self._log("POST", payload)
            issues: list[dict[str, Any]] = state["issues"]
            if re.match(r"^/repos/[^/]+/[^/]+/issues$", self.path):
                issue = {
                    "number": len(issues) + 1,
                    "state": "open",
                    "title": payload.get("title", ""),
                    "body": payload.get("body", ""),
                }
                issues.append(issue)
                state["writes"] += 1
                self._send(201, issue)
                return
            if re.match(r"^/repos/[^/]+/[^/]+/issues/\d+/comments$", self.path):
                state["comments"].append(str(payload.get("body", "")))
                state["writes"] += 1
                self._send(201, {"id": len(state["comments"])})
                return
            self._send(404, {"message": "not found"})

        def do_PATCH(self) -> None:
            payload = self._payload()
            self._log("PATCH", payload)
            match = re.match(r"^/repos/[^/]+/[^/]+/issues/(?P<number>\d+)$", self.path)
            issues: list[dict[str, Any]] = state["issues"]
            if match:
                for issue in issues:
                    if issue["number"] == int(match.group("number")):
                        issue.update(payload)
                        state["writes"] += 1
                        self._send(200, issue)
                        return
            self._send(404, {"message": "not found"})

    server = _LoopbackServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    def shutdown() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return f"http://{host}:{port}", shutdown


def cases() -> tuple[Case, ...]:
    """The proofs: both halves end to end, the status ladder, the body's cap, the publish
    transitions, the hash's insensitivity to time, and the linter when it is installed."""
    return (
        Case("validate: the fixture article tree passes the offline checks", _case_validate_clean),
        Case("validate: a missing render and a broken anchor are reported", _case_validate_broken),
        Case("validate: a documented npm script package.json lacks is reported", _case_readme_command),
        Case("drift: every pin at its tip is healthy and the hash ignores the clock", _case_drift_current),
        Case("drift: the status ladder, the conflicts and the stale claims", _case_drift_ladder),
        Case("report: the body caps the commit table and the JSON keeps every commit", _case_report_cap),
        Case("publish: one issue, one write, then no writes at all", _case_publish_idempotent),
        Case("publish: a healthy run comments once and closes the issue", _case_publish_closes),
        Case("publish: an unreachable upstream keeps the issue open and the run red", _case_publish_unhealthy),
        Case("selftest: no request leaves the loopback API", _case_no_external_target),
        Case("markdownlint: the real linter finds the injected MD032", _case_markdownlint),
    )


def main() -> int:
    """Run every case and return 0 only if all of them passed.

    Invariant: a case that cannot run (the linter is not installed here) is reported as a
    skip and counted apart from the proofs, so a no-op can never be read as a pass.
    """
    failed = 0
    skipped = 0
    total = 0
    for case in cases():
        total += 1
        try:
            detail = case.run()
        except AssertionError as error:
            failed += 1
            print(f"FAIL {case.name}\n     {error}")
        except Exception as error:  # noqa: BLE001 - a fixture bug is a failed proof
            failed += 1
            print(f"ERROR {case.name}\n      {type(error).__name__}: {error}")
        else:
            if detail.startswith("skip:"):
                skipped += 1
                print(f"skip {case.name}\n     {detail.removeprefix('skip: ')}")
            else:
                print(f"ok   {case.name}\n     {detail}")
    ran = total - skipped
    tail = f", {skipped} skipped" if skipped else ""
    print(f"\n{ran - failed}/{ran} selftest cases passed{tail}")
    return 1 if failed else 0


def _case_validate_clean() -> str:
    with _fixture("current") as fixture:
        problems = validate_repo(fixture.articles_dir)
        offline = [problem for problem in problems if problem.code != "markdownlint"]
        _check(not offline, f"the offline checks reported: {[str(problem) for problem in offline]}")
        articles = len(discover(fixture.articles_dir).articles)
        return f"0 problems from the offline checks over {articles} articles"


def _case_validate_broken() -> str:
    with _fixture("current") as fixture:
        inventory = discover(fixture.articles_dir)
        article = sorted(inventory.articles, key=lambda item: item.path)[0]
        target = f"images/{article.slug}-architecture.png"
        (fixture.articles_dir / target).unlink()
        run_git(["git", "rm", "-q", "--cached", target], cwd=fixture.articles_dir)
        anchor_ref = next(
            (ref for entry in inventory.articles for ref in entry.local_refs if ref.target.startswith("#")),
            None,
        )
        if anchor_ref is None:
            raise AssertionError("the fixture articles carry no #anchor to break")
        file = fixture.articles_dir / anchor_ref_owner(inventory.articles, anchor_ref)
        file.write_text(
            file.read_text(encoding="utf-8").replace(anchor_ref.target, "#no-such-heading"),
            encoding="utf-8",
        )
        problems = validate_repo(fixture.articles_dir)
        codes = {problem.code for problem in problems}
        _check("diagram" in codes, f"a deleted render was not reported; codes={sorted(codes)}")
        _check("local-ref" in codes, f"an untracked image was not reported; codes={sorted(codes)}")
        _check("local-anchor" in codes, f"a broken anchor was not reported; codes={sorted(codes)}")
        return f"reported {sorted(codes)}"


def _case_readme_command() -> str:
    with _fixture("current") as fixture:
        manifest = fixture.articles_dir / "package.json"
        try:
            scripts = json.loads(manifest.read_text(encoding="utf-8"))["scripts"]
        except (OSError, ValueError, KeyError) as error:  # a fixture bug is a failed proof
            raise AssertionError(f"the fixture package.json is unreadable: {error}") from error
        del scripts["capture:routre"]
        manifest.write_text(json.dumps({"scripts": scripts}, indent=2), encoding="utf-8")
        codes = {problem.code for problem in validate_repo(fixture.articles_dir)}
        _check(
            "readme-command" in codes,
            f"a documented `npm run capture:routre` that package.json no longer defines was not "
            f"reported; codes={sorted(codes)}",
        )
        return "reported a documented npm script that package.json does not define"


def _case_drift_current() -> str:
    with _fixture("current") as fixture, _environment(fixture):
        first = drift_run(fixture.articles_dir)
        second = drift_run(fixture.articles_dir)
        _check(first.totals["up-to-date"] == 5, f"expected 5 current pins, got {dict(first.totals)}")
        _check(healthy(first) and not unhealthy(first), f"a fully current run was not healthy: {dict(first.totals)}")
        _check(
            first.content_hash == second.content_hash,
            f"the content hash moved between runs: {first.content_hash} != {second.content_hash}",
        )
        return f"5 current pins, content {first.content_hash} on both runs"


def _case_drift_ladder() -> str:
    with _fixture("drifted") as fixture, _environment(fixture):
        report = drift_run(fixture.articles_dir)
        statuses = {row.status for row in report.articles}
        _check(
            statuses == {"drifted", "diverged", "pin-unknown", "up-to-date"},
            f"expected the four reachable statuses, got {sorted(statuses)}",
        )
        row = _row(report, "drifted")
        _check(row.ahead_by >= 20, f"the extended upstream reported only {row.ahead_by} commits")
        _check(row.review_required, "an unclear commit did not raise review_required on the row")
        _check(report.review_required, "the run did not flag review_required")
        _check(row.categories["new feature"] > 0 and row.categories["deps"] == 1, f"categories: {dict(row.categories)}")
        conflicts = [conflict for commit in row.commits for conflict in commit.conflicts]
        _check(
            "prefix docs disagrees with paths deps" in conflicts,
            f"the docs-prefix/deps-paths conflict was not recorded: {conflicts}",
        )
        anchors = [claim for claim in row.stale_claims if claim.source == "anchor"]
        _check(bool(anchors), "no stale claim was built from the article's own citations")
        _check(all(claim.heading_line > 0 for claim in anchors), "a stale claim carries no article heading line")
        _check(bool(row.unresolved_anchors), "the fixture's small upstream tree produced no unresolved anchor to report")
        _check(bool(_row(report, "diverged").problems), "the diverged row carries no reason for the rewrite")
        cited = [line for claim in anchors for line in claim.cited_lines]
        _check(bool(cited), f"the numbered path mention carried no cited line: {[claim.section for claim in anchors]}")
        return (
            f"statuses {sorted(statuses)}, {row.ahead_by} commits, {len(row.stale_claims)} stale claims, "
            f"cited upstream lines {sorted(cited)}, {len(row.unresolved_anchors)} unresolved anchors"
        )


def _case_report_cap() -> str:
    with _fixture("drifted") as fixture, _environment(fixture):
        report = drift_run(fixture.articles_dir)
        row = _row(report, "drifted")
        body = to_markdown(report)
        rows = [line for line in body.splitlines() if line.startswith("| [`")]
        _check(
            len(rows) == MAX_COMMITS_IN_BODY,
            f"the body showed {len(rows)} commit rows, expected {MAX_COMMITS_IN_BODY}",
        )
        remaining = len(row.commits) - MAX_COMMITS_IN_BODY
        _check(
            f"and {remaining} more commit" in body and "drift-report.json" in body,
            "the body did not say how many commits the artifact carries",
        )
        try:
            payload: dict[str, Any] = json.loads(to_json(report))
        except ValueError as error:  # a fixture bug is a failed proof
            raise AssertionError(f"the report is not JSON: {error}") from error
        carried = len(_json_row(payload, row.slug)["commits"])
        _check(carried == len(row.commits), f"the JSON carried {carried} of {len(row.commits)} commits")
        return f"{MAX_COMMITS_IN_BODY} rows in the body, all {carried} commits in the JSON"


def _case_publish_idempotent() -> str:
    with _fixture("drifted") as fixture, _api() as api, _environment(fixture, api=api[0], api_token=SELFTEST_TOKEN):
        state = api[1]
        first = _run_cli(["drift", "--publish", "--root", str(fixture.articles_dir), "--repo", CLI_REPO])
        writes_after_first = int(state["writes"])
        second = _run_cli(["drift", "--publish", "--root", str(fixture.articles_dir), "--repo", CLI_REPO])
        writes_after_second = int(state["writes"])
        _check(len(state["issues"]) == 1, f"expected one issue, found {len(state['issues'])}")
        _check(writes_after_first == 1, f"the first publish performed {writes_after_first} writes, expected 1")
        _check(writes_after_second == writes_after_first, "the second publish wrote again")
        _check("issue: open" in first[1], f"the first run did not report opening the issue: {first[1][-300:]}")
        _check("issue: skip" in second[1], f"the second run did not skip: {second[1][-300:]}")
        return f"1 issue, {writes_after_first} write, then {writes_after_second - writes_after_first}"


def _case_publish_closes() -> str:
    with _fixture("drifted") as drifted, _api() as api, _fixture("current") as current:
        state = api[1]
        with _environment(drifted, api=api[0], api_token=SELFTEST_TOKEN):
            _run_cli(["drift", "--publish", "--root", str(drifted.articles_dir), "--repo", CLI_REPO])
            _check(bool(state["issues"]) and state["issues"][0]["state"] == "open", "the drifted run opened no issue")
        with _environment(current, api=api[0], api_token=SELFTEST_TOKEN):
            code, out, _ = _run_cli(["drift", "--publish", "--root", str(current.articles_dir), "--repo", CLI_REPO])
            _check("issue: close" in out, f"the healthy run did not close the issue: {out[-300:]}")
            _check(state["issues"][0]["state"] == "closed", "the issue is still open after a healthy run")
            _check(len(state["comments"]) == 1, f"expected one closing comment, found {len(state['comments'])}")
            _check(code == 0, f"a healthy publish exited {code}")
        return f"closed with {len(state['comments'])} comment, exit 0"


def _case_publish_unhealthy() -> str:
    with _fixture("drifted") as drifted, _fixture("unreachable") as broken, _api() as api:
        state = api[1]
        with _environment(drifted, api=api[0], api_token=SELFTEST_TOKEN):
            _run_cli(["drift", "--publish", "--root", str(drifted.articles_dir), "--repo", CLI_REPO])
        with _environment(broken, api=api[0], api_token=SELFTEST_TOKEN):
            before = int(state["writes"])
            code, out, _ = _run_cli(["drift", "--publish", "--root", str(broken.articles_dir), "--repo", CLI_REPO])
            _check(code != 0, "an unreachable upstream exited 0")
            _check("close gate: kept open" in out, f"the gate did not keep the issue open: {out[-400:]}")
            _check("issue: close" not in out, "an unhealthy run closed the issue")
            _check(state["issues"][0]["state"] == "open", "the issue is not open after an unhealthy run")
            _check(int(state["writes"]) == before + 1, "the unhealthy run did not update the issue after the red run")
            _check("not checked" in str(state["issues"][0]["body"]), "the body does not name the unreachable article")
            code_again, _, _ = _run_cli(["drift", "--publish", "--root", str(broken.articles_dir), "--repo", CLI_REPO])
            _check(int(state["writes"]) == before + 1, "a repeated unreachable run wrote again: the stderr is in the hash")
            _check(code_again != 0, "the repeated unreachable run exited 0")
        return f"issue open, {int(state['writes']) - before} write for 2 unhealthy runs, exits {code}/{code_again}"


def _case_no_external_target() -> str:
    with _fixture("drifted") as fixture, _api() as api, _environment(fixture, api=api[0], api_token=SELFTEST_TOKEN):
        state = api[1]
        _run_cli(["drift", "--publish", "--root", str(fixture.articles_dir), "--repo", CLI_REPO])
        targets = [str(entry["target"]) for entry in state["requests"]]
        _check(bool(targets), "the publish performed no request at all")
        _check(all(target.startswith("/") for target in targets), f"a request left the loopback base: {targets}")
        _check(api[0].startswith("http://127.0.0.1:"), f"the fixture API is not loopback: {api[0]}")
        _check(not fixture.git_base.startswith("http"), f"the fixture git base is not a local path: {fixture.git_base}")
        return f"{len(targets)} requests, all to {api[0]}"


def _case_markdownlint() -> str:
    binary = REPO_ROOT / "node_modules" / ".bin" / "markdownlint-cli2"
    if not binary.exists():
        # The selftest job installs nothing, so the linter is absent exactly where this half is
        # proved; the validate job runs `npm ci` and proves this one check end to end. Reported
        # as a skip rather than a pass, so a no-op can never be read as evidence.
        return "skip: markdownlint-cli2 is not installed here; the validate job proves this check with npm ci"
    with _fixture("current") as fixture:
        inventory = discover(fixture.articles_dir)
        target = sorted(inventory.articles, key=lambda item: item.path)[0]
        _inject_md032(fixture.articles_dir / target.path)
        problems = check_markdownlint(fixture.articles_dir, inventory.articles)
        _check(len(problems) == 1, f"expected exactly one lint problem, got {[problem.detail for problem in problems]}")
        _check(problems[0].code == "markdownlint", f"wrong check code: {problems[0].code}")
        _check("MD032" in problems[0].detail, f"the injected finding was not MD032: {problems[0].detail}")
        _check(problems[0].line > 0, "the lint problem carries no line")
        return f"{target.path}:{problems[0].line} {problems[0].detail[:48]}"


def _copy_skeleton(articles_dir: Path) -> None:
    for name in ("README.md", ".markdownlint.json", "package.json"):
        shutil.copy2(REPO_ROOT / name, articles_dir / name)
    for source in sorted(REPO_ROOT.glob("*.md")):
        shutil.copy2(source, articles_dir / source.name)
    shutil.copytree(REPO_ROOT / "diagrams", articles_dir / "diagrams")
    shutil.copytree(REPO_ROOT / "tools" / "capture", articles_dir / "tools" / "capture")
    if (REPO_ROOT / "node_modules" / ".bin" / "markdownlint-cli2").exists():
        (articles_dir / "node_modules").symlink_to(REPO_ROOT / "node_modules", target_is_directory=True)


def _materialise_images(articles_dir: Path, articles: Sequence[Article]) -> None:
    """Create every image the copied articles reference.

    Invariant: the file list is derived from the articles' own references, so the fixture
    never keeps a second copy of the article set.
    """
    written: set[str] = set()
    (articles_dir / "images").mkdir(parents=True, exist_ok=True)
    for source in sorted((articles_dir / "diagrams").glob("*.mmd")):
        for theme in ("light", "dark"):
            target = image_path(source.stem, theme)
            (articles_dir / target).write_bytes(b"\x89PNG\r\n\x1a\n")
            written.add(target)
    for article in articles:
        text = (articles_dir / article.path).read_text(encoding="utf-8")
        _, refs = parse_refs(text, parse_sections(text))
        for ref in refs:
            target = ref.target.split("#", 1)[0].split("?", 1)[0]
            if not target.startswith("images/") or target in written:
                continue
            path = articles_dir / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            written.add(target)


def _build_upstream(git_base: Path, article: Article, branch: str, *, scenario: str, role: str) -> tuple[str, str]:
    """One bare upstream, with the commit history the scenario asks for.

    Invariant: the recorded pin is written seven characters long, exactly like the real
    articles, so the fixture exercises the short-sha resolution the report depends on.
    """
    bare = _bare_path(git_base, article.repo.full_name())
    bare.parent.mkdir(parents=True, exist_ok=True)
    work = bare.parent / f"{article.repo.name}.work"
    run_git(["git", "init", "-q", "--bare", "-b", branch, str(bare)])
    run_git(["git", "init", "-q", "-b", branch, str(work)])
    _configure(work)

    (work / "README.md").write_text(f"# {article.repo.name}\n\nfixture upstream\n", encoding="utf-8")
    for target in _referenced_paths(article):
        path = work / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {target}\n", encoding="utf-8")
    _commit(work, "feat: the first fixture commit")
    pin = run_git(["git", "rev-parse", "HEAD"], cwd=work).strip()[:7]
    run_git(["git", "push", "-q", str(bare), branch], cwd=work)

    if scenario == "drifted" and role == "drifted":
        _extend(work, article)
        run_git(["git", "push", "-q", str(bare), branch], cwd=work)
    elif scenario == "drifted" and role == "diverged":
        _rewrite(work)
        run_git(["git", "push", "-q", "-f", str(bare), branch], cwd=work)
    elif scenario == "drifted" and role == "pin-unknown":
        pin = UNKNOWN_PIN

    tip = run_git(["git", "rev-parse", "HEAD"], cwd=work).strip()
    return tip, pin


def _extend(work: Path, article: Article) -> None:
    """The drifted scenario's commits: known categories, one recorded conflict, one unclear.

    Invariant: the fix commit touches both a linked path and a path the article quotes at a
    line number, so the report has to carry the cited line as well as the heading line.
    """
    anchor = next((link.path for link in article.links if link.path), "README.md")
    numbered = _numbered_paths(article)
    code = f"{ADDED_CODE_DIR}/cache.go"

    _write(work, code, "package ci\n")
    _commit(work, "feat(cache): add the shared cache stage")

    _write(work, code, "package ci // guarded\n")
    _write(work, anchor, "# moved on\n")
    for target in numbered[:1]:
        _write(work, target, "# the line the article quoted changed\n")
    _commit(work, "fix: handle an empty response from the upstream cache")

    _write(work, "package.json", '{"name":"fixture","dependencies":{"next":"2.0.0"}}\n')
    _write(work, "package-lock.json", '{"lockfileVersion":3}\n')
    _commit(work, "docs: bump the pinned toolchain to 2.0")

    _write(work, code, "package ci // wibble\n")
    _commit(work, "Wibble the frobnicator")

    for number in range(21):
        _write(work, f"{ADDED_CODE_DIR}/module{number}.go", f"package ci // module {number}\n")
        _commit(work, f"feat: add fixture module {number}")


def _rewrite(work: Path) -> None:
    run_git(["git", "commit", "-q", "--amend", "-m", "refactor: rewrite the published history"], cwd=work)


def _write(work: Path, path: str, text: str) -> None:
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(work: Path, message: str) -> None:
    run_git(["git", "add", "-A"], cwd=work)
    run_git(["git", "commit", "-q", "-m", message], cwd=work)


def _configure(work: Path) -> None:
    run_git(["git", "config", "user.name", GIT_IDENTITY[0]], cwd=work)
    run_git(["git", "config", "user.email", GIT_IDENTITY[1]], cwd=work)


def _rewrite_pins(articles_dir: Path, articles: Sequence[Article], pins: Mapping[str, str]) -> None:
    for article in articles:
        if article.pin is None:
            continue
        file = articles_dir / article.path
        text = file.read_text(encoding="utf-8")
        replaced = article.pin.block.replace(f"`{article.pin.sha}`", f"`{pins[article.slug]}`", 1)
        file.write_text(text.replace(article.pin.block, replaced), encoding="utf-8")


def _break_pin(articles_dir: Path, article: Article) -> None:
    if article.pin is None:
        return
    file = articles_dir / article.path
    text = file.read_text(encoding="utf-8")
    broken = article.pin.block.replace(f"`{article.pin.sha}`", "`not-a-commit`", 1)
    file.write_text(text.replace(article.pin.block, broken), encoding="utf-8")


def _init_article_repo(articles_dir: Path) -> None:
    run_git(["git", "init", "-q", "-b", "main", str(articles_dir)])
    _configure(articles_dir)
    run_git(["git", "add", "-A"], cwd=articles_dir)
    run_git(["git", "commit", "-q", "-m", "fixture articles"], cwd=articles_dir)


def _referenced_paths(article: Article) -> tuple[str, ...]:
    """The upstream paths the fixture must create for an article's claims to resolve.

    Invariant: derived from the article's own links and numbered mentions, so the fixture
    never keeps its own copy of any article's contents.
    """
    paths: list[str] = []
    for link in article.links:
        if link.path is None:
            continue
        paths.append(f"{link.path}/index.md" if link.kind == "tree" else link.path)
    paths.extend(_numbered_paths(article))
    return tuple(dict.fromkeys(paths))


def _numbered_paths(article: Article) -> tuple[str, ...]:
    """The paths this article quotes with a `:line` suffix, without the suffix."""
    found: list[str] = []
    for mention in article.mentions:
        if not NUMBERED_MENTION_RE.match(mention.text):
            continue
        path = mention.text.rsplit(":", 1)[0]
        if looks_like_path(path) and path not in found:
            found.append(path)
    return tuple(found)


def _scenario_roles(articles: Sequence[Article]) -> tuple[int, int, int]:
    """Which article plays the drifted, diverged and pin-unknown part.

    Invariant: the drifted part goes to the first article that quotes a path at a line
    number, so the fixture proves the cited-line handoff; the other two parts go to the
    first articles that are left, in file order. No slug is named here.
    """
    indices = range(len(articles))
    drifted = next((index for index in indices if _numbered_paths(articles[index])), 0)
    diverged = next(index for index in indices if index != drifted)
    unknown = next(index for index in indices if index not in (drifted, diverged))
    return drifted, diverged, unknown


def _bare_path(git_base: Path, repo: str) -> Path:
    return git_base / f"{repo}.git"


def _row(report: DriftReport, status: str):
    for row in report.articles:
        if row.status == status:
            return row
    raise AssertionError(f"the report has no {status} row")


def _json_row(payload: dict[str, Any], slug: str) -> dict[str, Any]:
    for row in payload["articles"]:
        if row["slug"] == slug:
            return row
    raise AssertionError(f"the JSON has no {slug} row")


def _inject_md032(path: Path) -> None:
    """Put a numbered list directly under the closing paragraph, which is exactly MD032.

    Invariant: only this rule fires, so the case asserts a specific finding rather than
    "some finding"; the closing paragraph is the last block of every article.
    """
    text = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(f"{text}\n1. a list with no blank line before it\n", encoding="utf-8")


def anchor_ref_owner(articles: Sequence[Article], ref: object) -> str:
    """The article file that carries one parsed local reference."""
    for article in articles:
        if any(candidate is ref or candidate == ref for candidate in article.local_refs):
            return article.path
    raise AssertionError("no article owns the reference being broken")


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    from .cli import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(argv)
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _fixture(scenario: str) -> Iterator[Fixture]:
    with tempfile.TemporaryDirectory(prefix=f"articles-ci-selftest-{scenario}-") as directory:
        yield build_fixture(Path(directory), scenario=scenario)


@contextlib.contextmanager
def _api() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"issues": [], "comments": [], "requests": [], "writes": 0}
    base, shutdown = start_fake_api(state)
    try:
        yield base, state
    finally:
        shutdown()


@contextlib.contextmanager
def _environment(fixture: Fixture, *, api: str | None = None, api_token: str | None = None) -> Iterator[None]:
    saved = dict(os.environ)
    os.environ["ARTICLES_CI_GIT_BASE"] = fixture.git_base
    os.environ["ARTICLES_CI_WORKDIR"] = str(fixture.root / "workdir")
    os.environ["GITHUB_REPOSITORY"] = CLI_REPO
    for name in ("ARTICLES_CI_API_BASE", "GITHUB_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_RUN_URL", "GITHUB_RUN_ID"):
        os.environ.pop(name, None)
    if api:
        os.environ["ARTICLES_CI_API_BASE"] = api
    if api_token:
        os.environ["GITHUB_TOKEN"] = api_token
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
