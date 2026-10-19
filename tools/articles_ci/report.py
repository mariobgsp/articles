"""The report's two rendered forms, and the decision of whether the issue needs a write.

Invariant: this module is the only place that knows what the issue body looks like, and
`merge_check_run` is a pure function of (existing body, report), so the idempotency rule is
decided without HTTP and the body can never drift from the JSON it was rendered from.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Literal

from .drift import (
    UNCHECKED_STATUSES,
    ArticleDrift,
    DriftReport,
    StaleClaim,
    healthy,
    unhealthy,
)

MARKER = "<!-- articles-drift-report"
MARKER_HASH_RE = re.compile(r"content-sha256=([0-9a-f]+)")
MAX_COMMITS_IN_BODY = 20
DRIFT_REPORT_ARTIFACT = "drift-report.json"
Action = Literal["skip", "update", "open", "reopen", "close"]
STATUS_LABELS: dict[str, str] = {
    "up-to-date": "current",
    "drifted": "moved",
    "diverged": "history rewritten",
    "pin-unknown": "pin unknown",
    "unreachable": "not checked",
    "malformed-pin": "pin malformed",
}


@dataclass(frozen=True)
class MergeDecision:
    """What the next API call should be, and the body it should carry."""

    action: Action
    body: str


def to_json(report: DriftReport) -> str:
    """The wire shape, in a fixed key order, with every commit the run saw.

    Invariant: the JSON is the complete artifact; the issue body is a view of it, so
    anything the body truncates is still here and still machine-readable.
    """
    payload = {
        "schema": report.schema,
        "generated_at": report.generated_at,
        "run_url": report.run_url,
        "content_hash": report.content_hash,
        "review_required": report.review_required,
        "totals": dict(report.totals),
        "other_root_markdown": list(report.other_root_markdown),
        "defective": [
            {"slug": row.slug, "path": row.path, "detail": row.detail} for row in report.defective
        ],
        "articles": [_article_json(row) for row in report.articles],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def to_markdown(report: DriftReport) -> str:
    """The issue body, and the human's local read of the same run.

    Invariant: every line is generated from an `ArticleDrift` row, so the body cannot say
    anything the JSON does not, and the marker line is the first line because that is what
    `issues.upsert` reads to decide whether anything changed.
    """
    drifted = report.totals["drifted"]
    total = report.totals["articles"]
    checked = _checked_at(report)
    lines = [
        f"{MARKER} schema={report.schema} content-sha256={report.content_hash} -->",
        f"# Article drift: {drifted} of {total} upstreams moved past their recorded pins",
        "",
    ]
    run = f" · [run]({report.run_url})" if report.run_url else ""
    lines.append(f"Checked {checked} UTC{run}")
    lines.append("")
    if healthy(report):
        lines.append(f"All {total} pins are current as of {checked} UTC. Nothing to update, and CI never edits prose.")
    else:
        current = report.totals["up-to-date"]
        lines.append(
            f"{current} of {total} upstreams are current. CI does not edit prose: update the article, "
            "then move its closing pin to the commit you verified against."
        )
    lines.append("")
    lines.extend(_summary_table(report))
    number = 0
    for row in report.articles:
        if row.status in ("unreachable", "malformed-pin", "up-to-date"):
            continue
        number += 1
        lines.append("")
        lines.extend(_article_section(row, number))
    lines.extend(_not_checked(report))
    lines.append("")
    lines.append("---")
    lines.append("")
    # README.md is the index, not news: `validate`'s readme-index check owns it. Anything else
    # at the root is a document no check lints, so the footer is where a human first sees it.
    other = tuple(name for name in report.other_root_markdown if name != "README.md")
    other_note = f" · {len(other)} other root markdown ({', '.join(other)})" if other else ""
    lines.append(
        f"<sub>Schema `{report.schema}` · {total} articles derived, {report.totals['defective']} defective · "
        f"content `{report.content_hash}`{other_note} · the complete commit list is attached to the run as "
        f"`{DRIFT_REPORT_ARTIFACT}`</sub>"
    )
    return "\n".join(lines) + "\n"


def to_annotations(report: DriftReport) -> tuple[str, ...]:
    """`::warning::` per article that could not be checked, and one `::notice::` summary.

    Invariant: the warnings carry the failed command's own stderr, so a red job's annotation
    says which host failed without anyone opening the log.
    """
    annotations: list[str] = []
    for row in report.articles:
        if row.status in ("unreachable", "pin-unknown", "diverged", "malformed-pin"):
            detail = row.problems[0] if row.problems else STATUS_LABELS[row.status]
            annotations.append(f"::warning file={row.article}::{detail}")
    for row in report.defective:
        annotations.append(f"::warning file={row.path}::{row.detail}")
    annotations.append(
        f"::notice::article drift: {report.totals['drifted']} of {report.totals['articles']} upstreams moved, "
        f"{report.totals['not-checked']} not checked, content {report.content_hash}"
    )
    return tuple(annotations)


def merge_check_run(existing_body: str, report: DriftReport) -> MergeDecision:
    """Whether the issue needs a write at all, from its marker versus the report's hash.

    Invariant: a run whose drift substance is unchanged decides "skip" and performs no
    request, and a healthy run only ever decides "close" - never "open", so an issue exists
    iff something needs a human.
    """
    has_human_work = not healthy(report)
    same_substance = marker_hash(existing_body) == report.content_hash
    if same_substance:
        return MergeDecision(action="skip", body=existing_body)
    if has_human_work:
        return MergeDecision(action="update" if existing_body else "open", body=to_markdown(report))
    if not existing_body:
        return MergeDecision(action="skip", body="")
    return MergeDecision(action="close", body=to_markdown(report))


def marker_hash(body: str) -> str | None:
    """The content hash carried by an existing issue body, or None when it carries no marker.

    Invariant: this is the whole stored state of the pipeline, so a hand-edited body without
    a marker reads as "no issue yet" rather than as matching.
    """
    match = MARKER_HASH_RE.search(body)
    return match.group(1) if match else None


def gate_decision(report: DriftReport) -> str:
    """The one-line decision the run prints: whether the drift issue stays open.

    Invariant: the string is derived from the same `healthy`/`unhealthy` predicates that
    `issues.upsert` and the exit code use, so the printed decision cannot disagree with what
    the run actually did.
    """
    if healthy(report):
        return "close gate: clear - every pin is current and no article has problems, so nothing needs a human"
    flagged = sum(1 for row in report.articles if row.problems and row.status not in UNCHECKED_STATUSES)
    reasons = [f"{report.totals['drifted']} drifted", f"{report.totals['not-checked']} not checked"]
    if report.totals["defective"]:
        reasons.append(f"{report.totals['defective']} defective")
    if flagged:
        reasons.append(f"{flagged} flagged")
    if unhealthy(report):
        tail = "the run exits non-zero after the issue has been written"
    elif flagged:
        tail = "exit 0: a flagged article is a validate problem, not a drift failure"
    else:
        tail = "plain drift is exit 0"
    return f"close gate: kept open - {', '.join(reasons)}; {tail}"


def write_step_summary(report: DriftReport) -> bool:
    """Write the body to `$GITHUB_STEP_SUMMARY` when it is set.

    Invariant: this is the only file the drift command writes outside `workdir` and
    `--out`, and only because the runner asked for it in the environment.
    """
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return False
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(to_markdown(report))
    except OSError:
        return False
    return True


def _article_json(row: ArticleDrift) -> dict[str, object]:
    return {
        "slug": row.slug,
        "article": row.article,
        "repo": row.repo,
        "status": row.status,
        "recorded_sha": row.recorded_sha,
        "recorded_note": row.recorded_note,
        "default_branch": row.default_branch,
        "upstream_sha": row.upstream_sha,
        "ahead_by": row.ahead_by,
        "behind_by": row.behind_by,
        "merge_commits": row.merge_commits,
        "range": row.range,
        "review_required": row.review_required,
        "categories": dict(row.categories),
        "commits": [
            {
                "sha": commit.commit.sha,
                "short": commit.commit.short,
                "date": commit.commit.date,
                "subject": commit.commit.subject,
                "category": commit.category,
                "confidence": commit.confidence,
                "decided_by": commit.decided_by,
                "conflicts": list(commit.conflicts),
                "evidence": list(commit.evidence),
                "paths": list(commit.commit.paths),
                "paths_truncated": commit.commit.paths_truncated,
                "touches": sorted({anchor.scope for anchor in commit.touches}),
            }
            for commit in row.commits
        ],
        "stale_claims": [_claim_json(claim) for claim in row.stale_claims],
        "unresolved_anchors": [
            {"scope": anchor.scope, "text": anchor.text, "detail": anchor.detail, "line": anchor.line}
            for anchor in row.unresolved_anchors
        ],
        "also_stale": list(row.also_stale),
        "problems": list(row.problems),
    }


def _claim_json(claim: StaleClaim) -> dict[str, object]:
    return {
        "section": claim.section,
        "role": claim.role,
        "claim": claim.claim,
        "evidence_paths": list(claim.evidence_paths),
        "commits": list(claim.commits),
        "source": claim.source,
        "heading_line": claim.heading_line,
        "cited_lines": list(claim.cited_lines),
        "article_lines": list(claim.article_lines),
    }


def _summary_table(report: DriftReport) -> tuple[str, ...]:
    lines = [
        "| Article | Upstream | Default branch | Recorded | Upstream | Commits |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.articles:
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                f"`{row.article}`",
                row.repo,
                f"`{row.default_branch}`" if row.default_branch else "—",
                f"`{row.recorded_sha}`" if row.recorded_sha else "—",
                _upstream_cell(row),
                _commits_cell(row),
            )
        )
    for row in report.defective:
        lines.append(f"| `{row.path}` | — | — | — | — | defective: {row.detail} |")
    return tuple(lines)


def _upstream_cell(row: ArticleDrift) -> str:
    if row.status == "up-to-date" or row.upstream_sha is None:
        return "—"
    return f"[`{row.upstream_sha[:7]}`]({_commit_url(row.repo, row.upstream_sha)})"


def _commits_cell(row: ArticleDrift) -> str:
    if row.status == "drifted":
        merges = f" (+{row.merge_commits} merges)" if row.merge_commits else ""
        return f"{row.ahead_by}{merges}"
    return STATUS_LABELS[row.status]


def _article_section(row: ArticleDrift, number: int) -> tuple[str, ...]:
    lines = [f"## {number} — `{row.article}`", ""]
    branch = f"`{row.repo}@{row.default_branch}`" if row.default_branch else f"`{row.repo}`"
    tip = f"`{row.upstream_sha[:7]}`" if row.upstream_sha else "unknown"
    if row.status == "drifted":
        merges = f"; {row.merge_commits} merges not classified" if row.merge_commits else ""
        lines.append(
            f"Recorded `{row.recorded_sha}` → {branch} {tip}; {row.ahead_by} commits, "
            f"range `{row.range}`{merges}."
        )
        lines.append("")
        if row.review_required:
            lines.append("**Review needed**: at least one commit could not be classified from its paths or subject.")
            lines.append("")
        lines.extend(_category_table(row))
        lines.append("")
        lines.extend(_commit_table(row))
    else:
        lines.append(f"Recorded `{row.recorded_sha}` → {branch} {tip}: {STATUS_LABELS[row.status]}.")
        lines.append("")
        for problem in row.problems:
            lines.append(f"- {problem}")
    lines.append("")
    lines.extend(_stale_sections(row))
    lines.extend(_unresolved(row))
    lines.append("")
    lines.extend(_handoff(row))
    return tuple(lines)


def _category_table(row: ArticleDrift) -> tuple[str, ...]:
    header = "| Category | " + " | ".join(row.categories.keys()) + " |"
    divider = "| --- | " + " | ".join("---" for _ in row.categories) + " |"
    values = "| Commits | " + " | ".join(str(count) for count in row.categories.values()) + " |"
    return (header, divider, values)


def _commit_table(row: ArticleDrift) -> tuple[str, ...]:
    total = len(row.commits)
    shown = row.commits[:MAX_COMMITS_IN_BODY]
    lines = [
        f"<details><summary><b>{total} commits</b> — sha, date, category, confidence, evidence</summary>",
        "",
        "| sha | date | category | subject | evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for commit in shown:
        evidence = ", ".join(f"`{item}`" for item in commit.evidence)
        lines.append(
            f"| [`{commit.commit.short}`]({_commit_url(row.repo, commit.commit.sha)}) | {commit.commit.date} | "
            f"{commit.category} ({commit.confidence}) | `{commit.commit.subject}` | {evidence} |"
        )
        for conflict in commit.conflicts:
            lines.append(f"| | | | | conflict: {conflict} |")
    remaining = total - len(shown)
    if remaining > 0:
        lines.append("")
        lines.append(
            f"… and {remaining} more commit{'s' if remaining != 1 else ''} — the full list is in "
            f"`{DRIFT_REPORT_ARTIFACT}`, attached to the run."
        )
    lines.append("")
    lines.append("</details>")
    return tuple(lines)


def _stale_sections(row: ArticleDrift) -> tuple[str, ...]:
    if not row.stale_claims:
        return ()
    lines = ["### Sections that are now stale", "", "| Section | Why | Upstream paths that prove it |", "| --- | --- | --- |"]
    for claim in row.stale_claims:
        paths = ", ".join(f"`{path}`" for path in claim.evidence_paths)
        lines.append(f"| {_claim_location(claim)} | {_claim_why(claim)} | {paths} |")
    if row.also_stale:
        lines.append("")
        lines.append("Also stale: " + "; ".join(f"`{name}`" for name in row.also_stale) + ".")
    return tuple(lines)


def _claim_location(claim: StaleClaim) -> str:
    location = f"`{claim.section}` (line {claim.heading_line})" if claim.heading_line else f"`{claim.section}`"
    if claim.cited_lines:
        cited = ", ".join(f"line {line}" for line in claim.cited_lines)
        location += f" — cites {cited}"
    return location


def _claim_why(claim: StaleClaim) -> str:
    why = claim.claim
    if claim.article_lines:
        why += " (quoted at article line " + ", ".join(str(line) for line in claim.article_lines) + ")"
    return why


def _unresolved(row: ArticleDrift) -> tuple[str, ...]:
    if not row.unresolved_anchors:
        return ()
    lines = [
        "",
        f"Anchors that did not resolve against the upstream tree at `{row.recorded_sha}` (reported, not a failure):",
        "",
    ]
    for anchor in row.unresolved_anchors:
        lines.append(f"- `{anchor.text}` (cited under `{anchor.scope}`) — {anchor.detail}")
    return tuple(lines)


def _handoff(row: ArticleDrift) -> tuple[str, ...]:
    """The three things a human has to do, in the order they have to do them.

    Invariant: the pin the handoff names is the tip this run actually diffed, and the
    diagram command is the one this repository's own renderer accepts (`<slug>`, or none for
    every diagram).
    """
    short = row.upstream_sha[:7] if row.upstream_sha else None
    sections = [claim.section for claim in row.stale_claims]
    lines = ["### Handoff", ""]
    if sections:
        unique = list(dict.fromkeys(sections))
        lines.append(f"- edit `{row.article}` § " + " / § ".join(f"`{section}`" for section in unique))
    elif row.status == "drifted":
        lines.append(f"- re-read `{row.article}` against the new commits; no cited path moved on its own")
    else:
        lines.append(f"- re-verify `{row.article}` by hand: its recorded pin is not in the published history")
    lines.append(
        f"- regenerate `diagrams/{row.slug}.mmd`, then run `python3 tools/render-diagrams.py {row.slug}` "
        "(both the light and the `-dark` render)"
    )
    if short:
        lines.append(f"- move the closing pin to `{short}`")
    return tuple(lines)


def _not_checked(report: DriftReport) -> tuple[str, ...]:
    """Every article a human still has to look at: unchecked, or checked but flagged.

    Invariant: a row that keeps the close gate shut is named here with its own reason, so the
    body never says "nothing to do" about an article the gate is holding open.
    """
    rows = [row for row in report.articles if row.status in ("unreachable", "malformed-pin") or row.problems]
    if not rows and not report.defective:
        return ()
    lines = ["", "### Not checked, or carrying a problem", "", "| Article | Reason |", "| --- | --- |"]
    for row in rows:
        reason = row.problems[0] if row.problems else STATUS_LABELS[row.status]
        lines.append(f"| `{row.article}` | {reason} |")
    for row in report.defective:
        lines.append(f"| `{row.path}` | {row.detail} |")
    return tuple(lines)


def _checked_at(report: DriftReport) -> str:
    return report.generated_at.replace("T", " ").removesuffix("Z")


def _commit_url(repo: str, sha: str) -> str:
    return f"https://github.com/{repo}/commit/{sha}"
