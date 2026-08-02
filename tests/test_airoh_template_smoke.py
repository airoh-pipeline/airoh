import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from invoke import Context
from invoke.config import Config


@pytest.mark.integration
def test_airoh_template_smoke():
    """
    Clone the template repo (from invoke config), fetch, run, and verify.

    Needs network access and installs into the ambient environment, hence the
    `integration` marker: run with `pytest -m integration`.
    """
    invoke_config_path = Path(__file__).parents[1] / "invoke.yaml"
    with open(invoke_config_path, "r") as f:
        overrides = yaml.safe_load(f)
    config = Config(overrides=overrides)
    c = Context(config=config)
    template_url = c.config.get("template_repo")
    assert template_url, "No 'template_repo' defined in invoke.yaml."

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        subprocess.run(["git", "clone", template_url, str(tmpdir_path)], check=True)

        env = {"PYTHONUNBUFFERED": "1", **os.environ}

        # 👇 Install local airoh from this repo before calling invoke in the template
        subprocess.run(["pip", "install", "-e", str(Path(__file__).parents[1])],
                       check=True, env=env)

        subprocess.run(["invoke", "fetch"], cwd=tmpdir_path, check=True, env=env)
        subprocess.run(["invoke", "run"], cwd=tmpdir_path, check=True, env=env)

        output_dir = tmpdir_path / "output_data"
        assert output_dir.exists(), "Output directory was not created."
        assert any(output_dir.iterdir()), "Output directory is empty."

        # The provenance records are written by fetch and run respectively.
        assert (tmpdir_path / "source_data" / "MANIFEST.json").is_file()
        assert (output_dir / "PROVENANCE.json").is_file()

        # A freshly cloned template must pass its own consistency checks: if it
        # does not, every project generated from it starts out already drifted.
        subprocess.run(["invoke", "verify"], cwd=tmpdir_path, check=True, env=env)

        print("✅ Airoh template smoke test succeeded.")
