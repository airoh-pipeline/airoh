# src/airoh/datalad.py
from invoke import task
import os
import shlex
import shutil
from pathlib import Path

@task
def get_data(c, name):
    """
    📦 Install and retrieve a Datalad subdataset defined in the Invoke configuration.

    This task ensures that a given Datalad dataset specified under the
    `datasets` section of `invoke.yaml` is installed and that all of its
    contents are retrieved. It is typically used to make sure local data
    mirrors are up to date or to initialize subdatasets after cloning
    a parent dataset.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context, automatically provided when called as a task.
    name : str
        The name of the dataset as defined under `datasets` in `invoke.yaml`.

    Raises
    ------
    ValueError
        If the specified dataset name does not exist in the configuration.

    Examples
    --------
    ```bash
    inv datalad.get-data --name image10k
    ```
    """
    datasets = c.config.get("datasets", {})
    if name not in datasets:
        raise ValueError(f"❌ Dataset '{name}' not found in invoke.yaml under 'datasets'.")

    path = datasets[name]
    print(f"📦 Checking dataset '{name}' at: {path}")

    if not os.path.exists(path):
        print(f"📥 Installing subdataset '{name}'...")
        c.run(f"datalad install --recursive {path}")

    print(f"📥 Retrieving data for '{name}'...")
    c.run(f"datalad get {path}")
    print("✅ Done.")

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

@task
def import_archive(c, url, archive_name=None, target_dir=".", drop_archive=False):
    """🗃️ Download and extract an archive via Datalad.

    Retrieves a remote archive (e.g., from Zenodo or Figshare), extracts its contents
    into the target directory, and optionally drops the original archive from the annex.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context (automatically provided when running as a task).
    url : str
        The remote URL of the archive (e.g., `.zip`, `.tar.gz`).
    archive_name : str, optional
        Optional filename override. Defaults to the basename of the URL.
    target_dir : str, optional
        Directory to extract files into (default: current directory).
    drop_archive : bool, optional
        If True, drops the downloaded archive from the annex after extraction.

    Notes
    -----
    Supported archive types: `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.7z`.

    Examples
    --------
    ```bash
    inv datalad.import-archive --url https://zenodo.org/record/XXXX/files/data.zip
    inv datalad.import-archive --url https://figshare.com/... --target-dir ./data --drop-archive
    ```
    """
    archive_name = archive_name or os.path.basename(url)
    archive_path = os.path.join(target_dir, archive_name)

    import_file(c, url, archive_path)

    archive_exts = ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.7z']
    if not any(archive_path.endswith(ext) for ext in archive_exts):
        print("⚠️ Skipping extraction — file does not appear to be a supported archive.")
        return

    print(f"📦 Extracting archive content into {target_dir}...")
    c.run(f"datalad add-archive-content --delete --extract {shlex.quote(archive_path)} -d {shlex.quote(target_dir)}")

    if drop_archive:
        print(f"🧹 Dropping archive from annex: {archive_path}")
        c.run(f"datalad drop {shlex.quote(archive_path)}")

    print("✅ Archive import complete.")
