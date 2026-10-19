"""Article references resolved against the upstream tree at the pin.

Invariant: an `Anchor` exists only if its reference matched a real path in
`git ls-tree -r <pin>`; everything else is an `UnresolvedAnchor` carrying a reason. That
is what makes "which section is now stale" evidence - the join is between the commit's
changed paths and the paths the article itself cited - instead of a guess keyed on
headings.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .parse import Article, CodeMention, UpstreamLink, scope_at

AnchorKind = Literal["link", "code_ref", "glob", "dir", "root_readme"]
MatchKind = Literal["exact", "suffix", "dir", "glob", "root_exact"]
ResolutionStatus = Literal["resolved", "ambiguous", "not_found"]
Role = Literal[
    "glance",
    "problem",
    "capability",
    "stack",
    "architecture",
    "decisions",
    "running",
    "screenshots",
    "defects",
    "links",
    "unknown",
]

LINE_SUFFIX_RE = re.compile(r":(?P<line>\d+)(?:-\d+)?$")
EXTENSIONS = (
    "md|markdown|py|go|ts|tsx|js|mjs|cjs|json|jsonc|toml|yaml|yml|sh|bash|css|scss|html|sql|txt|cfg|ini|"
    "lock|mod|sum|puml|mmd|csv|png|jpg|jpeg|svg|svelte|vue|rs|java|proto|env|example"
)
BARE_FILE_RE = re.compile(rf"^[A-Za-z0-9_.-]+\.({EXTENSIONS})$")
PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9_./*{},+=#-]+$")


@dataclass(frozen=True)
class Resolution:
    """One pattern against the upstream file list.

    Invariant: `status == "resolved"` implies `paths` is non-empty, so no caller can build
    an anchor out of an empty match set.
    """

    status: ResolutionStatus
    match: MatchKind
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Anchor:
    """One article reference that resolved upstream, and can therefore be traced back from
    a commit.

    Invariant: `upstream_path` is never empty, is the full path when the reference was a
    unique suffix, and keeps the pattern when it was a glob, which is how a commit path is
    matched back to it.
    """

    scope: str
    text: str
    kind: AnchorKind
    upstream_path: str
    match: MatchKind
    line: int
    cited_line: int | None


@dataclass(frozen=True)
class UnresolvedAnchor:
    """A reference that looks like an upstream path but is not one at the pin.

    Reported, never dropped and never a failure: `results/*.csv` in trading-suite.md is the
    real instance today, and the honest answer is "this citation is untraceable".
    """

    scope: str
    text: str
    detail: str
    line: int


@dataclass(frozen=True)
class SectionClaim:
    """One row of the article-shape policy: which kind of upstream change makes which
    section stale, and the sentence a human reads.

    Invariant: `pattern` is matched against the article's own heading text, so this table
    encodes the article shape README.md documents rather than a list of articles.
    """

    pattern: str
    role: Role
    claim: str
    feeds: tuple[Literal["anchors", "manifests"], ...]


@dataclass(frozen=True)
class AnchorIndex:
    """Everything one article cites, resolved against one upstream tree at the pin."""

    anchors: tuple[Anchor, ...]
    unresolved: tuple[UnresolvedAnchor, ...]


SECTION_CLAIMS: tuple[SectionClaim, ...] = (
    SectionClaim(
        pattern=r"^at a glance$",
        role="glance",
        claim="the measured numbers this section quotes come from a run of an older commit",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^the problem",
        role="problem",
        claim="upstream touched a path this section cites",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^what it does$",
        role="capability",
        claim="the capability list may be missing what upstream added",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^why this stack$",
        role="stack",
        claim="the version columns quote dependencies, and the manifests moved",
        feeds=("anchors", "manifests"),
    ),
    SectionClaim(
        pattern=r"^architecture$",
        role="architecture",
        claim="a new or moved module changes the diagram this section draws",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^architecture decisions",
        role="decisions",
        claim="upstream touched a path this section defends a decision about",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^running it$",
        role="running",
        claim="the commands this section quotes may no longer be what upstream runs",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^screenshots$",
        role="screenshots",
        claim="upstream touched a path this section shows",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^what i'd fix next$",
        role="defects",
        claim="upstream touched a path this section calls defective - the defect may be fixed",
        feeds=("anchors",),
    ),
    SectionClaim(
        pattern=r"^links$",
        role="links",
        claim="a document this section links to changed upstream",
        feeds=("anchors",),
    ),
)

DEFAULT_CLAIM = SectionClaim(
    pattern="",
    role="unknown",
    claim="upstream touched a path this article cites",
    feeds=("anchors",),
)


def normalize_reference(text: str) -> tuple[str, MatchKind]:
    """Article spelling -> (upstream-path pattern, match kind).

    Invariant: the kind is decided by the shape alone - trailing "/" is a directory, an
    interior "/" is a suffix, a glob or brace is a glob, a bare filename is a root-level
    filename - so `app/plan/bars.py:319` is a suffix and `README.md` must match at the
    tree root.
    """
    value = text.strip().strip("`").strip()
    value = LINE_SUFFIX_RE.sub("", value).rstrip(".,;")
    if value.endswith("/"):
        return value.rstrip("/"), "dir"
    if "*" in value or "{" in value:
        return value, "glob"
    if "/" in value:
        return value, "suffix"
    return value, "root_exact"


def cited_line(text: str) -> int | None:
    """The upstream line number an article writes after a path, or None.

    Kept beside `normalize_reference` because the suffix it strips is the line the handoff
    tells the human to look at.
    """
    match = LINE_SUFFIX_RE.search(text.strip().strip("`"))
    return int(match.group("line")) if match is not None else None


def resolve_reference(pattern: str, kind: MatchKind, tree: Sequence[str]) -> Resolution:
    """One pattern against the upstream file list at the pin.

    Invariant: a suffix match takes the unique longest match and reports `ambiguous` on a
    tie rather than guessing; a directory pattern requires at least one file beneath it; a
    glob requires at least one match.
    """
    files = set(tree)
    if kind == "root_exact":
        if pattern in files:
            return Resolution("resolved", "root_exact", (pattern,))
        return Resolution("not_found", "root_exact", ())

    if kind == "glob":
        patterns = _expand_braces(pattern)
        hits = tuple(sorted({path for path in tree if _glob_hit(path, patterns)}))
        if hits:
            return Resolution("resolved", "glob", hits)
        return Resolution("not_found", "glob", ())

    segments = [segment for segment in pattern.split("/") if segment]

    if kind == "dir":
        return _resolve_prefixes(pattern, segments, tree, "dir")

    suffixes = [path for path in tree if path == pattern or path.endswith("/" + pattern)]
    if len(suffixes) == 1:
        match: MatchKind = "exact" if suffixes[0] == pattern else "suffix"
        return Resolution("resolved", match, (suffixes[0],))
    if len(suffixes) > 1:
        longest = sorted(suffixes, key=len, reverse=True)
        if len(longest[0]) == len(longest[1]):
            return Resolution("ambiguous", "suffix", tuple(longest))
        return Resolution("resolved", "suffix", (longest[0],))
    return _resolve_prefixes(pattern, segments, tree, "suffix")


def build_index(article: Article, tree: Sequence[str]) -> AnchorIndex:
    """Every reference an article makes, resolved against the upstream tree at the pin.

    Invariant: a bare repo URL with no fragment is deliberately not an anchor ("anything
    changed" is not evidence a human can act on), and a mention under the article's own
    diagram or capture namespace is article-local, not drift evidence.
    """
    tops = {path.split("/")[0] for path in tree}
    local = _local_namespaces(article)
    anchors: list[Anchor] = []
    unresolved: list[UnresolvedAnchor] = []
    seen: set[tuple[str, str]] = set()

    for link in article.links:
        candidate = _link_candidate(link)
        if candidate is None:
            continue
        text, pattern, kind, anchor_kind, line, cited = candidate
        _resolve_into(article, text, pattern, kind, anchor_kind, line, cited, tree, tops, local, seen, anchors, unresolved)

    for mention in article.mentions:
        candidate = _mention_candidate(mention, local)
        if candidate is None:
            continue
        text, pattern, kind, anchor_kind, line, cited = candidate
        _resolve_into(article, text, pattern, kind, anchor_kind, line, cited, tree, tops, local, seen, anchors, unresolved)

    return AnchorIndex(anchors=tuple(sorted(anchors, key=lambda a: (a.line, a.upstream_path))), unresolved=tuple(unresolved))


def scope_role(scope: str) -> Role:
    """A scope label -> its role in the article shape.

    Invariant: an unmatched heading is "unknown" and still gets the generic claim, because
    nothing is dropped for having an unfamiliar name.
    """
    base = scope.split(" › ")[0].strip().lower()
    for claim in SECTION_CLAIMS:
        if re.search(claim.pattern, base):
            return claim.role
    return "unknown"


def claim_for(role: Role) -> SectionClaim:
    """The table row for a role: known roles first, then the generic default row."""
    for claim in SECTION_CLAIMS:
        if claim.role == role:
            return claim
    return DEFAULT_CLAIM


def sections_touched(index: AnchorIndex, paths: Sequence[str]) -> tuple[Anchor, ...]:
    """Reverse the index: which article anchors do these upstream paths hit.

    Invariant: the result is deduplicated by (scope, upstream_path) and ordered by the
    reference's line in the article, so each touched section is claimed once and the
    report reads top to bottom like the article.
    """
    hits: dict[tuple[str, str], Anchor] = {}
    for anchor in index.anchors:
        if any(_anchor_hits(anchor, path) for path in paths):
            hits.setdefault((anchor.scope, anchor.upstream_path), anchor)
    return tuple(sorted(hits.values(), key=lambda a: (a.line, a.upstream_path)))


def _local_namespaces(article: Article) -> frozenset[str]:
    names: set[str] = set()
    candidates = [article.pin.diagram] if article.pin and article.pin.diagram else []
    if article.pin:
        candidates.extend(article.pin.captures)
    for path in candidates:
        if path:
            names.add(path.split("/")[0])
    return frozenset(names)


def _link_candidate(link: UpstreamLink) -> tuple[str, str, MatchKind, AnchorKind, int, int | None] | None:
    if link.kind == "repo":
        if "#" not in link.url:
            return None
        return link.url, "README.md", "root_exact", "root_readme", link.line, None
    if link.path is None:
        return None
    pattern, kind = normalize_reference(link.path)
    return link.path, pattern, kind, "link", link.line, cited_line(link.path)


def _mention_candidate(
    mention: CodeMention, local: frozenset[str]
) -> tuple[str, str, MatchKind, AnchorKind, int, int | None] | None:
    if not looks_like_path(mention.text):
        return None
    pattern, kind = normalize_reference(mention.text)
    if pattern.split("/")[0] in local:
        return None
    if kind == "glob":
        anchor_kind: AnchorKind = "glob"
    elif kind == "dir":
        anchor_kind = "dir"
    elif kind == "root_exact" and pattern == "README.md":
        anchor_kind = "root_readme"
    else:
        anchor_kind = "code_ref"
    return mention.text, pattern, kind, anchor_kind, mention.line, cited_line(mention.text)


def _resolve_into(
    article: Article,
    text: str,
    pattern: str,
    kind: MatchKind,
    anchor_kind: AnchorKind,
    line: int,
    cited: int | None,
    tree: Sequence[str],
    tops: set[str],
    local: frozenset[str],
    seen: set[tuple[str, str]],
    anchors: list[Anchor],
    unresolved: list[UnresolvedAnchor],
) -> None:
    resolution = resolve_reference(pattern, kind, tree)
    scope = _scope_for_line(article, line)
    if resolution.status == "resolved":
        upstream_path = pattern if resolution.match == "glob" else resolution.paths[0]
        key = (scope, upstream_path)
        if key in seen:
            return
        seen.add(key)
        anchors.append(
            Anchor(
                scope=scope,
                text=text,
                kind=anchor_kind,
                upstream_path=upstream_path,
                match=resolution.match,
                line=line,
                cited_line=cited,
            )
        )
        return
    if resolution.status == "not_found" and _not_a_path(pattern, tops):
        return
    detail = (
        f"no upstream path matches '{pattern}'"
        if resolution.status == "not_found"
        else f"'{pattern}' matches several upstream paths: {', '.join(resolution.paths[:3])}"
    )
    unresolved.append(UnresolvedAnchor(scope=scope, text=text, detail=detail, line=line))


def _scope_for_line(article: Article, line: int) -> str:
    return scope_at(article.sections, line)


def _anchor_hits(anchor: Anchor, path: str) -> bool:
    target = anchor.upstream_path
    if anchor.match == "root_exact":
        return path == target
    if anchor.match == "dir":
        return path.startswith(target + "/")
    if anchor.match == "glob":
        return _glob_hit(path, _expand_braces(target))
    return path == target or path.endswith("/" + target)


def _resolve_prefixes(pattern: str, segments: Sequence[str], tree: Sequence[str], kind: MatchKind) -> Resolution:
    prefixes: set[str] = set()
    for path in tree:
        found = _segment_run_prefix(path, segments)
        if found is not None and (kind == "suffix" or found[1] is True):
            prefixes.add(found[0])
    if not prefixes:
        return Resolution("not_found", kind, ())
    ordered = sorted(prefixes, key=len, reverse=True)
    if len(ordered) > 1 and len(ordered[0]) == len(ordered[1]):
        return Resolution("ambiguous", kind, tuple(ordered))
    return Resolution("resolved", kind, (ordered[0],))


def _segment_run_prefix(path: str, segments: Sequence[str]) -> tuple[str, bool] | None:
    """(`prefix`, `has_more`) for the first contiguous run of `segments` inside `path`.

    `has_more` is True when the path continues past the run, which is what makes the
    prefix a directory rather than a file that happens to end with the pattern.
    """
    parts = path.split("/")
    size = len(segments)
    for index in range(len(parts) - size + 1):
        if parts[index : index + size] == list(segments):
            return "/".join(parts[: index + size]), len(parts) > index + size
    return None


def _expand_braces(pattern: str) -> tuple[str, ...]:
    match = re.search(r"\{([^{}]*)\}", pattern)
    if match is None:
        return (pattern,)
    expanded: list[str] = []
    for choice in match.group(1).split(","):
        expanded.extend(_expand_braces(pattern[: match.start()] + choice + pattern[match.end() :]))
    return tuple(expanded)


def _glob_hit(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if not pattern.startswith("/") and fnmatch.fnmatchcase(path, "*/" + pattern):
            return True
    return False


def _not_a_path(pattern: str, tops: set[str]) -> bool:
    """Whether a token that matched nothing is structurally not a repository path.

    Invariant: it only fires when the token has no file extension and neither its first
    nor its last segment names a top-level entry, which is how HTTP routes, Go imports and
    package names stay out of the report while a real citation such as `results/*.csv`
    stays in it.
    """
    segments = pattern.strip("/").split("/")
    if any("." in segment for segment in segments[-1:]):
        return False
    return segments[0] not in tops and segments[-1] not in tops


def looks_like_path(text: str) -> bool:
    """Whether one inline code span names a repository path rather than a route, an import
    or a package.

    Invariant: only spans this accepts become anchors, and the fixture that has to create
    an upstream tree uses the same predicate, so there is one rule rather than two.
    """
    value = text.strip().strip("`").strip()
    if not value or len(value) > 120:
        return False
    if "://" in value or " " in value or "\t" in value or "\n" in value:
        return False
    base = LINE_SUFFIX_RE.sub("", value)
    if not base or base[0] in "/~@#.":
        return False
    if PATH_CHARS_RE.match(base) is None:
        return False
    if "/" in base:
        return True
    return BARE_FILE_RE.match(base) is not None
