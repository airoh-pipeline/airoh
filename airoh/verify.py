# src/airoh/verify.py
"""🔍 Consistency checks between a project's code, config, data and docs.

A reproducible pipeline drifts from its own documentation quietly: a task is
renamed and the README still lists the old one, a step stops fetching something
and the docstring still says it does, an output appears and CONTENT.md never
hears about it. None of that breaks a run, so nothing catches it — which is
exactly why it needs a prespecified check rather than a habit.

``invoke verify`` runs a flat list of independent checks and exits non-zero if
any of them FAIL. There is deliberately no ordering, no dependency graph and no
caching here: each check reads the repository as it currently is and reports.

Each check returns a :class:`Finding` with one of four statuses:

* ``PASS``  — the check ran and found nothing.
* ``WARN``  — something worth looking at that is legitimately transient (an
  output that has not been produced yet, say).
* ``FAIL``  — a real inconsistency. Exits non-zero.
* ``SKIP``  — the check could not run here (no git, no README, no linter
  configured). Never a failure: a project without a README is not broken.

Checks are configured under an optional ``verify:`` block in invoke.yaml::

    verify:
      skip_checks: [lint]
      ignore_paths: [output_data/figures/*.png]
      max_tracked_bytes: 10000000

This complements the ``/verify`` skill, which runs these checks first and then
reads the prose for claims about behaviour that no regex can evaluate.
"""

import ast
import fnmatch
import re
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

from invoke import Exit, task

from airoh.provenance import parse_requirements, requirement_name

#: One check's verdict. ``details`` is a list of strings printed beneath it.
Finding = namedtuple("Finding", "check status message details")

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

_STATUS_ICON = {PASS: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "🫧"}

# Git tracks history forever, so a large file committed once is committed for
# good. Projects that legitimately track something big raise the ceiling in
# invoke.yaml rather than deleting the check.
DEFAULT_MAX_TRACKED_BYTES = 10 * 1024 * 1024

# Extensions that essentially never belong in git for an analysis project.
RISKY_EXTENSIONS = (".nii", ".nii.gz", ".mgz", ".h5", ".hdf5", ".mat", ".npy",
                    ".npz", ".zip", ".tar", ".tar.gz", ".tgz", ".dcm", ".sqlite")

# Files whose prose is checked for path references.
DOC_FILES = ("README.md", "CLAUDE.md", "source_data/CONTENT.md",
             "output_data/CONTENT.md")

# Records written by airoh.provenance, not data the project is expected to
# describe in CONTENT.md.
_PROVENANCE_NAMES = ("MANIFEST.json", "PROVENANCE.json")

# Keys airoh's own tasks read straight from the config, so a project's tasks.py
# never mentions them and they must not be reported as unused.
AIROH_CONFIG_KEYS = {"files", "datasets", "verify", "manifest_file",
                     "provenance_file", "provenance_hash_max_bytes",
                     "output_data_dir", "source_data_dir", "notebooks_dir",
                     "figures_dir", "docker_image", "docker_archive"}


def _ok(check, message):
    return Finding(check, PASS, message, [])


def _skip(check, message):
    return Finding(check, SKIP, message, [])


class Project:
    """The paths and settings the checks read, resolved once."""

    def __init__(self, config, root=None):
        self.config = config
        self.root = Path(root or Path.cwd())
        self.source_dir = self.root / config.get("source_data_dir", "source_data")
        self.output_dir = self.root / config.get("output_data_dir", "output_data")
        self.tasks_file = self.root / "tasks.py"
        self.invoke_yaml = self.root / "invoke.yaml"
        settings = config.get("verify") or {}
        self.skip_checks = set(settings.get("skip_checks") or [])
        self.ignore_paths = list(settings.get("ignore_paths") or [])
        self.max_tracked_bytes = settings.get(
            "max_tracked_bytes", DEFAULT_MAX_TRACKED_BYTES)
        self._basenames = None

    def basenames(self):
        """Every file and directory name in the repo, indexed once.

        Docs name a file by its bare basename far more often than by its full
        path (`floc_avgtsnr.png`, not `output_data/figures/tsnr_maps/…`), so
        resolving those needs a name index rather than a path join.
        """
        if self._basenames is None:
            skip = {".git", ".venv", "__pycache__", "node_modules",
                    ".ruff_cache", ".pytest_cache"}
            names = set()
            stack = [self.root]
            while stack:
                for entry in _iterdir(stack.pop()):
                    if entry.name in skip:
                        continue
                    names.add(entry.name)
                    if entry.is_dir() and not entry.is_symlink():
                        stack.append(entry)
            self._basenames = names
        return self._basenames

    def read(self, relative):
        """Text of a repo-relative file, or None if it is absent/unreadable."""
        try:
            return (self.root / relative).read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def is_ignored(self, relative):
        """Whether a repo-relative path is exempted by `verify.ignore_paths`."""
        return any(fnmatch.fnmatch(relative, pattern)
                   for pattern in self.ignore_paths)


# --------------------------------------------------------------------------- #
# Reading the project
# --------------------------------------------------------------------------- #
def task_names(tasks_file):
    """Names of the invoke tasks defined in ``tasks.py``, hyphenated.

    Parsed from the source rather than by importing it: verify must work on a
    project whose tasks.py imports something unavailable. Only tasks defined in
    the file are seen — namespaced tasks pulled in from airoh are not, and the
    README is not expected to list them.
    """
    try:
        tree = ast.parse(Path(tasks_file).read_text())
    except (OSError, SyntaxError):
        return None
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name) and target.id == "task":
                names.append(node.name.replace("_", "-"))
                break
    return sorted(set(names))


def config_keys_used(tasks_file):
    """Config keys ``tasks.py`` reads via ``c.config.get("…")``.

    Only a ``.get`` called directly on ``…​.config`` counts. Chained lookups
    such as ``c.config.get("datasets", {}).get("name", {})`` read *into* a
    value, and their inner keys are not top-level invoke.yaml keys.
    """
    try:
        tree = ast.parse(Path(tasks_file).read_text())
    except (OSError, SyntaxError):
        return set()
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        is_config = isinstance(receiver, ast.Attribute) and receiver.attr == "config"
        if node.func.attr != "get" or not node.args or not is_config:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            keys.add(first.value)
    return keys


def string_literals(tasks_file):
    """Every string literal in ``tasks.py``.

    Used only to decide whether a declared key is unused: a key can be handed
    to airoh by name rather than read directly (``keys=["source_data_dir"]``),
    and that still counts as used.
    """
    try:
        tree = ast.parse(Path(tasks_file).read_text())
    except (OSError, SyntaxError):
        return set()
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def config_keys_declared(invoke_yaml):
    """Top-level keys declared in invoke.yaml.

    Read straight from the file rather than from the loaded config, which also
    holds invoke's own defaults (``run``, ``timeouts``, …) and would make every
    project look full of unused keys.
    """
    try:
        text = Path(invoke_yaml).read_text()
    except OSError:
        return None
    return {match.group(1)
            for match in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*):", text, re.MULTILINE)}


def _git_tracked_files(root):
    """Repo-relative paths git tracks, or None outside a repository."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(root), capture_output=True,
            text=True, stdin=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [name for name in result.stdout.split("\0") if name]


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_task_list(project):
    """Every task defined in tasks.py is documented, and vice versa."""
    defined = task_names(project.tasks_file)
    if defined is None:
        return _skip("task_list", "no readable tasks.py")

    invoked, mentioned = set(), set()
    for doc in ("README.md", "CLAUDE.md"):
        text = project.read(doc)
        if text:
            invoked |= documented_task_names(text)
            mentioned |= backticked_names(text)

    if not invoked:
        return _skip("task_list", "no `invoke <task>` references in README/CLAUDE")

    # Naming a task anywhere (a README table row) documents it; only an
    # explicit `invoke foo` claims that a task by that name exists.
    undocumented = sorted(set(defined) - invoked - mentioned)
    phantom = sorted(invoked - set(defined))
    details = ([f"defined but not documented: {name}" for name in undocumented]
               + [f"documented but not defined: {name}" for name in phantom])
    if details:
        return Finding("task_list", FAIL,
                       "task list in the docs does not match tasks.py", details)
    return _ok("task_list", f"{len(defined)} task(s) documented")


def documented_task_names(text):
    """Task names documented as ``invoke <name>`` in ``text``.

    Only code spans and fenced blocks are read: a sentence about "reusable
    invoke tasks" is prose, not a claim that a task named ``tasks`` exists. The
    lookahead drops placeholders like ``invoke fetch-{name}``, which document a
    naming convention rather than one concrete task.
    """
    spans = re.findall(r"```(.*?)```", text, re.DOTALL)
    spans += re.findall(r"`([^`\n]+)`", text)
    names = set()
    for span in spans:
        names |= set(re.findall(r"\binvoke\s+([a-z][a-z0-9-]*[a-z0-9])(?![\w{-])", span))
    return names


def backticked_names(text):
    """Code spans that are bare task-like names, e.g. `run-simulation`.

    A README often documents a task in a table cell rather than as a command,
    which still counts as documenting it.
    """
    return {token for token in re.findall(r"`([a-z][a-z0-9-]*[a-z0-9])`", text)}


def check_dependencies(project):
    """pyproject.toml, requirements.txt and environment.yml declare the same packages."""
    from airoh.provenance import _parse_pyproject_dependencies

    declared = {}
    if (project.root / "pyproject.toml").is_file():
        declared["pyproject.toml"] = set(
            _parse_pyproject_dependencies(project.root / "pyproject.toml"))
    if (project.root / "requirements.txt").is_file():
        declared["requirements.txt"] = set(
            parse_requirements(project.root / "requirements.txt"))
    if (project.root / "environment.yml").is_file():
        declared["environment.yml"] = _conda_dependencies(
            project.root / "environment.yml")

    declared = {name: packages for name, packages in declared.items() if packages}
    if len(declared) < 2:
        return _skip("dependencies", "fewer than two dependency files to compare")

    union = set().union(*declared.values())
    details = []
    for name, packages in sorted(declared.items()):
        missing = sorted(union - packages)
        if missing:
            details.append(f"{name} is missing: {', '.join(missing)}")
    if details:
        return Finding("dependencies", FAIL,
                       "dependency files disagree — an install path is broken",
                       details)
    return _ok("dependencies", f"{len(union)} package(s) agree across {len(declared)} file(s)")


def _conda_dependencies(path):
    """Package names under `dependencies:` in an environment.yml.

    Block-aware: entries under `channels:` are not packages, and the `pip:`
    sub-list holds packages but is not one itself.
    """
    try:
        text = Path(path).read_text()
    except OSError:
        return set()
    names, in_dependencies = set(), False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_dependencies = line.startswith("dependencies:")
            continue
        stripped = line.strip()
        if not in_dependencies or not stripped.startswith("- "):
            continue
        item = stripped[2:].rstrip(":")
        if item.startswith("-"):
            # A pip flag (`-e .`, `-r requirements.txt`), not a package.
            continue
        candidate = requirement_name(item)
        if candidate and candidate not in ("pip", "python"):
            names.add(candidate)
    return names


def check_doc_paths(project):
    """Paths named in the docs exist on disk."""
    missing_hard, missing_soft = [], []
    for doc in DOC_FILES:
        text = project.read(doc)
        if text is None:
            continue
        # A CONTENT.md names its own folder's entries by bare filename, so
        # paths resolve relative to the document as well as to the repo root.
        doc_dir = project.root / Path(doc).parent
        for candidate in _path_candidates(text):
            if project.is_ignored(candidate) or _resolves(project, doc_dir, candidate):
                continue
            if _is_soft(project, doc, candidate):
                missing_soft.append(f"{doc}: {candidate} (not present yet)")
            else:
                missing_hard.append(f"{doc}: {candidate}")

    if missing_hard:
        return Finding("doc_paths", FAIL, "docs reference paths that do not exist",
                       missing_hard + missing_soft)
    if missing_soft:
        return Finding("doc_paths", WARN,
                       "docs reference data paths not present yet "
                       "(run `invoke fetch` / `invoke run`?)", missing_soft)
    return _ok("doc_paths", "every path named in the docs exists")


def _path_candidates(text):
    """Repo-relative paths mentioned in backticks in ``text``.

    Conservative on purpose: a token counts only if it looks like a path (a
    slash, or a known file extension) and cannot be something else — a URL, a
    shell command, a git remote.
    """
    candidates = set()
    for token in re.findall(r"`([^`\n]+)`", text):
        token = token.strip().rstrip(",.;:")
        if not token or any(char in token for char in " \t@{}…") or "://" in token:
            continue
        if token.startswith(("/", "~", "-")) or ".." in token:
            continue
        has_extension = re.search(
            r"\.(py|md|ipynb|yaml|yml|toml|txt|json|tsv|csv|png|svg|lock|cfg)$", token)
        if "/" not in token and not has_extension:
            continue
        # Strip a leading "./" as a prefix, not as a character set — lstrip
        # would also eat the dot of a path like ".claude/skills".
        if token.startswith("./"):
            token = token[2:]
        candidates.add(token.rstrip("/"))
    return sorted(candidates)


def _iterdir(path):
    """Entries of a directory, empty on any error."""
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _resolves(project, doc_dir, candidate):
    """Whether a documented path can be found, from the root or from the doc.

    A candidate with a directory component is resolved as a path; a bare
    basename is looked up in the repo's name index, since docs rarely spell
    out where a file lives.
    """
    is_glob = any(char in candidate for char in "*?[")
    if "/" not in candidate:
        # A bare pattern like `*_bold.json` names a file-naming convention, not
        # a location — there is nothing to resolve.
        return is_glob or candidate in project.basenames()
    if is_glob:
        return _glob_hit(project.root, candidate) or _glob_hit(doc_dir, candidate)
    return (project.root / candidate).exists() or (doc_dir / candidate).exists()


def _glob_hit(root, pattern):
    """Whether a glob pattern matches anything under ``root``."""
    try:
        return any(root.glob(pattern))
    except (ValueError, OSError):
        return True


def _is_soft(project, doc, candidate):
    """Whether a missing path is a warning rather than a failure.

    Three cases are legitimately absent: data that has not been fetched or
    produced yet, a directory the docs tell the reader to *create* (the
    template's `tests/`), and a bare filename, which claims that a file exists
    somewhere rather than that it exists at a particular place — `MANIFEST.json`
    before the first fetch, say. A missing path *with a directory component* is
    a real failure: it points somewhere specific, and nothing is there.
    """
    for data_dir in (project.source_dir, project.output_dir):
        if candidate.startswith(data_dir.name + "/") or data_dir.name in doc:
            return True
    return "/" not in candidate or "." not in Path(candidate).name


def check_content_md(project):
    """Everything in the data folders is described in that folder's CONTENT.md."""
    details, checked = [], 0
    for data_dir in (project.source_dir, project.output_dir):
        if not data_dir.is_dir():
            continue
        content = project.read(Path(data_dir.name) / "CONTENT.md")
        if content is None:
            details.append(f"{data_dir.name}/CONTENT.md is missing")
            continue
        checked += 1
        for entry in sorted(data_dir.iterdir()):
            if entry.name.startswith(".") or entry.name in _PROVENANCE_NAMES:
                continue
            if entry.name == "CONTENT.md":
                continue
            if entry.name not in content:
                details.append(
                    f"{data_dir.name}/{entry.name} is not described in "
                    f"{data_dir.name}/CONTENT.md")

    if not checked and not details:
        return _skip("content_md", "no data folders to check")
    if details:
        return Finding("content_md", FAIL,
                       "data folders and their CONTENT.md disagree", details)
    return _ok("content_md", "data folders match their CONTENT.md")


def check_config_keys(project):
    """tasks.py and invoke.yaml agree on which config keys exist."""
    declared = config_keys_declared(project.invoke_yaml)
    if declared is None:
        return _skip("config_keys", "no readable invoke.yaml")
    used = config_keys_used(project.tasks_file)
    if not used:
        return _skip("config_keys", "tasks.py reads no config keys")

    details = [f"read in tasks.py but not declared in invoke.yaml: {key}"
               for key in sorted(used - declared)]
    if details:
        return Finding("config_keys", FAIL,
                       "tasks.py reads config keys invoke.yaml does not declare",
                       details)
    unused = sorted(declared - used - AIROH_CONFIG_KEYS
                    - string_literals(project.tasks_file))
    if unused:
        return Finding("config_keys", WARN,
                       "invoke.yaml declares keys nothing reads",
                       [f"unused: {key}" for key in unused])
    return _ok("config_keys", f"{len(used)} config key(s) declared and read")


def check_tracked_size(project):
    """No oversized or binary-by-nature file is tracked in git."""
    tracked = _git_tracked_files(project.root)
    if tracked is None:
        return _skip("tracked_size", "not a git repository")

    oversized, risky = [], []
    for name in tracked:
        if project.is_ignored(name):
            continue
        # Dot-directories hold tooling (.claude/ skills, .github/ workflows),
        # not data — an archive there is deliberate.
        is_tooling = name.startswith(".")
        if name.lower().endswith(RISKY_EXTENSIONS) and not is_tooling:
            risky.append(f"risky file type tracked: {name}")
        path = project.root / name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > project.max_tracked_bytes:
            oversized.append(f"{name} is {size // 1024 // 1024} MB")

    if oversized or risky:
        return Finding("tracked_size", FAIL,
                       "git tracks files that do not belong in git",
                       risky + oversized)
    return _ok("tracked_size", f"{len(tracked)} tracked file(s) within limits")


def check_provenance(project):
    """The provenance records exist and are not older than what they describe."""
    manifest = project.root / project.config.get(
        "manifest_file", "source_data/MANIFEST.json")
    record = project.root / project.config.get(
        "provenance_file", "output_data/PROVENANCE.json")

    details = []
    if _has_data(project.source_dir) and not manifest.is_file():
        details.append(f"{manifest.name} is missing though source data is present "
                       "— run `invoke fetch`")
    if _has_data(project.output_dir):
        if not record.is_file():
            details.append(f"{record.name} is missing though outputs are present "
                           "— run `invoke run`")
        elif _newest_mtime(project.output_dir, record) > record.stat().st_mtime:
            details.append(f"{record.name} is older than the outputs it describes "
                           "— re-run `invoke run`")

    if not manifest.is_file() and not record.is_file() and not details:
        return _skip("provenance", "no provenance records and no data yet")
    if details:
        return Finding("provenance", WARN, "provenance records are stale or missing",
                       details)
    return _ok("provenance", "provenance records are present and current")


def _has_data(data_dir):
    """Whether a data folder holds anything beyond its own bookkeeping."""
    if not data_dir.is_dir():
        return False
    return any(entry.name not in _PROVENANCE_NAMES + ("CONTENT.md",)
               and not entry.name.startswith(".")
               for entry in data_dir.iterdir())


def _newest_mtime(data_dir, exclude):
    """Newest mtime of an actual output under ``data_dir``.

    Repository bookkeeping that happens to live in the data folder — the record
    itself, dotfiles, CONTENT.md — is not an output, and editing it must not
    make the provenance record look stale.
    """
    newest = 0
    for path in data_dir.rglob("*"):
        if not path.is_file() or path == exclude or path.name == "CONTENT.md":
            continue
        if any(part.startswith(".") for part in path.relative_to(data_dir).parts):
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def check_lint(project):
    """The project's configured linter passes."""
    pyproject = project.read("pyproject.toml") or ""
    if "[tool.ruff" in pyproject:
        command, binary = ["ruff", "check", "."], "ruff"
    elif (project.root / "setup.cfg").is_file() and "[flake8]" in (
            project.read("setup.cfg") or ""):
        command, binary = ["flake8"], "flake8"
    else:
        return _skip("lint", "no linter configured")

    if shutil.which(binary) is None:
        return _skip("lint", f"{binary} is configured but not on PATH")
    try:
        result = subprocess.run(command, cwd=str(project.root), capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        return _skip("lint", f"could not run {binary}: {error}")
    if result.returncode != 0:
        return Finding("lint", FAIL, f"{binary} reported problems",
                       result.stdout.strip().splitlines()[:20])
    return _ok("lint", f"{binary} is clean")


#: Every check, in report order. Add to this list to add a check.
CHECKS = (
    check_content_md,
    check_task_list,
    check_dependencies,
    check_doc_paths,
    check_config_keys,
    check_tracked_size,
    check_provenance,
    check_lint,
)


def run_checks(config, root=None, skip=()):
    """Run every check and return the findings, in report order.

    Importable so a project (or a test) can consume the findings directly
    rather than parsing printed output.
    """
    project = Project(config, root)
    skip = set(skip) | project.skip_checks
    findings = []
    for check in CHECKS:
        name = check.__name__.replace("check_", "")
        if name in skip:
            findings.append(_skip(name, "skipped by configuration"))
            continue
        try:
            findings.append(check(project))
        except Exception as error:  # a broken check must not mask the others
            findings.append(Finding(name, SKIP, f"check raised {error!r}", []))
    return findings


@task(help={
    "skip": "Comma-separated check names to skip (also settable under "
            "`verify: skip_checks:` in invoke.yaml).",
    "strict": "Treat warnings as failures.",
})
def verify(c, skip=None, strict=False):
    """🔍 Check that code, config, data and docs still agree.

    Runs a flat list of independent checks — see the module docstring for what
    each one covers — prints a report, and exits non-zero if any FAIL (or, with
    --strict, any WARN). Never called by `run`: reproduction should not depend
    on documentation hygiene. Run it before committing, and in CI.

    The checks are mechanical. For claims that only prose can make — "this step
    never pulls data", "this default is 30" — use the `/verify` skill, which
    runs these first and then reads the docs against the code.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    skip : str, optional
        Comma-separated check names to skip.
    strict : bool, optional
        Treat warnings as failures (default: False).

    Examples
    --------
    ```bash
    inv verify
    inv verify --skip lint --strict
    ```
    """
    skip_names = [name.strip() for name in (skip or "").split(",") if name.strip()]
    findings = run_checks(c.config, skip=skip_names)

    for finding in findings:
        print(f"{_STATUS_ICON[finding.status]} {finding.check}: {finding.message}")
        for detail in finding.details:
            print(f"      · {detail}")

    failed = [f for f in findings if f.status == FAIL]
    warned = [f for f in findings if f.status == WARN]
    passed = [f for f in findings if f.status == PASS]
    print(f"\n{len(passed)} passed, {len(warned)} warning(s), {len(failed)} failure(s)")

    if failed or (strict and warned):
        raise Exit("❌ verify failed — the docs and the code disagree", code=1)
    print("✅ verify passed")
