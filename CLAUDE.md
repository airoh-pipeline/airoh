# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install for development:
```bash
pip install -e .
pip install -r requirements_dev.txt
```

Run tests (unit only, skipping integration):
```bash
pytest -m "not integration"
```

Run integration test (clones `airoh-template` repo and runs a full workflow — requires network and Docker):
```bash
pytest -m integration
```

Lint (isort only, as configured):
```bash
ruff check --fix airoh/
```

Build documentation locally:
```bash
invoke make-docs
```

Build and publish the package:
```bash
hatch build
twine upload dist/*
```

## Architecture

`airoh` is a library of reusable [`invoke`](https://www.pyinvoke.org/) task definitions for reproducible research pipelines. Users import tasks from `airoh` into their project's `tasks.py` and call them via `invoke <module>.<task-name>`.

The library has three modules, each corresponding to a domain:

- **`airoh/utils.py`** — Python env setup, git submodules, editable installs, directory management, and Jupyter notebook execution (`run_figures`)
- **`airoh/containers.py`** — Docker and Apptainer lifecycle: build, archive to `.tar.gz`/`.sif`, download a prebuilt image, and run an `invoke` task inside a container
- **`airoh/datalad.py`** — Data retrieval via Datalad: install/get subdatasets, download single tracked files, and download+extract remote archives

**Configuration contract**: every task reads project-specific values from the consumer project's `invoke.yaml` via `c.config.get(key)`. Key names used across modules: `docker_image`, `docker_archive`, `datasets` (dict), `files` (dict with `url`/`output_file`), `notebooks_dir`, `figures_dir`. Tasks raise `ValueError` if a required key is missing.

**Container run tasks** (`docker_run`, `apptainer_run`) mount the current working directory into the container at `/home/jovyan/work` and execute an `invoke` task inside it — enabling fully containerized pipeline steps while keeping task definitions in the host `tasks.py`.

**Docs** are generated with `pdoc` from docstrings and deployed to GitHub Pages via `.github/workflows/docs.yml` on every push to `main`.

**The `tasks.py`** at the repo root defines only `make-docs` — it is the library's own build task, not an example for users.

**Testing** uses a single integration smoke test (`tests/test_airoh_template_smoke.py`) that clones the `airoh-template` repo (URL from `invoke.yaml`), installs the local `airoh` editable, and runs `invoke setup`, `invoke fetch`, `invoke run` end-to-end.
