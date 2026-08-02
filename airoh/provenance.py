# src/airoh/provenance.py
"""📜 Lightweight provenance records for a pipeline's inputs and outputs.

Two records, written by two tasks:

* ``record_sources`` → ``source_data/MANIFEST.json``, written at the end of
  ``fetch``: what each data asset actually resolved to (a URL, a real path
  behind a symlink), how big it was, its checksum, and — when the asset lives
  in a git or datalad repository — the commit it sat at.
* ``record_run`` → ``output_data/PROVENANCE.json``, written at the end of
  ``run``: the project commit, the environment, the manifest it consumed, and
  a checksum of every file the run produced.

Together they answer "which inputs produced these outputs, on which code, in
which environment" without requiring datalad. When a project *is* tracked with
datalad, these records are redundant but harmless — datalad remains the only
thing that can actually *retrieve* a past state. See CLAUDE.md.

Everything here is tolerant by design: a provenance record is documentation,
never a precondition, so a missing file, an absent git binary or an unreadable
asset is recorded as ``null`` with a warning. Nothing in this module raises.
"""

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from invoke import task

# Hashing a multi-gigabyte asset would make every fetch crawl for a number
# nobody reads, so files above this size record their metadata only. Override
# with `provenance_hash_max_bytes` in invoke.yaml.
DEFAULT_HASH_MAX_BYTES = 100 * 1024 * 1024

# git is invoked for metadata only, so it should answer immediately. A bounded
# timeout keeps an unreachable remote or a credential prompt from stalling a
# fetch (git is also run with prompts disabled, below).
_GIT_TIMEOUT_SECONDS = 15

_GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0")


def _utc_now():
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(args, cwd):
    """Return the stripped stdout of a git command, or None if it fails.

    Tolerant: a missing git binary, a path outside any repository, or a
    timeout all yield None rather than an exception.
    """
    try:
        result = subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, env=_GIT_ENV, timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_info(path):
    """Describe the git state of the repository containing ``path``.

    Returns a dict with ``commit``, ``branch``, ``remote`` and ``dirty``, or
    None when ``path`` is not inside a git repository (or git is unavailable).
    Used both for data assets — a symlinked datalad superdataset is a git
    repository, so its commit pins the input state — and for the project repo
    itself.
    """
    path = Path(path)
    cwd = path if path.is_dir() else path.parent
    if not cwd.exists():
        return None
    if _git(["rev-parse", "--is-inside-work-tree"], cwd) != "true":
        return None
    status = _git(["status", "--porcelain"], cwd)
    return {
        "toplevel": _git(["rev-parse", "--show-toplevel"], cwd),
        "commit": _git(["rev-parse", "HEAD"], cwd),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
        "remote": _git(["config", "--get", "remote.origin.url"], cwd),
        "dirty": bool(status) if status is not None else None,
    }


def _datalad_id(path):
    """The datalad dataset id at ``path``, or None if it is not one."""
    config = Path(path) / ".datalad" / "config"
    if not config.is_file():
        return None
    try:
        text = config.read_text()
    except OSError:
        return None
    match = re.search(r"^\s*id\s*=\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def sha256_file(path, max_bytes=DEFAULT_HASH_MAX_BYTES):
    """Hex sha256 of ``path``, or None if it is too large or unreadable.

    Files over ``max_bytes`` are deliberately skipped — see
    ``DEFAULT_HASH_MAX_BYTES``.
    """
    path = Path(path)
    try:
        if path.stat().st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def describe_path(path, max_bytes=DEFAULT_HASH_MAX_BYTES):
    """Describe one file or directory for a provenance record.

    Records existence, size, mtime and (for files) a checksum, plus the git and
    datalad state of whatever the path really points at. A symlink is resolved
    first, so an asset linked to a checkout elsewhere on disk is attributed to
    that checkout rather than to the link.
    """
    path = Path(path)
    record = {
        "path": str(path),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
        "resolved_path": None,
        "size_bytes": None,
        "mtime": None,
        "sha256": None,
        "git": None,
        "datalad_id": None,
    }
    if not path.exists():
        return record

    resolved = path.resolve()
    record["resolved_path"] = str(resolved)
    try:
        stat = resolved.stat()
        record["mtime"] = datetime.fromtimestamp(
            stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
        if resolved.is_file():
            record["size_bytes"] = stat.st_size
            record["sha256"] = sha256_file(resolved, max_bytes)
    except OSError:
        pass

    record["git"] = git_info(resolved)
    record["datalad_id"] = _datalad_id(resolved)
    return record


def _asset_entries(config):
    """Yield ``(name, target_path, declared_source)`` for every data asset.

    Covers both the ``files:`` section (assets with an ``output_file``) and the
    ``datasets:`` section, which projects write either as a plain path string
    or as a mapping with ``output_dir``/``output_file``.
    """
    for name, entry in (config.get("files") or {}).items():
        if isinstance(entry, dict):
            target = entry.get("output_file") or entry.get("output_dir")
            yield name, target, entry.get("source") or entry.get("url")

    for name, entry in (config.get("datasets") or {}).items():
        if isinstance(entry, dict):
            target = entry.get("output_dir") or entry.get("output_file")
            yield name, target, entry.get("source") or entry.get("url")
        else:
            yield name, entry, None


def _asset_mode(record, declared_source):
    """How the asset got here: symlink, datalad, download, copy or local."""
    if record["is_symlink"]:
        return "symlink"
    if record["datalad_id"]:
        return "datalad"
    if isinstance(declared_source, str) and declared_source.startswith(
            ("http://", "https://", "ftp://")):
        return "download"
    return "copy" if declared_source else "local"


def _drop_self_attribution(record, project_toplevel):
    """Forget git state that just describes the project repo itself.

    An asset downloaded into ``source_data/`` sits inside the project's own
    repository, and reporting the project's commit as that asset's version
    would be worse than reporting nothing — it looks like real input
    provenance. Only an asset in a repository of its own keeps its git block.
    """
    git = record.get("git")
    if git and project_toplevel and git.get("toplevel") == project_toplevel:
        record["git"] = None
    return record


@task(help={"output": "Where to write the manifest (default: the "
                      "`manifest_file` key in invoke.yaml)."})
def record_sources(c, output=None):
    """📜 Record what every data asset resolved to, into source_data/MANIFEST.json.

    Call at the end of ``fetch``. For each asset declared under ``files:`` or
    ``datasets:`` in invoke.yaml, records the path it landed at, what it really
    points at, its size and checksum, and the git commit / datalad id of the
    repository it belongs to — the part datalad would otherwise be needed for.

    Tolerant: an asset that was never fetched is recorded as absent, and no
    failure here can break a fetch.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    output : str, optional
        Manifest path. Defaults to the `manifest_file` key in invoke.yaml,
        falling back to `source_data/MANIFEST.json`.

    Examples
    --------
    ```bash
    inv provenance.record-sources
    ```
    """
    output_path = Path(output or c.config.get(
        "manifest_file", "source_data/MANIFEST.json"))
    max_bytes = c.config.get("provenance_hash_max_bytes", DEFAULT_HASH_MAX_BYTES)

    project_toplevel = (git_info(Path.cwd()) or {}).get("toplevel")

    assets = {}
    for name, target, declared_source in _asset_entries(c.config):
        if not target:
            print(f"⚠️  Asset '{name}' declares no output path — skipping")
            continue
        record = describe_path(target, max_bytes)
        record["declared_source"] = declared_source
        record["mode"] = _asset_mode(record, declared_source)
        assets[name] = _drop_self_attribution(record, project_toplevel)
        if not record["exists"]:
            print(f"⚠️  Asset '{name}' not present at {target} (recorded as absent)")

    manifest = {
        "schema": "airoh/manifest/1",
        "recorded_at": _utc_now(),
        "assets": assets,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"📜 Recorded {len(assets)} asset(s) → {output_path}")
    return output_path


def _declared_dependencies():
    """Package names this project declares, from pyproject.toml or requirements.txt.

    Recording the version of every installed package would bury the handful
    that matter in a few hundred transitive ones, so only declared
    dependencies are versioned.
    """
    names = []
    pyproject = Path("pyproject.toml")
    if pyproject.is_file():
        names = _parse_pyproject_dependencies(pyproject)
    if not names:
        requirements = Path("requirements.txt")
        if requirements.is_file():
            names = parse_requirements(requirements)
    return names


def _parse_pyproject_dependencies(path):
    """Package names in ``[project].dependencies``.

    Uses tomllib/tomli when available (tomllib is stdlib from 3.11) and falls
    back to reading the ``dependencies = [...]`` array directly, so airoh keeps
    its single-dependency footprint on older interpreters.
    """
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        return [requirement_name(spec)
                for spec in data.get("project", {}).get("dependencies", [])]
    except Exception:
        pass

    try:
        text = path.read_text()
    except OSError:
        return []
    match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    if not match:
        return []
    return [requirement_name(spec)
            for spec in re.findall(r"[\"']([^\"']+)[\"']", match.group(1))]


def requirement_name(spec):
    """Bare package name from a requirement spec ('numpy>=1.3' → 'numpy')."""
    return re.split(r"[\[<>=!~;\s]", spec.strip(), maxsplit=1)[0].lower()


def parse_requirements(path):
    """Package names in a requirements file, ignoring comments and flags."""
    names = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.append(requirement_name(line))
    return names


def _environment():
    """Python, platform and declared-dependency versions."""
    from importlib.metadata import PackageNotFoundError, version

    packages = {}
    for name in sorted(set(_declared_dependencies())):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    try:
        packages.setdefault("airoh", version("airoh"))
    except PackageNotFoundError:
        pass

    lockfiles = {}
    for name in ("uv.lock", "requirements.txt", "environment.yml"):
        if Path(name).is_file():
            lockfiles[name] = sha256_file(name)

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": packages,
        "lockfiles": lockfiles,
    }


def _output_files(output_dir, max_bytes, exclude):
    """Checksum every file the pipeline produced under ``output_dir``.

    The record itself, dotfiles and CONTENT.md are repository bookkeeping that
    lives in the output folder without being an output of the run.
    """
    outputs = {}
    root = Path(output_dir)
    if not root.is_dir():
        return outputs
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == exclude:
            continue
        if path.name == "CONTENT.md" or any(
                part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        outputs[str(path.relative_to(root))] = {
            "size_bytes": size,
            "sha256": sha256_file(path, max_bytes),
        }
    return outputs


@task(help={"output": "Where to write the record (default: the "
                      "`provenance_file` key in invoke.yaml).",
            "tasks": "Comma-separated names of the tasks that ran."})
def record_run(c, output=None, tasks=None):
    """📜 Record what produced the current outputs, into output_data/PROVENANCE.json.

    Call at the end of ``run``. Records the project's own git commit and dirty
    flag, the environment, a checksum of the input manifest (plus an inlined
    copy of each asset's commit and checksum, so the record still means
    something if the manifest is lost), and the size and checksum of every
    output file.

    This file changes on every run — that is the point of it. Do not try to
    make it stable; if the churn is unwanted, the answer is to run less often,
    not to record less.

    Tolerant: nothing here can break a run.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    output : str, optional
        Record path. Defaults to the `provenance_file` key in invoke.yaml,
        falling back to `output_data/PROVENANCE.json`.
    tasks : str, optional
        Comma-separated names of the tasks that ran, recorded as-is.

    Examples
    --------
    ```bash
    inv provenance.record-run --tasks run-qc-measures,run-notebooks
    ```
    """
    output_path = Path(output or c.config.get(
        "provenance_file", "output_data/PROVENANCE.json"))
    max_bytes = c.config.get("provenance_hash_max_bytes", DEFAULT_HASH_MAX_BYTES)
    manifest_path = Path(c.config.get("manifest_file", "source_data/MANIFEST.json"))
    output_dir = c.config.get("output_data_dir", "output_data")

    inputs = {"manifest_file": str(manifest_path), "manifest_sha256": None,
              "assets": {}}
    if manifest_path.is_file():
        inputs["manifest_sha256"] = sha256_file(manifest_path, max_bytes)
        try:
            manifest = json.loads(manifest_path.read_text())
            for name, record in (manifest.get("assets") or {}).items():
                inputs["assets"][name] = {
                    "resolved_path": record.get("resolved_path"),
                    "sha256": record.get("sha256"),
                    "commit": (record.get("git") or {}).get("commit"),
                }
        except (OSError, ValueError):
            print(f"⚠️  Could not read {manifest_path} — recording its hash only")
    else:
        print(f"⚠️  No manifest at {manifest_path} — run `invoke fetch` to create it")

    record = {
        "schema": "airoh/provenance/1",
        "recorded_at": _utc_now(),
        "tasks": [name.strip() for name in tasks.split(",")] if tasks else None,
        "command": " ".join(sys.argv),
        "repository": git_info(Path.cwd()),
        "environment": _environment(),
        "inputs": inputs,
        "outputs": _output_files(output_dir, max_bytes, output_path),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(f"📜 Recorded {len(record['outputs'])} output(s) → {output_path}")
    return output_path
