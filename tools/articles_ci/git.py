"""The upstream repositories, read through `git` and nothing else.

Invariant: this is the only module in the package that starts a process for git, and it
never reads a blob - `commits` asks for trees only, which is why rename detection is off.
Every path out of here is a `RemoteRef`, a `CommitFact` or a string, so no caller ever
sees a git wire format.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_GIT_BASE = "https://github.com"
DEFAULT_WORKDIR_NAME = "articles-ci"
MAX_PATHS_PER_COMMIT = 200
MAX_STDERR_CHARS = 400


class GitError(RuntimeError):
    """A git command failed.

    Invariant: `argv` and `stderr` are carried verbatim, because the drift report quotes
    them to the human instead of paraphrasing them.
    """

    def __init__(self, argv: Sequence[str], stderr: str) -> None:
        self.argv = tuple(argv)
        self.stderr = stderr
        super().__init__(f"{' '.join(self.argv)} failed: {self.stderr}")


class PinUnknown(GitError):
    """The recorded commit is not an object in the upstream's published history.

    Distinct from `GitError` because it is a report line ("pin-unknown"), not a failure
    to reach the upstream.
    """


@dataclass(frozen=True)
class RemoteRef:
    """Where an upstream's published branch is, right now."""

    default_branch: str
    sha: str


@dataclass(frozen=True)
class CommitFact:
    """One non-merge commit as the upstream records it.

    Invariant: no blob is read to build this, so there is no diff text here by
    construction, and `paths_truncated` says how many paths the cap dropped.
    """

    sha: str
    short: str
    date: str
    subject: str
    body: str
    paths: tuple[str, ...]
    added: tuple[str, ...]
    deleted: tuple[str, ...]
    paths_truncated: int


@dataclass
class Remote:
    """One upstream repository, reachable only through this object.

    Invariant: `url` may be a local path, which is what makes the fixture world
    network-free, and `mirror` is created only by `fetch` so a current pin costs one
    `ls-remote` and no clone.
    """

    repo: str
    url: str
    mirror: Path
    _ref: RemoteRef | None = field(default=None, repr=False)

    def head(self) -> RemoteRef:
        """The published default branch and its tip, from `git ls-remote --symref HEAD`.

        Invariant: the branch is read from the symref line, so payment-system's `master`
        is discovered rather than assumed, and no branch name is kept anywhere.
        """
        out = run_git(["git", "ls-remote", "--symref", self.url, "HEAD"])
        branch: str | None = None
        sha: str | None = None
        for line in out.splitlines():
            if line.startswith("ref:") and line.endswith("HEAD"):
                branch = line.split()[1].removeprefix("refs/heads/")
            elif line.endswith("\tHEAD"):
                sha = line.split()[0]
        if branch is None or sha is None:
            raise GitError(
                ("git", "ls-remote", "--symref", self.url, "HEAD"),
                f"could not read a default branch and tip from {self.url}",
            )
        self._ref = RemoteRef(default_branch=branch, sha=sha)
        return self._ref

    def fetch(self) -> Path:
        """Create the blobless mirror once, on the branch `head()` discovered.

        Invariant: the branch is passed in from `head()` rather than left to the clone's
        own default, and no blob is fetched, so the whole mirror is commit and tree
        objects only.
        """
        if self._ref is None:
            raise GitError(("git", "clone", self.url), "head() must run before fetch()")
        if self.mirror.exists():
            return self.mirror
        self.mirror.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--single-branch",
                "--no-tags",
                "--no-checkout",
                "--quiet",
                "--branch",
                self._ref.default_branch,
                self.url,
                str(self.mirror),
            ]
        )
        return self.mirror

    def resolve(self, sha: str) -> str:
        """A recorded short sha -> 40 hex, inside the mirror.

        Invariant: the lookup never falls back to the remote, so an object the mirror
        does not have is `PinUnknown` rather than a surprise network call, and the
        promise that a current pin costs no clone is kept.
        """
        argc = [
            "git",
            "-c",
            "remote.origin.promisor=false",
            "rev-parse",
            "--verify",
            "--quiet",
            f"{sha}^{{commit}}",
        ]
        proc = _exec(argc, self.mirror)
        resolved = proc.stdout.strip()
        if proc.returncode != 0 or not resolved:
            raise PinUnknown(argc, f"{sha} is not in the published history of {self.repo}")
        return resolved

    def is_ancestor(self, sha: str, tip: str) -> bool:
        """Whether the recorded commit is an ancestor of the published tip.

        Invariant: False means the published history no longer contains the pin, which is
        a different report line ("diverged") from simply being behind.
        """
        argv = ["git", "merge-base", "--is-ancestor", sha, tip]
        proc = _exec(argv, self.mirror)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise GitError(argv, proc.stderr.strip())

    def tip(self) -> str:
        """The mirror's tip of the default branch.

        Invariant: this is what the report calls `upstream_sha`, not the earlier
        `head()`, so the sha reported is the one that was actually diffed.
        """
        if self._ref is None:
            raise GitError(("git", "rev-parse", self.url), "head() must run before tip()")
        return run_git(["git", "rev-parse", f"refs/heads/{self._ref.default_branch}"], cwd=self.mirror).strip()

    def count(self, spec: str, *, merges: bool = False) -> int:
        """`git rev-list --count` for a range such as `<a>..<b>`."""
        argv = ["git", "rev-list", "--count"]
        if merges:
            argv.append("--merges")
        argv.append(spec)
        return int(run_git(argv, cwd=self.mirror).strip())

    def commits(self, base: str, tip: str) -> tuple[CommitFact, ...]:
        """The non-merge commits in `<base>..<tip>`, newest first.

        Invariant: `--no-renames` is load-bearing, not cosmetic - rename detection would
        make git read blobs, which a blob-nothing mirror would then have to fetch.
        `--name-status` supplies the added/deleted subsets without a second walk.
        """
        out = run_git(
            [
                "git",
                "log",
                "--no-merges",
                "--no-renames",
                "--diff-filter=ACDMRT",
                "--name-status",
                "--format=%x00%H%x1f%cs%x1f%s%x1f%b%x1d",
                f"{base}..{tip}",
            ],
            cwd=self.mirror,
        )
        facts: list[CommitFact] = []
        for record in out.split("\x00"):
            if not record.strip():
                continue
            head, _, rest = record.partition("\x1d")
            fields = head.split("\x1f", 3)
            if len(fields) != 4:
                raise GitError(
                    ("git", "log", "--format=%x00%H%x1f%cs%x1f%s%x1f%b%x1d", f"{base}..{tip}"),
                    f"unparsable git log record: {record[:80]!r}",
                )
            sha, date, subject, body = fields
            entries: list[tuple[str, str]] = []
            for line in rest.splitlines():
                if "\t" not in line:
                    continue
                status, _, path = line.partition("\t")
                entries.append((status.strip(), path.strip()))
            kept = entries[:MAX_PATHS_PER_COMMIT]
            paths = tuple(path for _, path in kept)
            facts.append(
                CommitFact(
                    sha=sha.strip(),
                    short=sha.strip()[:7],
                    date=date.strip(),
                    subject=subject.strip(),
                    body=body.strip(),
                    paths=paths,
                    added=tuple(path for status, path in kept if status.startswith("A")),
                    deleted=tuple(path for status, path in kept if status.startswith("D")),
                    paths_truncated=max(0, len(entries) - len(kept)),
                )
            )
        return tuple(facts)

    def tree(self, sha: str) -> tuple[str, ...]:
        """The upstream file list at `sha`, which is what the anchors resolve against."""
        out = run_git(["git", "ls-tree", "-r", "--name-only", sha], cwd=self.mirror)
        return tuple(line for line in out.splitlines() if line.strip())


def git_base() -> str:
    """`ARTICLES_CI_GIT_BASE`, default `https://github.com`, without a trailing slash.

    The seam is the URL, not a mock: pointing it at a local directory makes the whole
    drift half run offline through the production code path.
    """
    return (os.environ.get("ARTICLES_CI_GIT_BASE") or DEFAULT_GIT_BASE).rstrip("/")


def remote_url(repo: str) -> str:
    """`<base>/<owner>/<name>.git` for an `owner/name` from an article's header row."""
    return f"{git_base()}/{repo}.git"


def workdir_default() -> Path:
    """`ARTICLES_CI_WORKDIR`, or a fresh per-process directory under the system temp dir.

    Invariant: there is no mirror cache across runs; every run re-reads the upstreams, so
    there is no second state that can disagree with the article pins. The CLI passes its own
    throwaway directory, so a command leaves nothing behind; this default is what a direct
    caller gets - the selftest, mostly, which points the variable inside its fixture root.
    """
    configured = os.environ.get("ARTICLES_CI_WORKDIR")
    if configured:
        return Path(configured)
    return Path(tempfile.mkdtemp(prefix=f"{DEFAULT_WORKDIR_NAME}-"))


def resolve_remote(repo: str, workdir: Path | None = None) -> Remote:
    """The `Remote` for one article's repository, with its mirror under `workdir`.

    Invariant: a `Remote` is only ever built here, so the clone URL and the mirror path
    have one owner and `git remote add` never appears anywhere in the package.
    """
    base = workdir or workdir_default()
    return Remote(repo=repo, url=remote_url(repo), mirror=base / f"{repo.replace('/', '__')}.git")


def tracked_files(repo_root: Path) -> frozenset[str]:
    """This repository's tracked files, from `git ls-files -z`.

    Invariant: `validate` asks git once, here, instead of starting git itself; presence on
    disk is not what is checked, because an untracked image is an image the published site
    never gets.
    """
    out = run_git(["git", "ls-files", "-z"], cwd=repo_root)
    return frozenset(name for name in out.split("\0") if name)


def origin_repo(repo_root: Path) -> str:
    """`owner/name` of this repository's `origin`, so the issue repo is never hand-kept."""
    url = run_git(["git", "remote", "get-url", "origin"], cwd=repo_root).strip()
    path = url.removesuffix(".git").rstrip("/")
    if path.startswith("git@"):
        path = path.split(":", 1)[-1]
    else:
        parts = path.split("://")[-1].split("/")
        path = "/".join(parts[1:]) if len(parts) > 1 else path
    segments = path.split("/")
    return "/".join(segments[-2:])


def run_git(argv: Sequence[str], *, cwd: Path | None = None, retries: int = 3) -> str:
    """The single retrying subprocess boundary.

    Invariant: the argv is passed as a list and never through a shell, and a failure
    raises `GitError` with the argv and the last stderr after `retries` attempts, so a
    flaky upstream is reported and not silently turned into "no drift".
    """
    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(max(1, retries)):
        last = _exec(argv, cwd)
        if last.returncode == 0:
            return last.stdout
    assert last is not None
    raise GitError(argv, (last.stderr or last.stdout).strip()[:MAX_STDERR_CHARS])


def _exec(argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "LC_ALL": "C",
        }
    )
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
