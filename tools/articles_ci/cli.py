"""The three subcommands, their output, and their exit codes.

Invariant: the CLI is the only caller of `report` and `issues`, the only place that prints,
and the only place that decides an exit code. `validate` exits 1 on a finding, `drift` exits
1 only when an article could not be checked, and 2 when the invocation itself is wrong - so
plain drift into new commits stays green and a red run is always a real problem.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from .drift import run as drift_run
from .drift import unhealthy
from .git import origin_repo
from .issues import GitHubAPI, GitHubError, report_title, upsert
from .parse import discover
from .report import gate_decision, to_annotations, to_json, to_markdown, write_step_summary
from .selftest import main as selftest_main
from .validate import validate_repo

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
REPORT_FILENAME = "drift-report.json"


def main(argv: Sequence[str]) -> int:
    """Parse `argv`, run one subcommand, and return its exit code."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.articles_ci",
        description="Validate the five articles, and detect upstream drift behind their pins.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate", help="check the article tree against itself (no network)")
    validate.add_argument("--root", default=".", help="repository root (default: the current directory)")

    drift = subcommands.add_parser("drift", help="check each article's pin against its upstream")
    drift.add_argument("--root", default=".", help="repository root (default: the current directory)")
    drift.add_argument("--out", default=None, help=f"write the report JSON to this path (CI uses {REPORT_FILENAME})")
    drift.add_argument("--publish", action="store_true", help="update the single drift issue (needs GITHUB_TOKEN)")
    drift.add_argument("--repo", default=None, help="owner/name of this repository (default: GITHUB_REPOSITORY or origin)")

    subcommands.add_parser("selftest", help="prove validate, drift and publish offline against local fixtures")

    args = parser.parse_args(list(argv))
    if args.command == "validate":
        return _validate(Path(args.root).resolve())
    if args.command == "drift":
        return _drift(args)
    return selftest_main()


def _validate(root: Path) -> int:
    inventory = discover(root)
    problems = validate_repo(root, inventory=inventory)
    counts = Counter(problem.code for problem in problems)
    refs = sum(len(article.local_refs) for article in inventory.articles)
    print(f"markdownlint: {counts['markdownlint']} problems")
    print(f"pin: {counts['pin']} problems")
    print(f"local refs / anchors: {counts['local-ref'] + counts['local-anchor']} problems")
    print(f"diagram: {counts['diagram']} problems")
    print(f"capture script: {counts['capture']} problems")
    print(f"readme index: {counts['readme-index']} problems")
    print(f"readme command: {counts['readme-command']} problems")
    for problem in problems:
        print(f"::error file={problem.path},line={max(1, problem.line)}::{problem.detail}")
    if problems:
        files = len({problem.path for problem in problems})
        print(f"\n{len(problems)} problems in {files} files.")
        return EXIT_FINDINGS
    print(f"\n{len(inventory.articles)} articles valid ({refs} local refs resolved).")
    return EXIT_OK


def _drift(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if args.publish and not token:
        print(
            "--publish needs GITHUB_TOKEN in the environment; run without --publish to read the report only",
            file=sys.stderr,
        )
        return EXIT_USAGE

    root = Path(args.root).resolve()
    # The mirrors live for the length of the run only: no cache to go stale, nothing left behind.
    with tempfile.TemporaryDirectory(prefix="articles-ci-") as work:
        report = drift_run(root, workdir=Path(work))
    print(to_markdown(report), end="")
    totals = report.totals
    print(
        f"articles: {totals['articles']} - up-to-date {totals['up-to-date']}, drifted {totals['drifted']}, "
        f"not checked {totals['not-checked']}, defective {totals['defective']}; content {report.content_hash}"
    )
    for annotation in to_annotations(report):
        print(annotation)
    print(gate_decision(report))
    if args.out:
        Path(args.out).write_text(to_json(report), encoding="utf-8")
        print(f"report: wrote {args.out} ({len(report.articles)} articles, content {report.content_hash})")
    write_step_summary(report)

    if args.publish and token:
        repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or origin_repo(root)
        base = os.environ.get("ARTICLES_CI_API_BASE") or None
        try:
            outcome = upsert(GitHubAPI(token, base), repo, report)
        except GitHubError as error:
            print(f"issue: failed - {error}", file=sys.stderr)
            return EXIT_USAGE
        print(f"issue: {outcome.action} {outcome.issue_number if outcome.issue_number else ''} - {outcome.detail}")
        print(f"issue title: {report_title(report)}")

    if unhealthy(report):
        print(
            "\nunhealthy: at least one article could not be checked; the drift issue is kept open",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    return EXIT_OK
