# src/airoh/utils.py
import os
import shutil
from pathlib import Path
from invoke import task

@task
def setup_env_python(c, reqs="requirements.txt"):
    """🐍 Set up a Python environment from a requirements file.

    Installs dependencies listed in a requirements file using pip.
    This is typically the first step in configuring a new development
    environment for the project.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    reqs : str, optional
        Path to the requirements file (default: "requirements.txt").

    Raises
    ------
    FileNotFoundError
        If the requirements file cannot be found.

    Examples
    --------
    ```bash
    inv utils.setup-env-python
    ```
    """
    if not os.path.exists(reqs):
        raise FileNotFoundError(f"⚠️ Requirements file not found: {reqs}")

    print(f"🐍 Installing Python requirements from {reqs}...")
    c.run(f"pip install -r {reqs}")

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
    inv utils.ensure-submodule --path src/external-lib
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

@task
def install_local(c, path):
    """🔧 Install a local Python package in editable mode.

    Performs a pip install with the `-e` flag, allowing live code
    updates without reinstalling the package.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    path : str
        Path to the Python package directory.

    Raises
    ------
    FileNotFoundError
        If the given path does not exist.

    Examples
    --------
    ```bash
    inv utils.install-local --path src/my_module
    ```
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"❌ Package path not found: {path}")

    print(f"🔧 Installing package from {path} in editable mode...")
    c.run(f"pip install -e {path}")
    print("✅ Editable install complete.")

@task
def ensure_dir_exist(c, name):
    """📁 Ensure a directory exists, creating it if needed.

    Retrieves the directory path from the Invoke configuration and creates
    it if it does not exist.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    name : str
        Key in invoke.yaml referring to a directory path.

    Raises
    ------
    ValueError
        If the provided key does not resolve to a string path.
    """
    output_dir = c.config.get(name)
    if not isinstance(output_dir, str):
        raise ValueError("❌ 'output_data_dir' not found or not a string in invoke.yaml")

    output_path = Path(output_dir)
    if not output_path.exists():
        output_path.mkdir(parents=True)
        print(f"📁 Created output directory: {output_path}")
    else:
        print(f"✅ Output directory already exists: {output_path}")

@task
def clean_folder(c, name, pattern=None):
    """🧹 Clean files or directories from a given path.

    Removes files or directories based on an invoke.yaml key.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    name : str
        Key in invoke.yaml whose value is the directory path.
    pattern : str, optional
        Glob pattern of files to delete (e.g., '*.png'). If not provided,
        the entire folder will be removed.

    Examples
    --------
    ```bash
    inv utils.clean-folder --name output_data_dir --pattern '*.tmp'
    ```
    """
    dir_name = c.config.get(name)
    if not isinstance(dir_name, str):
        raise ValueError(f"❌ Could not resolve a path from invoke config for key: '{name}'")

    if not os.path.exists(dir_name):
        print(f"🫧 Nothing to clean: {name}")
        return

    if pattern:
        path = Path(dir_name)
        files = list(path.glob(pattern))
        if not files:
            print(f"🫧 No files matching '{pattern}' in {dir_name}")
            return
        for f in files:
            f.unlink()
            print(f"🧹 Removed: {f}")
    else:
        shutil.rmtree(dir_name)
        print(f"💥 Removed {name} at {dir_name}")

def _build_env_from_config(c, keys):
    """⚙️ Build an environment dictionary from Invoke config keys.

    Converts selected keys from invoke.yaml into environment variables,
    resolving them as absolute paths.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    keys : list[str]
        Keys to retrieve from configuration.

    Returns
    -------
    dict
        Environment dictionary with uppercase variable names.

    Raises
    ------
    ValueError
        If a key is missing from configuration.
    """
    env = dict(os.environ)
    for key in keys:
        val = c.config.get(key)
        if val is None:
            raise ValueError(f"❌ Missing key in invoke config: {key}")
        path = Path(val).resolve()
        env_key = key.upper()
        env[env_key] = str(path)
    return env

@task
def run_figures(c, notebooks_path=None, figures_base=None, keys=None):
    """📊 Execute all Jupyter notebooks generating figures.

    Scans the figure notebook directory and executes each notebook that
    doesn't yet have a corresponding output folder. Environment variables
    can be injected based on invoke.yaml configuration.

    Parameters
    ----------
    c : invoke.Context
        The Invoke context.
    notebooks_path : str, optional
        Path to the folder containing notebooks. Defaults to `notebooks_dir`.
    figures_base : str, optional
        Base directory for figure outputs. Defaults to `figures_dir`.
    keys : list[str], optional
        List of configuration keys to expose as environment variables.

    Examples
    --------
    ```bash
    inv utils.run-figures
    ```
    """
    notebooks_path = Path(notebooks_path or c.config.get("notebooks_dir", "code/figures"))
    figures_base = Path(figures_base or c.config.get("figures_dir", "output_data/Figures"))

    env = None
    if keys:
        env = _build_env_from_config(c, keys)

    if not notebooks_path.exists():
        print(f"⚠️ Notebooks directory not found: {notebooks_path}")
        return

    notebooks = sorted(notebooks_path.glob("*.ipynb"))

    if not notebooks:
        print(f"⚠️ No notebooks found in {notebooks_path}/")
        return

    for nb in notebooks:
        fig_name = nb.stem
        fig_output_dir = figures_base / fig_name

        if fig_output_dir.exists():
            print(f"✅ Skipping {nb.name} (output exists at {fig_output_dir})")
            continue

        print(f"📈 Running {nb.name}...")
        c.run(f"jupyter nbconvert --to notebook --execute --inplace {nb}", env=env)

    print("🎉 All figure notebooks processed.")
