# src/airoh/datalad.py
"""Datalad-backed data retrieval: install/get subdatasets, tolerant of partial failures.

A datalad dataset is often only partly accessible — some content lives on
credentialed special remotes a given environment cannot reach. These tasks
retrieve whatever is reachable and warn (rather than abort) on the rest by
default, since a single inaccessible file must not fail a whole fetch. Pass
``strict=True`` (or ``--strict`` on the CLI) where that tolerance is wrong —
typically only in a smoke test, which must fail loudly when retrieval doesn't
work at all.

Requires the `datalad` CLI. Import succeeds even when it's absent — only
calling a task raises, so `invoke --list` and non-datalad projects are never
affected. Install with `pip install airoh[datalad]`.
"""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from invoke import task

# Some environments can reach github over HTTPS but not SSH, while a dataset's
# recorded submodule URLs are often SSH (git@github.com:...). When a first
# attempt fails we retry once, rewriting SSH github URLs to HTTPS for that
# single invocation only (no persistent change to the user's git config).
_HTTPS_OVERRIDE = "url.https://github.com/.insteadOf=git@github.com:"

# Some datalad content lives on credentialed SSH special remotes a given
# environment has no key for. Left to their defaults, git/ssh fall back to an
# interactive password prompt — which reads from stdin and, with no TTY
# attached to a non-interactive subprocess, blocks forever instead of failing.
# Forcing batch mode (no prompts) plus a bounded connect timeout makes an
# unreachable/unauthorized remote fail fast like any other inaccessible
# content, instead of hanging the whole fetch indefinitely.
_NONINTERACTIVE_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": (
        os.environ.get("GIT_SSH_COMMAND", "ssh")
        + " -o BatchMode=yes -o ConnectTimeout=15"
    ),
}

# Hard backstop in case some other step (not git/ssh auth) still stalls —
# generous enough for a real bulk retrieval, short enough to not hang forever.
_SUBPROCESS_TIMEOUT_SECONDS = 600

_FAILURES_FILENAME = ".fetch_failures.json"


def _require_datalad():
    if not shutil.which("datalad"):
        raise RuntimeError(
            "The 'datalad' CLI is required for airoh.datalad tasks. "
            "Install it with: pip install airoh[datalad]"
        )


def _dataset_entry(c, name):
    """Normalize a `datasets:` entry to a dict with `output_dir`, `url`, `source`.

    Accepts either a plain path string (back-compat: `{name: path}`) or a
    mapping with `output_dir`/`output_file`, `url`, and/or `source` — the
    same shape `airoh.provenance` reads (see `_asset_entries`).
    """
    datasets = c.config.get("datasets", {})
    if name not in datasets:
        raise ValueError(f"❌ Dataset '{name}' not found in invoke.yaml under 'datasets'.")

    entry = datasets[name]
    if isinstance(entry, str):
        return {"output_dir": entry, "url": None, "source": None}
    output_dir = entry.get("output_dir") or entry.get("output_file")
    if not output_dir:
        raise ValueError(f"❌ Entry for '{name}' must define 'output_dir'.")
    return {
        "output_dir": output_dir,
        "url": entry.get("url"),
        "source": entry.get("source"),
    }


def _is_datalad_dataset(root):
    root = Path(root)
    return (root / ".datalad").is_dir() or (root / ".git").exists()


def _run(extra_config, args, cwd):
    cmd = ["datalad"]
    if extra_config:
        cmd += ["-c", extra_config]
    cmd += args
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, env=_NONINTERACTIVE_ENV,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\ntimed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
        )


def datalad_get(paths, dataset_root, recursive=False, get_content=True, strict=False):
    """Run `datalad get` for `paths` (relative to `dataset_root`).

    With `get_content=False` only the subdataset tree (filenames) is
    installed, not the annexed content. On failure it retries once over HTTPS.
    If that also fails: by default (tolerant) it prints a warning and returns
    so the caller can proceed with whatever content is present; with
    `strict=True` it raises `RuntimeError` instead.
    """
    _require_datalad()
    root = Path(dataset_root)
    if not _is_datalad_dataset(root):
        if strict:
            raise RuntimeError(f"{root} is not a Datalad dataset")
        return
    if isinstance(paths, (str, Path)):
        paths = [paths]
    args = ["get"]
    if not get_content:
        args.append("-n")
    if recursive:
        args.append("-r")
    args += [str(p) for p in paths]

    result = _run(None, args, root)
    if result.returncode != 0:
        result = _run(_HTTPS_OVERRIDE, args, root)
    if result.returncode != 0:
        preview = ", ".join(str(p) for p in paths[:2])
        if strict:
            raise RuntimeError(
                f"datalad get failed for {preview} ...\n{result.stderr.strip()}"
            )
        print(f"⚠️  datalad get returned errors (continuing without): {preview} ...")


def update_subdataset(path, dataset_root, strict=False):
    """Advance an already-installed subdataset's pin via `datalad update --merge`.

    Only the git tree (commit pin, filenames) is refreshed — no annexed
    content is fetched, so this stays cheap even against a large derivative.
    Non-recursive: it advances only `path` itself, not any subdataset nested
    inside it. If `path` isn't installed yet (no `.git`), this is a no-op —
    `install_subdataset` handles that case instead.

    On failure it retries once over HTTPS. By default tolerant (prints a
    warning and returns) so a stale/unreachable remote never aborts the whole
    fetch; with `strict=True` it raises `RuntimeError`.
    """
    _require_datalad()
    root = Path(dataset_root) / path
    if not (root / ".git").exists():
        return
    result = _run(None, ["update", "--merge"], root)
    if result.returncode != 0:
        result = _run(_HTTPS_OVERRIDE, ["update", "--merge"], root)
    if result.returncode != 0:
        if strict:
            raise RuntimeError(
                f"datalad update --merge failed for {path}\n{result.stderr.strip()}"
            )
        print(f"⚠️  datalad update --merge returned errors (continuing without): {path}")


def install_subdataset(path, dataset_root, strict=False):
    """Install the subdataset at `path` (relative to `dataset_root`), no content.

    Runs `datalad get -n` rather than plain `git submodule update --init`:
    a nested subdataset (a subdataset inside another subdataset) cannot be
    reached by plain git submodule commands, which only see the top level.
    `datalad get -n` installs the intermediate dataset and the nested one in
    a single call, leaving large sibling subdatasets untouched
    (non-recursive). By default tolerant like `datalad_get` (only warns), so
    an inaccessible subdataset never aborts the whole run. With
    `strict=True` it raises if the subdataset is not actually installed
    afterwards (no `.git` at `path`).

    When the subdataset is already installed (`.git` already present at
    `path`), skips the `datalad get -n` install call (the expensive full
    install is only needed once) but still runs a lightweight
    `update_subdataset` refresh, so new upstream commits surface on every
    repeat fetch instead of only at first install.
    """
    _require_datalad()
    if (Path(dataset_root) / path / ".git").exists():
        update_subdataset(path, dataset_root, strict=strict)
        return
    datalad_get(path, dataset_root, get_content=False, strict=strict)
    if strict and not (Path(dataset_root) / path / ".git").exists():
        raise RuntimeError(
            f"subdataset {path} was not installed (no .git at "
            f"{Path(dataset_root) / path})"
        )


def ensure_dataset(dest, url=None, source=None):
    """Make a datalad dataset checkout available at `dest`: symlink, clone, or no-op.

    Mirrors `airoh.acquisition.fetch_data`'s "symlink existing data, else
    acquire it" decision, adapted for a datalad dataset:

    - if `dest` already exists (a prior symlink or clone), this is a no-op;
    - else if `source` is given, symlink `dest` to the existing checkout at
      `source` (does not run `datalad get` — un-fetched content in that
      checkout stays un-fetched; see the template's CLAUDE.md for why
      symlinking a datalad dataset only exposes what's already present);
    - else if `url` is given, `datalad clone url dest` (dataset tree only, no
      annexed content — a later `get_data`/`install_subdataset` call
      retrieves what's actually needed).

    Raises `ValueError` if neither `source` nor `url` is given and `dest`
    does not already exist.
    """
    dest_path = Path(dest)
    if dest_path.exists():
        print(f"🫧 Skipping: {dest} already exists.")
        return

    if source:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise ValueError(f"❌ Source does not exist: {source_path}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.symlink_to(source_path)
        print(f"✅ Linked {dest} -> {source_path}.")
        return

    if url:
        _require_datalad()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["datalad", "clone", url, str(dest_path)], check=True,
                       env=_NONINTERACTIVE_ENV, timeout=_SUBPROCESS_TIMEOUT_SECONDS)
        print(f"✅ Cloned {dest} from {url} (no content fetched yet).")
        return

    raise ValueError(f"❌ {dest} does not exist and neither 'source' nor 'url' was given.")


def load_known_failures(cache_dir):
    """Return the set of (root-relative) paths that previously failed to fetch."""
    path = Path(cache_dir) / _FAILURES_FILENAME
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text()))


def save_known_failures(cache_dir, failures):
    """Persist the given set of (root-relative) paths as the known failures."""
    path = Path(cache_dir) / _FAILURES_FILENAME
    path.write_text(json.dumps(sorted(failures), indent=2) + "\n")


def prefetch_pattern(dataset_root, pattern, subdir="", skip_set=(), match=None):
    """Glob `dataset_root/subdir` for `pattern`, `datalad get` what's missing.

    Generic core of a project's "fetch every small file the analysis reads"
    step. Files already present on disk are never re-requested; among the
    rest, only those whose root-relative path is not in `skip_set` (see
    `load_known_failures`) are handed to `datalad_get` as one batch.

    `match(path) -> bool` is an optional extra filter (e.g. narrow by
    subject/session) applied to every glob hit before it counts as matched.

    Returns `(already_present, newly_fetched, skipped, new_failures,
    resolved)`: counts of files already on disk and newly fetched, a count of
    files skipped because they were previously known to fail, and the sets of
    root-relative paths that newly failed or newly succeeded this call — for
    the caller to update its failure cache with `save_known_failures`.
    """
    root = Path(dataset_root)
    target_dir = root / subdir if subdir else root
    if not target_dir.is_dir():
        return 0, 0, 0, set(), set()

    matched = [p for p in target_dir.rglob(pattern) if match is None or match(p)]
    rel_paths = {p: str(p.relative_to(root)) for p in matched}
    missing = [p for p in matched if not p.is_file()]
    already_present = len(matched) - len(missing)

    to_attempt = [p for p in missing if rel_paths[p] not in skip_set]
    skipped = len(missing) - len(to_attempt)

    if to_attempt:
        datalad_get([p.relative_to(root) for p in to_attempt], root)

    newly_fetched, new_failures, resolved = 0, set(), set()
    for p in to_attempt:
        if p.is_file():
            newly_fetched += 1
            resolved.add(rel_paths[p])
        else:
            new_failures.add(rel_paths[p])

    return already_present, newly_fetched, skipped, new_failures, resolved


@task(
    help={
        "name": "Logical name of the dataset, as defined in the 'datasets' section of invoke.yaml.",
        "source": "Path to an existing checkout to symlink instead of cloning.",
    }
)
def install_dataset(c, name, source=None):
    """📦 Make a datalad dataset checkout available: symlink existing, or clone.

    Looks up `name` under `datasets` in invoke.yaml, then calls
    `ensure_dataset` with the entry's `output_dir` as `dest` and `url`. A
    `--source` argument (or the entry's own `source` key) symlinks an
    existing checkout instead of cloning — this does not run `datalad get`,
    so only content already present at `source` becomes visible.

    Examples
    --------
    ```bash
    inv datalad.install-dataset --name cneuromod
    inv datalad.install-dataset --name cneuromod --source /data/cneuromod.all
    ```
    """
    entry = _dataset_entry(c, name)
    ensure_dataset(entry["output_dir"], url=entry["url"], source=source or entry["source"])


@task(
    help={
        "name": "Logical name of the dataset, as defined in the 'datasets' section of invoke.yaml.",
        "path": "Path (relative to the dataset root) to retrieve; defaults to the whole dataset.",
        "recursive": "Recurse into nested subdatasets.",
        "strict": "Raise instead of warning if retrieval fails.",
    }
)
def get_data(c, name, path=None, recursive=False, strict=False):
    """📥 Retrieve content for a datalad dataset (or a path within it).

    Looks up `name` under `datasets` in invoke.yaml and runs `datalad get` at
    its `output_dir`, narrowed to `path` when given. Tolerant of partial
    failures by default — pass `--strict` to raise instead (e.g. in a smoke
    test, which must fail loudly when retrieval doesn't work).

    Examples
    --------
    ```bash
    inv datalad.get-data --name cneuromod
    inv datalad.get-data --name cneuromod --path sub-01/anat --strict
    ```
    """
    entry = _dataset_entry(c, name)
    target = path or "."
    datalad_get(target, entry["output_dir"], recursive=recursive, strict=strict)


@task(
    help={
        "name": "Logical name of the dataset, as defined in the 'datasets' section of invoke.yaml.",
        "strict": "Raise instead of warning if the update fails.",
    }
)
def update_dataset(c, name, strict=False):
    """🔄 Advance a dataset's pin via `datalad update --merge`.

    Looks up `name` under `datasets` in invoke.yaml and calls
    `update_subdataset` on its `output_dir`. No-op if the dataset isn't
    installed yet (no `.git`); use `install_dataset` first.

    Examples
    --------
    ```bash
    inv datalad.update-dataset --name cneuromod
    ```
    """
    entry = _dataset_entry(c, name)
    output_dir = Path(entry["output_dir"])
    update_subdataset(output_dir.name, str(output_dir.parent), strict=strict)


@task
def import_file(c, name):
    """🌐 Download a single file tracked via Datalad.

    Finds an entry under the `files` section of `invoke.yaml`, downloads the file
    if it doesn't already exist, and tracks it with Datalad.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context (automatically provided when running as a task).
    name : str
        The file name key as defined under `files` in `invoke.yaml`.

    Raises
    ------
    ValueError
        If the file configuration is missing or incomplete.

    Examples
    --------
    ```bash
    inv datalad.import-file --name stimuli
    ```
    """
    _require_datalad()
    files = c.config.get("files", {})
    if name not in files:
        raise ValueError(f"❌ No file config found for '{name}' in invoke.yaml.")

    entry = files[name]
    url = entry.get("url")
    output_file = entry.get("output_file")

    if not url or not output_file:
        raise ValueError(f"❌ Entry for '{name}' must define both 'url' and 'output_file'.")

    output_path = Path(output_file)
    if output_path.exists():
        print(f"🫧 Skipping {name}: {output_file} already exists.")
        return

    c.run(f"datalad download-url -O {shlex.quote(output_file)} {shlex.quote(url)}")
    print(f"✅ Downloaded {name} to {output_file}")
