"""Commit -> category, by evidence over three channels.

Invariant: a conventional prefix never decides a category on its own. Path evidence wins
over message evidence, the prefix is recorded as evidence, and a prefix that disagrees
with the winning reading is written into `conflicts` and costs one step of confidence. A
commit nothing fires on is `unclear` with confidence `low`, which is a reportable result
and the only honest one - it is also what raises `review_required` on the whole run.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .anchor import Anchor, AnchorIndex, sections_touched
from .git import CommitFact

Category = Literal["new feature", "fix", "docs", "deps", "other", "unclear"]
Confidence = Literal["high", "medium", "low"]
DecidedBy = Literal["paths", "message", "body", "none"]

CATEGORIES: tuple[Category, ...] = ("new feature", "fix", "docs", "deps", "other", "unclear")
CONFIDENCE_STEPS: tuple[Confidence, ...] = ("low", "medium", "high")

MANIFEST_GLOBS: tuple[str, ...] = (
    "go.mod",
    "go.sum",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "requirements*.txt",
    "poetry.lock",
    "Cargo.toml",
    "Cargo.lock",
    "*/Dockerfile",
    "*/docker-compose*.yml",
    "docker-compose*.yml",
    ".nvmrc",
    "mise.toml",
)

PREFIX_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<breaking>!)?:\s*(?P<rest>.*)$")
DOC_SUFFIXES = (".md", ".markdown", ".puml", ".mmd", ".rst", ".txt")
DOC_NAMES = ("README", "CHANGELOG", "CONTRIBUTING", "LICENSE", "NOTICE", "AGENTS", "CLAUDE")

FEATURE_TOKENS: tuple[str, ...] = (
    "add",
    "adds",
    "added",
    "introduce",
    "introduces",
    "introduced",
    "implement",
    "implements",
    "implemented",
    "support",
    "supports",
    "supported",
    "new",
    "enable",
    "enables",
    "expose",
    "exposes",
    "create",
    "creates",
    "ship",
    "ships",
    "wire",
    "wires",
    "integrate",
    "integrates",
)
FIX_TOKENS: tuple[str, ...] = (
    "fix",
    "fixes",
    "fixed",
    "repair",
    "repairs",
    "harden",
    "hardens",
    "correct",
    "corrects",
    "handle",
    "handles",
    "recover",
    "recovers",
    "again",
    "no-longer",
    "no longer",
    "workaround",
    "fail",
    "fails",
    "failed",
    "guard",
    "guards",
    "regression",
    "bug",
    "race",
    "leak",
    "crash",
    "flaky",
)
DEP_TOKENS: tuple[str, ...] = ("bump", "bumps", "upgrade", "upgrades", "dependabot", "renovate", "downgrade")
DOC_TOKENS: tuple[str, ...] = ("doc", "docs", "document", "readme", "changelog", "typo", "wording", "comment")
OTHER_TOKENS: tuple[str, ...] = (
    "chore",
    "refactor",
    "refactors",
    "simplify",
    "rename",
    "renames",
    "move",
    "moves",
    "cleanup",
    "tidy",
    "format",
    "release",
    "lint",
    "test",
    "tests",
)
BODY_TOKENS: tuple[tuple[str, Category], ...] = (
    ("BREAKING CHANGE", "new feature"),
    ("revert", "other"),
    ("fixes #", "fix"),
)

PREFIX_CATEGORIES: dict[str, Category] = {
    "feat": "new feature",
    "feature": "new feature",
    "fix": "fix",
    "hotfix": "fix",
    "docs": "docs",
    "doc": "docs",
    "deps": "deps",
    "build": "deps",
    "chore": "other",
    "refactor": "other",
    "test": "other",
    "ci": "other",
    "style": "other",
    "perf": "other",
    "release": "other",
}

CI_PREFIXES = (".github/",)


@dataclass(frozen=True)
class ClassifiedCommit:
    """A commit plus the category, the confidence, the conflicts and the evidence.

    Invariant: `evidence` is never empty and `confidence == "low"` iff nothing fired on
    any channel, which is exactly the case that flags the run for review.
    """

    commit: CommitFact
    category: Category
    confidence: Confidence
    decided_by: DecidedBy
    conflicts: tuple[str, ...]
    evidence: tuple[str, ...]
    touches: tuple[Anchor, ...]


def is_manifest(path: str) -> bool:
    """Dependency manifest or lockfile, at any depth."""
    if any(fnmatch.fnmatch(path, glob) for glob in MANIFEST_GLOBS):
        return True
    name = path.rsplit("/", 1)[-1]
    if fnmatch.fnmatch(name, "requirements*.txt") or name in {"Dockerfile", "Cargo.toml", "go.mod"}:
        return True
    return name.startswith("docker-compose") and name.endswith((".yml", ".yaml"))


def is_docs(path: str) -> bool:
    """`docs/**`, `specs/**`, any `*.md`, `*.puml`, `LICENSE`, `CHANGELOG.md`."""
    lowered = path.lower()
    if lowered.startswith(("docs/", "specs/", "doc/", "documentation/")):
        return True
    name = path.rsplit("/", 1)[-1]
    if lowered.endswith(DOC_SUFFIXES):
        return True
    return name.split(".")[0].upper() in DOC_NAMES


def is_test(path: str) -> bool:
    """`*_test.go`, `test_*.py`, `*.test.ts`, `tests/**`."""
    name = path.rsplit("/", 1)[-1]
    if path.startswith(("tests/", "test/")) or "/tests/" in path or "/test/" in path:
        return True
    return (
        name.endswith(("_test.go", "_test.py", ".test.ts", ".spec.ts"))
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def is_ci(path: str) -> bool:
    """Anything under `.github/`, which is pipeline configuration rather than product code."""
    return path.startswith(CI_PREFIXES)


def manifest_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """The touched manifests, in tree order, for the stack claim and the deps category."""
    return tuple(path for path in paths if is_manifest(path))


def classify(commit: CommitFact, index: AnchorIndex) -> ClassifiedCommit:
    """Category by evidence, never by prefix alone.

    Invariant: the reading that produced the category is always named - `decided_by` says
    which channel won, `evidence` quotes what fired, and a disagreeing prefix is in
    `conflicts` rather than silently outvoted.
    """
    evidence: list[str] = []
    conflicts: list[str] = []
    paths = commit.paths
    manifests = manifest_paths(paths)
    code = tuple(path for path in paths if not (is_docs(path) or is_manifest(path) or is_test(path) or is_ci(path)))
    added_code = tuple(path for path in commit.added if path in set(code))
    touches = sections_touched(index, paths)

    path_category: Category | None = None
    if paths:
        if all(is_docs(path) for path in paths):
            path_category = "docs"
            evidence.append(f"paths:{len(paths)} changed, all docs")
        elif manifests and not code:
            path_category = "deps"
            evidence.append(f"paths:{', '.join(manifests)}")
        elif all(is_test(path) for path in paths):
            path_category = "other"
            evidence.append(f"paths:{len(paths)} changed, all tests")
        elif added_code:
            path_category = "new feature"
            evidence.append(f"paths:{len(added_code)} added under {_common_dir(added_code)}")
        elif touches:
            path_category = "fix"
            evidence.append(f"anchor:{touches[0].upstream_path}")
        elif all(is_ci(path) for path in paths):
            path_category = "other"
            evidence.append(f"paths:{len(paths)} changed, all workflow files")

    prefix_category, prefix_token = _prefix_category(commit.subject)
    message_category, message_token = _message_category(commit.subject)
    body_category, body_token = _body_category(commit.body)

    if path_category is not None:
        category, decided_by, confidence = path_category, "paths", "high"
    elif message_category is not None:
        category, decided_by, confidence = message_category, "message", "medium"
    elif body_category is not None:
        category, decided_by, confidence = body_category, "body", "medium"
    else:
        category, decided_by, confidence = "unclear", "none", "low"

    if prefix_token is not None:
        evidence.append(f"prefix:{prefix_token}")
    if prefix_category is not None and prefix_category != category:
        conflicts.append(f"prefix {prefix_category} disagrees with {decided_by} {category}")
        confidence = _drop(confidence)
    elif message_token is not None and message_category is not None and message_category != category:
        conflicts.append(f"message {message_category} disagrees with {decided_by} {category}")
        confidence = _drop(confidence)
    if message_token is not None:
        evidence.append(f"message:{message_token}")
    if body_token is not None:
        evidence.append(f"body:{body_token}")
    if not evidence:
        evidence.append("no signal in subject, paths or body")

    return ClassifiedCommit(
        commit=commit,
        category=category,
        confidence=confidence,
        decided_by=decided_by,
        conflicts=tuple(conflicts),
        evidence=tuple(evidence),
        touches=touches,
    )


def summarize(commits: Sequence[ClassifiedCommit]) -> dict[Category, int]:
    """All six categories present, zeros included, in `CATEGORIES` order."""
    counts: dict[Category, int] = dict.fromkeys(CATEGORIES, 0)
    for classified in commits:
        counts[classified.category] += 1
    return counts


def _prefix_category(subject: str) -> tuple[Category | None, str | None]:
    match = PREFIX_RE.match(subject.strip())
    if match is None:
        return None, None
    token = match.group("type").lower()
    return PREFIX_CATEGORIES.get(token), token


def _message_category(subject: str) -> tuple[Category | None, str | None]:
    stripped = PREFIX_RE.sub(r"\g<rest>", subject.strip()) if PREFIX_RE.match(subject.strip()) else subject
    words = re.findall(r"[a-z0-9][a-z0-9-]*", stripped.lower())
    joined = " ".join(words)
    for token in DEP_TOKENS:
        if token in words:
            return "deps", token
    for word in FIX_TOKENS:
        if word in words or (word == "no longer" and "no longer" in joined):
            return "fix", word
    for token in FEATURE_TOKENS:
        if token in words:
            return "new feature", token
    for token in DOC_TOKENS:
        if token in words:
            return "docs", token
    for token in OTHER_TOKENS:
        if token in words:
            return "other", token
    return None, None


def _body_category(body: str) -> tuple[Category | None, str | None]:
    for token, category in BODY_TOKENS:
        if token.lower() in body.lower():
            return category, token
    return None, None


def _common_dir(paths: Sequence[str]) -> str:
    """The directory the added paths share, or the first path's directory."""
    directories = [path.rsplit("/", 1)[0] if "/" in path else path for path in paths]
    shared = directories[0]
    for directory in directories[1:]:
        parts = shared.split("/")
        other = directory.split("/")
        prefix: list[str] = []
        for left, right in zip(parts, other, strict=False):
            if left != right:
                break
            prefix.append(left)
        shared = "/".join(prefix) or shared
    return shared


def _drop(confidence: Confidence) -> Confidence:
    index = CONFIDENCE_STEPS.index(confidence)
    return CONFIDENCE_STEPS[max(0, index - 1)]
