"""Half 1: the checks that run on every push and every pull request.

Invariant: this module is pure over the filesystem. It may start a parser (`node --check`,
`bash -n`, the markdown linter) but it never touches the network, never writes anywhere
inside the repository, and never edits prose - the only write in the whole pipeline is the
drift issue body, in `issues.py`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from .git import tracked_files
from .parse import (
    Article,
    DefectiveArticle,
    Inventory,
    discover,
    heading_slugs,
    parse_refs,
    parse_sections,
)

ValidateCode = Literal[
    "markdownlint",
    "pin",
    "local-ref",
    "local-anchor",
    "diagram",
    "capture",
    "readme-index",
    "readme-command",
]

CHECK_ORDER: tuple[ValidateCode, ...] = (
    "markdownlint",
    "pin",
    "local-ref",
    "local-anchor",
    "diagram",
    "capture",
    "readme-index",
    "readme-command",
)

MARKDOWNLINT_DETAIL_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::\d+)?\s+(?P<detail>MD\d+/\S+.*)$")
CAPTURE_PY = ".py"
CAPTURE_SH = ".sh"
CAPTURE_MJS = ".mjs"
RENDER_THEMES = ("light", "dark")
RENDERER = Path(__file__).resolve().parents[2] / "tools" / "render-diagrams.py"
NPM_RUN_RE = re.compile(r"^[ \t]*npm run ([A-Za-z0-9:_-]+)", re.MULTILINE)


@dataclass(frozen=True)
class Problem:
    """One finding, in the form GitHub renders as an annotation on the PR's files view.

    Invariant: `path` is repository-relative and `line` is a real line in that file, so
    `cli` can print `::error file=...,line=...::` without a second lookup.
    """

    code: ValidateCode
    path: str
    line: int
    detail: str


def validate_repo(repo_root: Path, *, inventory: Inventory | None = None) -> tuple[Problem, ...]:
    """Run the six checks over the repository and return every problem found.

    Invariant: the check order is fixed and the article order is the file order, so two
    runs produce byte-identical output and a diff between runs means something changed.
    A content defect is never an exception: an unreadable article is reported and the rest
    of the repository is still checked.
    """
    resolved: Inventory = inventory if inventory is not None else discover(repo_root)
    tracked = tracked_files(repo_root)
    articles = resolved.articles

    problems: list[Problem] = []
    problems.extend(check_markdownlint(repo_root, articles))
    for article in articles:
        problems.extend(check_pin(article, tracked))
    for article in articles:
        problems.extend(check_local_refs(article, tracked))
    problems.extend(check_diagrams(repo_root, tracked))
    problems.extend(check_capture_scripts(repo_root))
    problems.extend(check_readme_index(repo_root, articles))
    problems.extend(check_readme_commands(repo_root))
    problems.extend(_defective(resolved.defective))
    return tuple(problems)


def check_markdownlint(repo_root: Path, articles: Sequence[Article]) -> tuple[Problem, ...]:
    """The repository's own markdownlint config over the derived article list plus README.

    Invariant: the file list is the derived inventory, never a second hand-kept list, and
    a missing linter is one clear problem telling the human to run `npm ci` instead of a
    silent pass.
    """
    binary = repo_root / "node_modules" / ".bin" / "markdownlint-cli2"
    files = [article.path for article in articles]
    readme = repo_root / "README.md"
    if readme.exists():
        files.append(readme.name)
    if not files:
        return ()
    if not binary.exists():
        return (
            Problem(
                code="markdownlint",
                path=files[0],
                line=1,
                detail=f"{binary.relative_to(repo_root)} is missing; run `npm ci`",
            ),
        )
    argv = [str(binary), "--config", ".markdownlint.json", *files]
    proc = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True, check=False)
    # v0.18.1 prints findings on stderr and its own progress on stdout; read both.
    problems: list[Problem] = []
    for line in (*proc.stdout.splitlines(), *proc.stderr.splitlines()):
        match = MARKDOWNLINT_DETAIL_RE.match(line.strip())
        if match is None:
            continue
        problems.append(
            Problem(
                code="markdownlint",
                path=match.group("path"),
                line=max(1, int(match.group("line"))),
                detail=match.group("detail").strip(),
            )
        )
    if proc.returncode not in (0, 1) and not problems:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        problems.append(
            Problem(
                code="markdownlint",
                path=files[0],
                line=1,
                detail=f"markdownlint-cli2 exited {proc.returncode}: {detail[-1] if detail else 'no output'}",
            )
        )
    return tuple(problems)


def check_pin(article: Article, tracked: frozenset[str]) -> tuple[Problem, ...]:
    """The closing anchor: present, last block, well formed, and pointing at tracked files.

    Invariant: the closing paragraph is the only structured data in the repository, so it
    is checked here exactly once and a defective one keeps every other fact about the
    article usable. The tracked set is passed in because presence on disk is not the test.
    """
    problems = [
        Problem(code="pin", path=article.path, line=max(1, problem.line), detail=f"pin: {problem.detail}")
        for problem in article.problems
    ]
    pin = article.pin
    if pin is None:
        return tuple(problems)
    for path in (pin.diagram, *pin.captures):
        if path is None:
            continue
        if path.strip("/") not in tracked:
            problems.append(
                Problem(
                    code="pin",
                    path=article.path,
                    line=max(1, pin.line),
                    detail=f"pin: {path} is not tracked by git",
                )
            )
    return tuple(problems)


def check_local_refs(article: Article, tracked: frozenset[str]) -> tuple[Problem, ...]:
    """Every relative link and image resolves, and every `#anchor` names a real heading.

    Invariant: a reference is checked against the set of files git tracks, not against the
    working tree, because an untracked image is an image the published site never gets.
    """
    problems: list[Problem] = []
    slugs = heading_slugs(article.title, article.sections)
    for ref in article.local_refs:
        if ref.target.startswith("#"):
            anchor = ref.target.removeprefix("#").lower()
            if anchor and anchor not in slugs:
                problems.append(
                    Problem(
                        code="local-anchor",
                        path=article.path,
                        line=ref.line,
                        detail=f"local anchor: {ref.target} matches no heading in {article.path}",
                    )
                )
            continue
        target = unquote(ref.target.split("#", 1)[0].split("?", 1)[0]).strip()
        if not target:
            continue
        if target.strip("/") not in tracked:
            problems.append(
                Problem(
                    code="local-ref",
                    path=article.path,
                    line=ref.line,
                    detail=f"local ref: {ref.target} is not tracked by git",
                )
            )
    return tuple(problems)


def check_readme_index(repo_root: Path, articles: Sequence[Article]) -> tuple[Problem, ...]:
    """README.md's index and the derived inventory agree, both directions, once each.

    Invariant: this is the one place the article set is written twice - README needs its
    own list for readers - so the two copies are checked against each other instead of
    being kept in sync by discipline.
    """
    readme = repo_root / "README.md"
    if not readme.exists():
        return ()
    text = readme.read_text(encoding="utf-8")
    _, refs = parse_refs(text, parse_sections(text))
    linked: dict[str, list[int]] = {}
    for ref in refs:
        target = unquote(ref.target.split("#", 1)[0])
        if "/" in target or not target.endswith(".md"):
            continue
        linked.setdefault(target, []).append(ref.line)

    paths = {article.path for article in articles}
    problems: list[Problem] = []
    for article in articles:
        lines = linked.get(article.path, [])
        if not lines:
            problems.append(
                Problem(
                    code="readme-index",
                    path=readme.name,
                    line=1,
                    detail=f"readme index: {article.path} is not linked from README.md",
                )
            )
        elif len(lines) > 1:
            problems.append(
                Problem(
                    code="readme-index",
                    path=readme.name,
                    line=lines[1],
                    detail=f"readme index: {article.path} is linked {len(lines)} times in README.md",
                )
            )
    for target, lines in sorted(linked.items()):
        if target not in paths and target != readme.name:
            problems.append(
                Problem(
                    code="readme-index",
                    path=readme.name,
                    line=lines[0],
                    detail=f"readme index: README.md links {target}, which is not an article",
                )
            )
    return tuple(problems)


def check_readme_commands(repo_root: Path) -> tuple[Problem, ...]:
    """Every `npm run <script>` README tells a reader to run is a script `package.json` defines.

    Invariant: only README.md is read, because README is the one document that describes this
    repository's own commands - the articles quote other repositories' package.json - and only
    a line that *starts* with the command counts, so a trailing note such as `# needs npm run
    dev in that repo` is prose rather than a command. This is the defect the check was written
    for: README documented `npm run capture:durable-agents` while `package.json` defined only
    four `capture:*` scripts.
    """
    manifest = repo_root / "package.json"
    readme = repo_root / "README.md"
    if not manifest.exists() or not readme.exists():
        return ()
    try:
        scripts = json.loads(manifest.read_text(encoding="utf-8"))["scripts"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return (
            Problem(
                code="readme-command",
                path="package.json",
                line=1,
                detail=f"readme command: package.json is unreadable: {error}",
            ),
        )
    if not isinstance(scripts, dict):
        return ()
    known = set(scripts)
    text = readme.read_text(encoding="utf-8")

    problems: list[Problem] = []
    for match in NPM_RUN_RE.finditer(text):
        script = match.group(1)
        if script in known:
            continue
        line = text.count("\n", 0, match.start()) + 1
        problems.append(
            Problem(
                code="readme-command",
                path=readme.name,
                line=line,
                detail=f"readme command: `npm run {script}` is not a script in package.json",
            )
        )
    return tuple(problems)


def image_path(stem: str, theme: str) -> str:
    """One render's repository-relative path, taken from the renderer that produces it.

    Invariant: `images/<stem>-architecture[-dark].png` has exactly one owner, the renderer,
    so this check and the selftest read it there rather than restating the pattern. The file
    is loaded by path because `render-diagrams.py` is not a valid module name.
    """
    spec = importlib.util.spec_from_file_location("articles_render_diagrams", RENDERER)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {RENDERER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = RENDERER.parents[1]
    return module.output_path(stem, theme).relative_to(root).as_posix()


def check_diagrams(repo_root: Path, tracked: frozenset[str]) -> tuple[Problem, ...]:
    """Every `diagrams/*.mmd` has both of its renders under `images/`.

    Invariant: the set is derived from the directory listing, so a sixth article's diagram
    is covered the moment its source and renders land, with no list to update, and every
    file name comes from the renderer instead of from a copy of the renderer's rule.
    """
    problems: list[Problem] = []
    try:
        for source in sorted((repo_root / "diagrams").glob("*.mmd")):
            relative = f"diagrams/{source.name}"
            for theme in RENDER_THEMES:
                image = image_path(source.stem, theme)
                path = repo_root / image
                if not path.exists():
                    detail = f"diagram: {image} is missing"
                elif path.stat().st_size == 0:
                    detail = f"diagram: {image} is empty"
                elif image not in tracked:
                    detail = f"diagram: {image} is not tracked by git"
                else:
                    continue
                problems.append(Problem(code="diagram", path=relative, line=1, detail=detail))
    except (ImportError, AttributeError, OSError, ValueError) as error:
        return (
            Problem(
                code="diagram",
                path="tools/render-diagrams.py",
                line=1,
                detail=f"diagram: the renderer's naming rule could not be read: {error}",
            ),
        )
    return tuple(problems)


def check_capture_scripts(repo_root: Path) -> tuple[Problem, ...]:
    """Every `tools/capture/*` still parses with its own toolchain.

    Invariant: the set is derived from the directory listing and the command is chosen by
    suffix; `.py` is parsed rather than imported, so a check never writes bytecode into the
    repository.
    """
    problems: list[Problem] = []
    directory = repo_root / "tools" / "capture"
    if not directory.exists():
        return ()
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        relative = f"tools/capture/{path.name}"
        if path.suffix == CAPTURE_PY:
            try:
                ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as error:
                problems.append(Problem("capture", relative, 1, f"capture script: python syntax error: {error}"))
        elif path.suffix == CAPTURE_MJS:
            problems.extend(_run_parser(["node", "--check", str(path)], relative, repo_root))
        elif path.suffix == CAPTURE_SH:
            problems.extend(_run_parser(["bash", "-n", str(path)], relative, repo_root))
        else:
            problems.append(
                Problem("capture", relative, 1, f"capture script: no parser for the {path.suffix or 'extensionless'} form")
            )
    return tuple(problems)


def _run_parser(argv: Sequence[str], relative: str, repo_root: Path) -> tuple[Problem, ...]:
    if shutil.which(argv[0]) is None:
        return (Problem("capture", relative, 1, f"capture script: {argv[0]} is not installed"),)
    proc = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return ()
    output = (proc.stderr or proc.stdout).strip().splitlines()
    return (Problem("capture", relative, 1, f"capture script: {output[-1] if output else argv[0] + ' failed'}"),)


def _defective(defective: Sequence[DefectiveArticle]) -> tuple[Problem, ...]:
    return tuple(
        Problem(code="pin", path=row.path, line=1, detail=f"not an article: {row.detail}") for row in defective
    )
