"""What an EXISTING codebase contains — deterministic, no LLM (brownfield's ``design_pack``).

Greenfield answers "what am I building?" by parsing a design package. Brownfield has no design
package: the repository itself is the input, and something has to turn a directory of unfamiliar
files into the few structured facts the rest of the pipeline needs — which files are source, how
they group into modules, what language and package manager they use, what the tests are.

Same family as ``naming_contract`` / ``wiring`` / ``plan_builder`` / ``boilerplate``: pure logic
over data the executor hands back, no model, no side effects, unit-testable without a sandbox, and
degrading to an EMPTY inventory rather than a guessed one. An empty inventory is a legible failure
("nothing recognizable here, stop") — a guessed one would send the change planner after files that
do not exist.

This module also owns the two other facts acquisition needs and nothing else provides:
:func:`detect_default_branch` (so a PR is opened against the branch the repo actually uses, not a
hardcoded ``main``) and :func:`digests` (the pre-change baseline that later proves an edit really
happened, rather than a target file merely existing).

The skip-lists and grouping here are generalized from ``scripts/run_unit_testing.py``'s private
``_build_synthetic_work_items``, which proved the approach against real repos but could only be
used by that one script.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from typing import Any

from app.integrations.executor import Executor
from app.services.plan_builder import _is_test_path

#: Files that are configuration, lockfiles or documentation rather than application logic. They are
#: still inventoried in ``files`` — only ``source_files`` (what a change is planned against) skips
#: them, because nobody asks for a change request to be implemented "in package-lock.json".
SKIP_BASENAMES = frozenset({
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", ".gitignore", ".dockerignore",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    ".env.example", ".eslintrc.js", "eslint.config.mjs", "jest.config.js", "jest.config.cjs",
    "babel.config.cjs", "knexfile.js", "tsconfig.json", "vite.config.ts", "LICENSE",
})

#: Extensions treated as application source. Deliberately broader than the JS/TS/Python the
#: generator emits — brownfield targets repos this service did not write.
SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rb", ".rs",
    ".cs", ".php", ".kt", ".swift", ".scala", ".vue", ".svelte",
})

#: Directories whose contents are generated, vendored or data — never a change target.
SKIP_DIR_PARTS = frozenset({"migrations", "seeds", "vendor", "third_party", "generated", "fixtures"})

#: basename -> the package manager it implies. Drives ``languages``/``manifests`` reporting and,
#: later, which test command the repo actually uses.
MANIFEST_NAMES = {
    "package.json": "npm", "requirements.txt": "pip", "pyproject.toml": "pip",
    "setup.py": "pip", "Pipfile": "pip", "go.mod": "go", "Cargo.toml": "cargo",
    "pom.xml": "maven", "build.gradle": "gradle", "Gemfile": "bundler",
    "composer.json": "composer", "Makefile": "make",
}


@dataclass(frozen=True)
class RepoInventory:
    """The structured facts about an acquired repository. Empty == nothing recognizable."""

    files: list[str] = field(default_factory=list)          # every listable file, project-relative
    source_files: list[str] = field(default_factory=list)   # application logic only (no tests)
    test_files: list[str] = field(default_factory=list)
    by_dir: dict[str, list[str]] = field(default_factory=dict)   # module dir -> its source files
    languages: dict[str, int] = field(default_factory=dict)      # extension -> source-file count
    manifests: dict[str, str] = field(default_factory=dict)      # manifest path -> package manager

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def primary_language(self) -> str:
        """The most common source extension, or "" when there is no source at all. Used to pick
        sensible defaults (which test runner, which import syntax) without asking a model."""
        return max(self.languages, key=lambda k: self.languages[k]) if self.languages else ""

    def as_dict(self) -> dict[str, Any]:
        """Plain-data form for ``WorkflowState`` — the checkpointer only allow-lists ``WorkItem``
        for msgpack, so everything else on state must already be JSON-shaped."""
        return {
            "files": list(self.files),
            "source_files": list(self.source_files),
            "test_files": list(self.test_files),
            "by_dir": {k: list(v) for k, v in self.by_dir.items()},
            "languages": dict(self.languages),
            "manifests": dict(self.manifests),
            "primary_language": self.primary_language,
        }

    def render(self, max_paths: int = 60) -> str:
        """Human-readable summary for the run report. Overflow is REPORTED, never silently cut —
        the same discipline as code_review's context builder and refactoring's deferred list."""
        if self.is_empty:
            return "_No files found._"
        lines = [
            f"- **Files:** {len(self.files)} "
            f"({len(self.source_files)} source, {len(self.test_files)} test)",
            f"- **Primary language:** {self.primary_language or 'unknown'}",
            f"- **Modules:** {len(self.by_dir)}",
        ]
        if self.manifests:
            managers = sorted(set(self.manifests.values()))
            lines.append(f"- **Package managers:** {', '.join(managers)}")
        if self.languages:
            mix = ", ".join(f"{ext} x {n}" for ext, n in
                            sorted(self.languages.items(), key=lambda kv: -kv[1]))
            lines.append(f"- **Language mix:** {mix}")
        lines.append("")
        lines.append("| Module | Source files |")
        lines.append("| --- | --- |")
        for directory, paths in sorted(self.by_dir.items())[:max_paths]:
            lines.append(f"| `{directory or '.'}` | {len(paths)} |")
        if len(self.by_dir) > max_paths:
            lines.append(f"| _(+{len(self.by_dir) - max_paths} more modules)_ | |")
        return "\n".join(lines)


def _is_source(path: str) -> bool:
    base = posixpath.basename(path)
    if base in SKIP_BASENAMES or posixpath.splitext(base)[1] not in SOURCE_EXTENSIONS:
        return False
    return not (SKIP_DIR_PARTS & set(path.split("/")))


def inventory(executor: Executor, project_dir: str) -> RepoInventory:
    """Survey ``project_dir`` through the executor. Never raises — an unreadable or missing tree
    yields an empty inventory, which the caller treats as "nothing to work with"."""
    try:
        files = executor.list_files(project_dir)
    except Exception:  # noqa: BLE001 - a survey that fails is an empty survey, not a crashed run
        return RepoInventory()

    source, tests = [], []
    for path in files:
        if not _is_source(path):
            continue
        (tests if _is_test_path(path) else source).append(path)

    by_dir: dict[str, list[str]] = {}
    languages: dict[str, int] = {}
    for path in source:
        by_dir.setdefault(posixpath.dirname(path), []).append(path)
        ext = posixpath.splitext(path)[1]
        languages[ext] = languages.get(ext, 0) + 1

    manifests = {p: MANIFEST_NAMES[posixpath.basename(p)]
                 for p in files if posixpath.basename(p) in MANIFEST_NAMES}

    return RepoInventory(
        files=list(files),
        source_files=sorted(source),
        test_files=sorted(tests),
        by_dir={k: sorted(v) for k, v in sorted(by_dir.items())},
        languages=languages,
        manifests=dict(sorted(manifests.items())),
    )


def digests(executor: Executor, project_dir: str, paths: list[str]) -> dict[str, str]:
    """sha256 of each path's CURRENT content, project-relative.

    This is the baseline that makes "was the change actually made?" answerable. ``files_complete``
    — the only gate today — asks whether a path exists, which every pre-existing file passes
    without being touched, so a brownfield run could report success having changed nothing.

    An unreadable path maps to ``""`` rather than being dropped: absent and unreadable are both
    "no content to compare", and keeping the key means a later comparison sees the path at all.
    """
    out: dict[str, str] = {}
    for rel in paths:
        try:
            content = executor.read_file(f"{project_dir}/{rel}")
        except Exception:  # noqa: BLE001 - absent/unreadable == no baseline for this path
            out[rel] = ""
            continue
        out[rel] = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return out


def detect_default_branch(executor: Executor, project_dir: str, fallback: str = "main") -> str:
    """The branch this repo actually treats as its default.

    ``finalize`` opens its PR against this. Hardcoding "main" is right only for a repo this service
    scaffolded; a repo handed to us may use master/develop/trunk, and a PR against a branch that
    does not exist fails at the last step of a run that already did all its work.

    ``origin/HEAD`` is the authoritative answer and is set by ``git clone``. The remote-show
    fallback covers a clone whose HEAD ref was not written; ``fallback`` covers the rest.
    """
    res = executor.run_command(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=project_dir
    )
    if res.exit_code == 0 and res.stdout.strip():
        return res.stdout.strip().split("/", 1)[-1]

    res = executor.run_command(["git", "remote", "show", "origin"], cwd=project_dir)
    if res.exit_code == 0:
        for line in res.stdout.splitlines():
            if "HEAD branch:" in line:
                name = line.split("HEAD branch:", 1)[1].strip()
                if name and name != "(unknown)":
                    return name
    return fallback
