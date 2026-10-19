"""Half 2's policy: one article against its upstream, and the run's content hash.

Invariant: one unreachable upstream never costs the other four their report - every
failure is a `DriftStatus` with its stderr attached, never an exception. `content_hash`
covers the drift substance only, so a run that learns nothing new performs no write at all.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .anchor import AnchorIndex, Role, UnresolvedAnchor, build_index, claim_for, scope_role
from .classify import CATEGORIES, Category, ClassifiedCommit, classify, manifest_paths, summarize
from .git import GitError, PinUnknown, resolve_remote, workdir_default
from .parse import Article, DefectiveArticle, Inventory, discover, parse_sections

DriftStatus = Literal[
    "up-to-date",
    "drifted",
    "diverged",
    "pin-unknown",
    "unreachable",
    "malformed-pin",
]

UNCHECKED_STATUSES: tuple[DriftStatus, ...] = ("unreachable", "pin-unknown", "malformed-pin", "diverged")


@dataclass(frozen=True)
class StaleClaim:
    """One article section that the commit range makes stale, with the upstream paths that
    prove it.

    Invariant: `commits` is never empty for a stale claim, because a claim without a commit
    is a guess; `heading_line` is where the human starts reading, and `cited_lines` are the
    upstream line numbers the article itself quoted.
    """

    section: str
    role: Role
    claim: str
    evidence_paths: tuple[str, ...]
    commits: tuple[str, ...]
    source: Literal["anchor", "manifest"]
    heading_line: int
    cited_lines: tuple[int, ...]
    article_lines: tuple[int, ...]


@dataclass(frozen=True)
class ArticleDrift:
    """One row of the report.

    Invariant, enforced by `__post_init__` and not only stated here:
      status == "drifted"    -> upstream_sha is not None and ahead_by > 0 and commits
      status == "up-to-date" -> upstream_sha is not None and ahead_by == 0
      status in ("diverged", "pin-unknown", "unreachable", "malformed-pin") -> problems
    Every row carries the same keys even when a status leaves some empty, so a consumer can
    index a row without branching on the status first.
    """

    slug: str
    article: str
    repo: str
    status: DriftStatus
    recorded_sha: str | None
    recorded_note: str
    default_branch: str | None
    upstream_sha: str | None
    ahead_by: int
    behind_by: int
    merge_commits: int
    range: str | None
    commits: tuple[ClassifiedCommit, ...]
    categories: Mapping[Category, int]
    review_required: bool
    stale_claims: tuple[StaleClaim, ...]
    unresolved_anchors: tuple[UnresolvedAnchor, ...]
    also_stale: tuple[str, ...]
    problems: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status == "drifted" and (self.upstream_sha is None or self.ahead_by <= 0 or not self.commits):
            raise ValueError(f"{self.slug}: drifted needs an upstream sha, commits ahead and a commit list")
        if self.status == "up-to-date" and (self.upstream_sha is None or self.ahead_by != 0):
            raise ValueError(f"{self.slug}: up-to-date needs a resolved upstream sha and nothing ahead")
        if self.status in UNCHECKED_STATUSES and not self.problems:
            raise ValueError(f"{self.slug}: {self.status} must carry the reason it could not be checked")


@dataclass(frozen=True)
class DriftReport:
    """The whole run.

    Invariant: `content_hash` covers the drift substance and excludes `generated_at`,
    `run_url`, and the prose of an unreachable failure, which is what makes the issue
    upsert idempotent across two runs that learned the same thing.
    """

    schema: str
    generated_at: str
    run_url: str | None
    articles: tuple[ArticleDrift, ...]
    defective: tuple[DefectiveArticle, ...]
    other_root_markdown: tuple[str, ...]
    totals: Mapping[str, int]
    review_required: bool
    content_hash: str


def check_article(article: Article, *, workdir: Path, repo_root: Path | None = None) -> ArticleDrift:
    """Half 2 for one article.

    Invariant: `head()` runs first, so an article whose pin is still the published tip costs
    one `ls-remote` and no clone, and every failure below it becomes a status with its own
    reason instead of an exception.
    """
    repo = article.repo.full_name()
    if article.pin is None:
        return _row(article, status="malformed-pin", problems=tuple(problem.detail for problem in article.problems))

    remote = resolve_remote(repo, workdir)
    try:
        ref = remote.head()
    except GitError as error:
        return _row(
            article,
            status="unreachable",
            problems=(f"git ls-remote --symref {remote.url} HEAD failed 3 times; last stderr: {error.stderr}",),
        )

    if ref.sha.startswith(article.pin.sha):
        return _row(
            article,
            status="up-to-date",
            default_branch=ref.default_branch,
            upstream_sha=ref.sha,
            problems=tuple(problem.detail for problem in article.problems),
        )

    tip = ref.sha
    try:
        remote.fetch()
        tip = remote.tip()
        resolved = remote.resolve(article.pin.sha)
    except PinUnknown as error:
        return _row(
            article,
            status="pin-unknown",
            default_branch=ref.default_branch,
            upstream_sha=tip,
            problems=(f"pin: {error.stderr}",),
        )
    except GitError as error:
        return _row(article, status="unreachable", problems=(f"git clone {remote.url} failed: {error.stderr}",))

    try:
        if not remote.is_ancestor(resolved, tip):
            return _row(
                article,
                status="diverged",
                default_branch=ref.default_branch,
                upstream_sha=tip,
                ahead_by=remote.count(f"{tip}..{resolved}"),
                problems=(
                    f"pin {article.pin.sha} ({resolved[:12]}) is not an ancestor of "
                    f"{repo}@{ref.default_branch} ({tip[:7]}): the published history was rewritten",
                ),
            )

        commits = remote.commits(resolved, tip)
        index = build_index(article, remote.tree(resolved))
        classified = tuple(classify(commit, index) for commit in commits)
        claims = stale_claims(article, index, classified)
        return ArticleDrift(
            slug=article.slug,
            article=article.path,
            repo=repo,
            status="drifted",
            recorded_sha=article.pin.sha,
            recorded_note=article.pin.note,
            default_branch=ref.default_branch,
            upstream_sha=tip,
            ahead_by=len(classified),
            behind_by=0,
            merge_commits=remote.count(f"{resolved}..{tip}", merges=True),
            range=f"{resolved[:12]}..{tip[:7]}",
            commits=classified,
            categories=summarize(classified),
            review_required=any(commit.category == "unclear" for commit in classified),
            stale_claims=claims,
            unresolved_anchors=index.unresolved,
            also_stale=also_stale(article, claims, readme=_readme(repo_root)),
            problems=tuple(problem.detail for problem in article.problems),
        )
    except GitError as error:
        # One upstream that fails after the clone must not cost the other four their report.
        return _row(
            article,
            status="unreachable",
            default_branch=ref.default_branch,
            upstream_sha=tip,
            problems=(f"git failed while reading {repo}@{ref.default_branch}: {error.stderr}",),
        )


def stale_claims(
    article: Article,
    index: AnchorIndex,
    commits: Sequence[ClassifiedCommit],
) -> tuple[StaleClaim, ...]:
    """Section-level staleness, one claim per (scope, source).

    Invariant: two sources and no more - an anchor hit (the article cites the path) and a
    manifest change (the section's role says it quotes versions). Claims are ordered by the
    section's position in the article, so the issue body reads in the same order as the
    article, and a claim with no commit is not emitted.
    """
    hits: dict[tuple[str, str], tuple[set[str], set[int], set[int], set[str]]] = {}
    for classified in commits:
        for anchor in classified.touches:
            key = (anchor.scope, anchor.upstream_path)
            shas, cited, lines, paths = hits.setdefault(key, (set(), set(), set(), set()))
            shas.add(classified.commit.short)
            if anchor.cited_line is not None:
                cited.add(anchor.cited_line)
            lines.add(anchor.line)
            paths.add(anchor.upstream_path)

    claims: list[StaleClaim] = []
    for (scope, _), (shas, cited, lines, paths) in hits.items():
        role = scope_role(scope)
        row = claim_for(role)
        if "anchors" not in row.feeds:
            continue
        claims.append(
            StaleClaim(
                section=scope,
                role=role,
                claim=row.claim,
                evidence_paths=tuple(sorted(paths)),
                commits=_order_shas(commits, shas),
                source="anchor",
                heading_line=_heading_line(article, scope),
                cited_lines=tuple(sorted(cited)),
                article_lines=tuple(sorted(lines)),
            )
        )

    for section in article.sections:
        role = scope_role(section.scope)
        row = claim_for(role)
        if "manifests" not in row.feeds or any(claim.section == section.scope for claim in claims):
            continue
        manifests: set[str] = set()
        shas: set[str] = set()
        for classified in commits:
            touched = manifest_paths(classified.commit.paths)
            if touched:
                manifests.update(touched)
                shas.add(classified.commit.short)
        if not manifests:
            continue
        claims.append(
            StaleClaim(
                section=section.scope,
                role=role,
                claim=row.claim,
                evidence_paths=tuple(sorted(manifests)),
                commits=_order_shas(commits, shas),
                source="manifest",
                heading_line=section.line,
                cited_lines=(),
                article_lines=(),
            )
        )

    return tuple(sorted(claims, key=lambda claim: (claim.heading_line, claim.source)))


def also_stale(article: Article, claims: Sequence[StaleClaim], *, readme: Path | None = None) -> tuple[str, ...]:
    """The other file a defects claim makes stale.

    Invariant: README.md repeats the article's *What I'd fix next* rows, so a defects claim
    names both files; the README section is found in README.md's own headings rather than
    kept as a string here.
    """
    if not any(claim.role == "defects" for claim in claims):
        return ()
    if readme is None:
        return ("README.md",)
    heading = _defect_heading(readme)
    return (f"{readme.name} ({heading})" if heading else readme.name,)


def run(repo_root: Path, *, workdir: Path | None = None) -> DriftReport:
    """The whole of half 2: discover, check each article, build the report and its hash.

    Invariant: the only thing written anywhere is under `workdir`, and the row order is the
    article file order, so the JSON is byte-stable between runs that saw the same upstreams.
    """
    inventory: Inventory = discover(repo_root)
    work = workdir or workdir_default()
    rows = tuple(
        check_article(article, workdir=work, repo_root=repo_root)
        for article in sorted(inventory.articles, key=lambda article: article.path)
    )
    return DriftReport(
        schema="articles-drift/1",
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        run_url=_run_url(),
        articles=rows,
        defective=inventory.defective,
        other_root_markdown=inventory.other_root_markdown,
        totals=_totals(rows, inventory.defective),
        review_required=any(row.review_required for row in rows),
        content_hash=content_hash(rows, inventory.defective),
    )


def content_hash(articles: Sequence[ArticleDrift], defective: Sequence[DefectiveArticle]) -> str:
    """sha256 over the drift substance, 12 hex characters.

    Invariant: `generated_at`, `run_url` and the stderr prose of an unreachable article are
    excluded, so a flaky network or a later run does not look like news and write an issue;
    the unreachable *status* is included, so an upstream coming back does.
    """
    payload: list[dict[str, object]] = []
    for row in articles:
        payload.append(
            {
                "slug": row.slug,
                "status": row.status,
                "recorded_sha": row.recorded_sha,
                "default_branch": row.default_branch,
                "upstream_sha": row.upstream_sha,
                "ahead_by": row.ahead_by,
                "range": row.range,
                "review_required": row.review_required,
                "commits": [
                    {
                        "sha": commit.commit.sha,
                        "category": commit.category,
                        "confidence": commit.confidence,
                        "conflicts": list(commit.conflicts),
                        "evidence": list(commit.evidence),
                        "paths": list(commit.commit.paths),
                    }
                    for commit in row.commits
                ],
                "stale_claims": [
                    {
                        "section": claim.section,
                        "role": claim.role,
                        "source": claim.source,
                        "evidence_paths": list(claim.evidence_paths),
                        "commits": list(claim.commits),
                    }
                    for claim in row.stale_claims
                ],
                "unresolved_anchors": [anchor.text for anchor in row.unresolved_anchors],
                "also_stale": list(row.also_stale),
                "problems": [] if row.status == "unreachable" else list(row.problems),
            }
        )
    for row in defective:
        payload.append({"slug": row.slug, "path": row.path, "detail": row.detail})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def healthy(report: DriftReport) -> bool:
    """Whether every article is at its recorded pin and none carries a problem.

    Invariant: this is the only condition under which the drift issue is closed, so an
    unreachable upstream or a malformed pin can never erase a real drift by looking clean.
    """
    return (
        report.totals["drifted"] == 0
        and report.totals["not-checked"] == 0
        and report.totals["defective"] == 0
        and not any(row.problems for row in report.articles)
    )


def unhealthy(report: DriftReport) -> bool:
    """Whether some article could not be checked at all.

    Invariant: this is the exit gate - plain drift into new commits is a successful run,
    while an unreachable host, a malformed pin, a rewritten history or a pin that is not an
    object fails the job *after* the issue has been written.
    """
    return report.totals["not-checked"] > 0 or report.totals["defective"] > 0


def _row(
    article: Article,
    *,
    status: DriftStatus,
    default_branch: str | None = None,
    upstream_sha: str | None = None,
    ahead_by: int = 0,
    problems: tuple[str, ...] = (),
) -> ArticleDrift:
    pin = article.pin
    return ArticleDrift(
        slug=article.slug,
        article=article.path,
        repo=article.repo.full_name(),
        status=status,
        recorded_sha=pin.sha if pin else None,
        recorded_note=pin.note if pin else "",
        default_branch=default_branch,
        upstream_sha=upstream_sha,
        ahead_by=ahead_by,
        behind_by=0,
        merge_commits=0,
        range=None,
        commits=(),
        categories=dict.fromkeys(CATEGORIES, 0),
        review_required=False,
        stale_claims=(),
        unresolved_anchors=(),
        also_stale=(),
        problems=problems,
    )


def _totals(rows: Sequence[ArticleDrift], defective: Sequence[DefectiveArticle]) -> dict[str, int]:
    return {
        "articles": len(rows) + len(defective),
        "drifted": sum(1 for row in rows if row.status == "drifted"),
        "up-to-date": sum(1 for row in rows if row.status == "up-to-date"),
        "not-checked": sum(1 for row in rows if row.status in UNCHECKED_STATUSES),
        "defective": len(defective),
    }


def _order_shas(commits: Sequence[ClassifiedCommit], shas: set[str]) -> tuple[str, ...]:
    return tuple(commit.commit.short for commit in commits if commit.commit.short in shas)


def _heading_line(article: Article, scope: str) -> int:
    for section in article.sections:
        if section.scope == scope:
            return section.line
    return 0


def _readme(repo_root: Path | None) -> Path | None:
    if repo_root is None:
        return None
    readme = repo_root / "README.md"
    return readme if readme.exists() else None


def _defect_heading(readme: Path) -> str:
    for section in parse_sections(readme.read_text(encoding="utf-8")):
        if scope_role(section.scope) == "defects" or "defect" in section.title.lower():
            return section.title
    return ""


def _run_url() -> str | None:
    run_id = os.environ.get("GITHUB_RUN_ID")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not run_id or not repo:
        return None
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    return f"{server}/{repo}/actions/runs/{run_id}"
