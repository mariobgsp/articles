"""The article file format: discovery, headings, references and the closing pin.

Every fact the rest of the pipeline knows about an article is read here and nowhere
else, so there is one parse and many readers. Nothing in this module raises for content
defects: a file that cannot be read far enough to check is a `DefectiveArticle`, and a
defective closing anchor is an `AnchorProblem` on an otherwise usable `Article`, which
is what lets `validate` and `drift` keep reporting the other four articles.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RefKind = Literal["image", "link", "attr"]
LinkKind = Literal["blob", "tree", "repo", "release", "other"]
ProblemCode = Literal[
    "pin_no_closing_block",
    "pin_sha_unreadable",
    "pin_diagram_missing",
    "pin_diagram_slug_mismatch",
    "pin_no_capture",
]

# The `| **Repository** |` row is the only evidence an article is an article: there is no
# slug list anywhere in this package, and README.md falls out of the set because it has
# no such row.
REPOSITORY_ROW_RE = re.compile(r"^\|\s*\*\*Repository\*\*\s*\|(?P<cells>.*)\|\s*$", re.MULTILINE)
GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/\s#?]+)/(?P<name>[^/\s#?]+?)(?:\.git)?/?$"
)
GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/\s#?]+)/(?P<name>[^/\s#?]+)(?P<rest>[^\s]*)$"
)
INLINE_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(\s*([^)\s]+?)(?:\s+\"[^\"]*\")?\s*\)")
ATTRIBUTE_RE = re.compile(r"\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
SHA_RE = re.compile(r"`([0-9a-f]{7,40})`")
DIAGRAM_TARGET_RE = re.compile(r"^diagrams/(?P<slug>[A-Za-z0-9._-]+)\.mmd$")
CAPTURE_TARGET_RE = re.compile(r"^tools/capture/[^)\s]+$")


@dataclass(frozen=True)
class RepoRef:
    """The `| **Repository** |` row.

    Invariant: `owner` and `name` come from a two-segment `github.com/<owner>/<name>`
    URL, which is the only shape `git.remote_url` can turn into a clone URL; the row is
    rejected before construction otherwise.
    """

    owner: str
    name: str
    url: str

    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Section:
    """One `##` or `###` heading and the lines it owns.

    Invariant: `end_line` is the last line before the next heading at the same or a
    shallower level, so every line of the file belongs to a section or to none.
    """

    title: str
    scope: str
    level: int
    line: int
    end_line: int


@dataclass(frozen=True)
class UpstreamLink:
    """An absolute github.com link out of the article.

    `owner`/`name` are carried so `read_article` can keep only the links into the
    article's own repository; `path` is None when the link names no file (a release page,
    a bare repo URL), which is also how a repo-root link with a fragment arrives here.
    """

    url: str
    owner: str
    name: str
    kind: LinkKind
    ref: str | None
    path: str | None
    scope: str
    line: int


@dataclass(frozen=True)
class LocalRef:
    """A reference inside this repository: an image, a link, or an href/src attribute.

    `target` is the target as written, before fragment stripping, because the local
    checks need to tell a file reference from a `#anchor` reference.
    """

    target: str
    kind: RefKind
    scope: str
    line: int


@dataclass(frozen=True)
class CodeMention:
    """One inline code span, verbatim, with the section that contains it.

    Extracted here because the fence rules that keep a `#` inside a mermaid block from
    becoming a heading also decide what counts as an inline span; `anchor.py` decides
    which of these name a path.
    """

    text: str
    scope: str
    line: int


@dataclass(frozen=True)
class AnchorProblem:
    """A closing-pin defect. A `Pin` exists iff no problem carries "pin_sha_unreadable"."""

    code: ProblemCode
    line: int
    detail: str


@dataclass(frozen=True)
class Pin:
    """The closing verified-commit anchor, read out of the article.

    Grammar (all five articles satisfy it today): the last non-empty block of the file is
    a single-emphasis paragraph, it contains a backticked lowercase hex sha of 7..40
    chars - optionally followed by a parenthetical note - and it links
    `diagrams/<slug>.mmd` and at least one `tools/capture/*` path. `sha` is kept exactly
    as written; only `git.Remote.resolve` turns it into 40 characters.
    """

    sha: str
    note: str
    line: int
    diagram: str | None
    captures: tuple[str, ...]
    block: str


@dataclass(frozen=True)
class Article:
    """One well-formed article read out of one file.

    Every field is a fact observed in the file. `pin is None` iff some problem carries
    "pin_sha_unreadable", which is the only condition that makes the article
    undriftable; the other pin problems leave a usable `pin` behind.
    """

    slug: str
    path: str
    title: str
    repo: RepoRef
    pin: Pin | None
    sections: tuple[Section, ...]
    links: tuple[UpstreamLink, ...]
    local_refs: tuple[LocalRef, ...]
    mentions: tuple[CodeMention, ...]
    problems: tuple[AnchorProblem, ...]


@dataclass(frozen=True)
class DefectiveArticle:
    """A root markdown file that announces itself as an article but cannot be read.

    Invariant: it is never dropped from a report; the alternative is an article silently
    missing from both halves the moment its header table is edited.
    """

    slug: str
    path: str
    detail: str


ArticleFile = Article | DefectiveArticle


@dataclass(frozen=True)
class Inventory:
    """What `discover` found: the derived article set, plus the two lists it did not treat
    as articles so a report can name them instead of staying silent about them."""

    articles: tuple[Article, ...]
    defective: tuple[DefectiveArticle, ...]
    other_root_markdown: tuple[str, ...]


def slug_for_title(text: str) -> str:
    """The GitHub heading slug for one heading text.

    Invariant: this is the same slugger GitHub uses for `#anchor` targets (lowercase,
    punctuation dropped, spaces to hyphens), because `validate` fails an anchor that
    GitHub would not resolve.
    """
    lowered = text.strip().lower()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch in " -_")
    return kept.replace(" ", "-")


def heading_slugs(title: str, sections: Sequence[Section]) -> frozenset[str]:
    """Every heading slug an article offers as an anchor target."""
    slugs = {slug_for_title(title)} if title else set()
    slugs.update(slug_for_title(section.title) for section in sections)
    return frozenset(slug for slug in slugs if slug)


def discover(repo_root: Path) -> Inventory:
    """Every root `*.md`, classified by evidence rather than by filename.

    Invariant: an article is exactly a root markdown file whose header table carries a
    `| **Repository** |` row pointing at a two-segment github.com URL; everything else at
    the root is named in `other_root_markdown`. There is no slug list in this package, in
    the workflows, or in CI.
    """
    articles: list[Article] = []
    defective: list[DefectiveArticle] = []
    other: list[str] = []
    for path in sorted(repo_root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not REPOSITORY_ROW_RE.search(text):
            other.append(path.name)
            continue
        read = read_article(path)
        if isinstance(read, Article):
            articles.append(read)
        else:
            defective.append(read)
    return Inventory(articles=tuple(articles), defective=tuple(defective), other_root_markdown=tuple(other))


def read_article(path: Path, slug: str | None = None) -> ArticleFile:
    """Read one file into an `Article` or a `DefectiveArticle`.

    Invariant: a missing closing pin or an unreadable sha is a problem on the returned
    `Article`, never an exception, so one bad file cannot stop the other four from being
    validated or reported. The repository URL is checked first, because without it the
    file cannot be drifted at all.
    """
    slug = slug or path.stem
    text = path.read_text(encoding="utf-8")
    repo = repo_ref(path, text)
    if isinstance(repo, str):
        return DefectiveArticle(slug=slug, path=path.name, detail=repo)

    sections = parse_sections(text)
    links, local_refs = parse_refs(text, sections)
    own_links = tuple(link for link in links if link.owner == repo.owner and link.name == repo.name)
    pin, problems = parse_pin(text)
    if pin is not None and pin.diagram is not None and Path(pin.diagram).name != f"{slug}.mmd":
        problems += (
            AnchorProblem(
                code="pin_diagram_slug_mismatch",
                line=pin.line,
                detail=f"the closing paragraph links {pin.diagram}, which is not diagrams/{slug}.mmd",
            ),
        )
    return Article(
        slug=slug,
        path=path.name,
        title=_title(text),
        repo=repo,
        pin=pin,
        sections=sections,
        links=own_links,
        local_refs=local_refs,
        mentions=_mentions(text, sections),
        problems=problems,
    )


def repo_ref(article_path: Path, text: str) -> RepoRef | str:
    """The `| **Repository** |` row as a `RepoRef`, or a one-line reason it is unusable.

    Invariant: the returned URL has exactly two path segments, so no later stage has to
    guess whether a link pointed at a repository, a branch or a file.
    """
    row = REPOSITORY_ROW_RE.search(text)
    if row is None:
        return f"{article_path.name} has no `| **Repository** |` row in its header table"
    link = INLINE_LINK_RE.search(row.group("cells"))
    if link is None:
        return f"{article_path.name}: the `| **Repository** |` row is not a link"
    target = link.group(3)
    match = GITHUB_REPO_RE.match(target)
    if match is None:
        return f"{article_path.name}: `| **Repository** |` does not name a two-segment github.com repository ({target})"
    return RepoRef(owner=match.group("owner"), name=match.group("name"), url=target)


def parse_pin(text: str) -> tuple[Pin | None, tuple[AnchorProblem, ...]]:
    """The last block of the file -> (Pin, problems).

    Invariant: `Pin is None` iff the closing block yields no readable sha - the block is
    missing or not a single-emphasis paragraph (`pin_no_closing_block`), or it carries no sha
    (`pin_sha_unreadable`). A pin can therefore exist together with problems, which is how a
    closing note that lost its diagram or capture link is reported. Handles all five wordings
    in this repository, including a closing paragraph that wraps over several lines and a
    parenthetical that carries prose instead of a date.
    """
    lines = text.splitlines()
    last = _last_non_empty(lines)
    if last is None:
        return None, (AnchorProblem("pin_no_closing_block", 1, "the file is empty"),)
    start = last
    while start > 0 and lines[start - 1].strip():
        start -= 1
    block = "\n".join(lines[start : last + 1]).strip()
    if not (block.startswith("*") and block.endswith("*") and not block.startswith("**")):
        return None, (
            AnchorProblem(
                code="pin_no_closing_block",
                line=last,
                detail="the closing paragraph is not a single-emphasis paragraph, so the verified commit cannot be read",
            ),
        )

    sha_match = SHA_RE.search(block)
    if sha_match is None:
        return None, (
            AnchorProblem(
                code="pin_sha_unreadable",
                line=start,
                detail="the closing paragraph has no backticked 7..40 character lowercase hex commit",
            ),
        )
    sha = sha_match.group(1)
    note = ""
    note_match = re.match(r"\s*\(([^)]*)\)", block[sha_match.end() :])
    if note_match is not None:
        note = note_match.group(1).strip()

    diagram: str | None = None
    captures: list[str] = []
    for match in INLINE_LINK_RE.finditer(block):
        target = match.group(3)
        if diagram is None and DIAGRAM_TARGET_RE.match(target):
            diagram = target
        elif CAPTURE_TARGET_RE.match(target):
            captures.append(target)

    problems: list[AnchorProblem] = []
    if diagram is None:
        problems.append(
            AnchorProblem(
                code="pin_diagram_missing",
                line=start,
                detail="no `diagrams/<slug>.mmd` link in the closing paragraph",
            )
        )
    if not captures:
        problems.append(
            AnchorProblem(
                code="pin_no_capture",
                line=start,
                detail="no `tools/capture/*` link in the closing paragraph",
            )
        )
    pin = Pin(
        sha=sha,
        note=note,
        line=start,
        diagram=diagram,
        captures=tuple(captures),
        block=block,
    )
    return pin, tuple(problems)


def parse_sections(text: str) -> tuple[Section, ...]:
    """`##` and `###` headings with their line ranges.

    Invariant: fenced code blocks are skipped, so a `#` inside a mermaid diagram is never
    a heading, and `scope` is the enclosing `##` title plus ` › <### title>` for a
    sub-heading, which is the label `drift` reports.
    """
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for number, line in enumerate(_with_fences_blanked(lines), start=1):
        match = HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        if level not in (2, 3):
            continue
        headings.append((number, level, match.group(2).strip()))

    sections: list[Section] = []
    parent = ""
    for index, (number, level, title) in enumerate(headings):
        end = len(lines)
        for other_number, other_level, _ in headings[index + 1 :]:
            if other_level <= level:
                end = other_number - 1
                break
        if level == 2:
            parent = title
            scope = title
        else:
            scope = f"{parent} › {title}" if parent else title
        sections.append(Section(title=title, scope=scope, level=level, line=number, end_line=end))
    return tuple(sections)


def parse_refs(text: str, sections: Sequence[Section]) -> tuple[tuple[UpstreamLink, ...], tuple[LocalRef, ...]]:
    """Markdown links, images and raw href/src attributes, each with its section.

    Invariant: a target with a scheme is either a github.com `UpstreamLink` (carrying
    owner/name so the caller can keep only the article's own repository) or is dropped;
    every other target is a `LocalRef`, including `#anchor` targets, which the local
    checks resolve against the file's own headings.
    """
    upstream: list[UpstreamLink] = []
    local: list[LocalRef] = []
    for number, line in enumerate(_unfenced_lines(text), start=1):
        scope = scope_at(sections, number)
        for match in INLINE_LINK_RE.finditer(line):
            target = match.group(3)
            link = _upstream_link(target, "image" if match.group(1) else "link", scope, number)
            if link is not None:
                upstream.append(link)
            elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                local.append(LocalRef(target=target, kind="image" if match.group(1) else "link", scope=scope, line=number))
        for match in ATTRIBUTE_RE.finditer(line):
            target = match.group(1)
            link = _upstream_link(target, "attr", scope, number)
            if link is not None:
                upstream.append(link)
            elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                local.append(LocalRef(target=target, kind="attr", scope=scope, line=number))
    return tuple(upstream), tuple(local)


def scope_at(sections: Sequence[Section], line: int) -> str:
    """The scope of the deepest section containing `line`, or "" outside every section.

    Invariant: a line inside a `###` reports the sub-heading, never its parent, because
    the report names the section a human has to open.
    """
    best: Section | None = None
    for section in sections:
        if section.line <= line <= section.end_line and (best is None or section.level > best.level):
            best = section
    return best.scope if best is not None else ""


def _upstream_link(target: str, kind: str, scope: str, line: int) -> UpstreamLink | None:
    match = GITHUB_URL_RE.match(target)
    if match is None:
        return None
    owner = match.group("owner")
    name = match.group("name")
    rest = match.group("rest") or ""
    blob = re.match(r"^/(?:blob|tree)/(?P<ref>[^/\s#?]+)/(?P<path>[^\s#?]+)", rest)
    if blob is not None:
        path = blob.group("path").rstrip("/")
        return UpstreamLink(
            url=target,
            owner=owner,
            name=name,
            kind="tree" if rest.startswith("/tree/") else "blob",
            ref=blob.group("ref"),
            path=path or None,
            scope=scope,
            line=line,
        )
    if rest.startswith(("/releases", "/issues", "/pulls")):
        return UpstreamLink(url=target, owner=owner, name=name, kind="other", ref=None, path=None, scope=scope, line=line)
    return UpstreamLink(url=target, owner=owner, name=name, kind="repo", ref=None, path=None, scope=scope, line=line)


def _mentions(text: str, sections: Sequence[Section]) -> tuple[CodeMention, ...]:
    mentions: list[CodeMention] = []
    for number, line in enumerate(_unfenced_lines(text), start=1):
        scope = scope_at(sections, number)
        for match in INLINE_CODE_RE.finditer(line):
            mentions.append(CodeMention(text=match.group(1), scope=scope, line=number))
    return tuple(mentions)


def _title(text: str) -> str:
    for line in _unfenced_lines(text):
        match = HEADING_RE.match(line)
        if match is not None and len(match.group(1)) == 1:
            return match.group(2).strip()
    return ""


def _with_fences_blanked(lines: Sequence[str]) -> list[str]:
    blanked: list[str] = []
    fenced = False
    for line in lines:
        if FENCE_RE.match(line):
            fenced = not fenced
            blanked.append("")
            continue
        blanked.append("" if fenced else line)
    return blanked


def _unfenced_lines(text: str) -> list[str]:
    return _with_fences_blanked(text.splitlines())


def _last_non_empty(lines: Sequence[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            return index
    return None
