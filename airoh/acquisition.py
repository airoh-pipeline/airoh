# src/airoh/acquisition.py
"""Tasks for acquiring data assets: download, symlink/copy, or git submodule.

These are the dependency-light acquisition tasks. Datalad-backed acquisition
lives in `airoh.datalad`, which is gated behind the optional `datalad` extra.
"""
import os
import shutil
from pathlib import Path
from urllib.request import Request, urlopen

from invoke import task


@task(
    help={
        "name": "Logical name of the file, as defined in the 'files' section of invoke.yaml."
    }
)
def download_data(c, name):
    """🌐 Download a single file from a URL using urllib.

    Looks up the file entry by logical name in invoke.yaml under the `files`
    key, then downloads it to the configured output path. Skips the download
    if the output file already exists and is non-empty. Uses a `.part` temp
    file during transfer and atomically replaces the target on success.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    name : str
        Logical name of the file to download, matching a key under `files`
        in invoke.yaml. Each entry must define `url` and `output_file`.

    Raises
    ------
    ValueError
        If `name` is not found under `files` in invoke.yaml, or if the
        matched entry is missing `url` or `output_file`.
    RuntimeError
        If the download completes with 0 bytes, or if any network error occurs.

    Examples
    --------
    ```bash
    inv acquisition.download-data --name my_dataset
    ```
    """
    files = c.config.get("files", {})
    if name not in files:
        raise ValueError(f"❌ No file config found for '{name}' in invoke.yaml.")

    entry = files[name]
    url = entry.get("url")
    output_file = entry.get("output_file")

    if not url or not output_file:
        raise ValueError(
            f"❌ Entry for '{name}' must define both 'url' and 'output_file'."
        )

    output_path = Path(output_file)
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")

    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"🫧 Skipping {name}: {output_file} already exists.")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.unlink(missing_ok=True)

    print(f"📥 Downloading '{name}' from {url}")
    print(f"📁 Target: {output_file}")

    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        },
    )

    try:
        with urlopen(req, timeout=60) as response, tmp_path.open("wb") as f:
            total = 0
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)

        if total == 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"❌ Downloaded 0 bytes for '{name}'.")

        tmp_path.replace(output_path)

    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"❌ Failed to download '{name}' from {url}: {e}") from e

    print(f"✅ Downloaded {name} to {output_file} ({output_path.stat().st_size} bytes)")


@task(
    help={
        "name": "Logical name of the asset, as defined in the 'files' section of invoke.yaml.",
        "source": (
            "Path to already-present data to link instead of downloading. "
            "Overrides the entry's optional 'source' key."
        ),
        "copy": "Copy the source data instead of symlinking it.",
    }
)
def fetch_data(c, name, source=None, copy=False):
    """📦 Make a data asset available: symlink existing data, or download it.

    Looks up the asset entry by logical name in invoke.yaml under the `files`
    key. Decides where the data comes from in this order:

    1. the `source` argument (e.g. ``invoke ... --source /path``),
    2. the entry's optional `source` key in invoke.yaml,
    3. otherwise, the entry's `url`, downloaded via `download_data`.

    When a source path is found, it is symlinked (or copied, with ``copy=True``)
    to the entry's `output_file`. Both single files and whole directories are
    supported. The operation is idempotent: a link that already points at the
    source is left untouched.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    name : str
        Logical name of the asset, matching a key under `files` in invoke.yaml.
        Each entry must define `output_file`, plus `url` and/or `source`.
    source : str, optional
        Path to existing data to link/copy. Takes precedence over the entry's
        `source` key. If neither is set, the asset is downloaded from `url`.
    copy : bool, optional
        Copy the source data instead of symlinking it (default: False).

    Raises
    ------
    ValueError
        If `name` is not found under `files`, if the entry has no `output_file`,
        if the resolved source path does not exist, or if `output_file` already
        exists as something other than a symlink.

    Examples
    --------
    ```bash
    inv acquisition.fetch-data --name my_dataset                       # download
    inv acquisition.fetch-data --name my_dataset --source /data/mine   # symlink
    inv acquisition.fetch-data --name my_dataset --source /data/mine --copy
    ```
    """
    files = c.config.get("files", {})
    if name not in files:
        raise ValueError(f"❌ No file config found for '{name}' in invoke.yaml.")

    entry = files[name]
    output_file = entry.get("output_file")
    if not output_file:
        raise ValueError(f"❌ Entry for '{name}' must define 'output_file'.")

    resolved_source = source or entry.get("source")
    if not resolved_source:
        download_data(c, name)
        return

    _link_data(name, resolved_source, output_file, copy=copy)


def _link_data(name, source, output_file, copy=False):
    """Symlink (or copy) existing data at `source` to `output_file`.

    Idempotent: if `output_file` is already a symlink pointing at `source`, it is
    left untouched; a symlink pointing elsewhere is repointed. A pre-existing
    non-symlink at `output_file` raises, to avoid clobbering real data.
    """
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output_file)

    if not source_path.exists():
        raise ValueError(f"❌ Source for '{name}' does not exist: {source_path}")

    if output_path.is_symlink():
        if output_path.resolve() == source_path:
            print(f"🫧 Skipping {name}: {output_file} already links to {source_path}.")
            return
        output_path.unlink()
    elif output_path.exists():
        raise ValueError(
            f"❌ {output_file} already exists and is not a symlink; remove it or "
            f"run `invoke clean` before linking '{name}'."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if copy:
        copier = shutil.copytree if source_path.is_dir() else shutil.copy2
        copier(source_path, output_path)
        print(f"✅ Copied {name} from {source_path} to {output_file}.")
    else:
        output_path.symlink_to(source_path)
        print(f"✅ Linked {name}: {output_file} -> {source_path}.")


@task
def ensure_submodule(c, path, recursive=True):
    """🔄 Ensure a git submodule is initialized and up to date.

    Initializes or updates a specified git submodule recursively.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    path : str
        Path to the submodule directory.
    recursive : boolean, default True
        Update all submodules recursively

    Examples
    --------
    ```bash
    inv acquisition.ensure-submodule --path src/external-lib
    ```
    """
    if recursive:
        flag_recursive = "--recursive"
    else:
        flag_recursive = ""

    if not os.path.exists(path) or not os.path.exists(os.path.join(path, ".git")):
        print(f"📦 Initializing submodule at {path}...")
        c.run(f"git submodule update --init {flag_recursive} {path}")
    else:
        print(f"🔄 Updating submodule at {path}...")
        c.run(f"git submodule update --remote {path}")
