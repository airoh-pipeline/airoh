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

Release a new version (publishes to PyPI via CI using Trusted Publishing):
1. Bump `version` in `pyproject.toml`.
2. Tag the commit `vX.Y.Z` and push the tag.
3. Publish a GitHub Release for that tag — this triggers `.github/workflows/publish.yml`, which builds the package and uploads it to PyPI via OIDC (no stored token).

Manual fallback (local `~/.pypirc` token required):
```bash
hatch build
twine upload dist/*
```

## Architecture

`airoh` is a library of reusable [`invoke`](https://www.pyinvoke.org/) task definitions for reproducible research pipelines. Users import tasks from `airoh` into their project's `tasks.py` and call them via `invoke <module>.<task-name>`.

The library has seven modules, each corresponding to a domain:

- **`airoh/utils.py`** — Python env setup, git submodules, editable installs, directory management, and Jupyter notebook execution (`run_notebooks`)
- **`airoh/containers.py`** — Docker and Apptainer lifecycle: build, archive to `.tar.gz`/`.sif`, download a prebuilt image, and run an `invoke` task inside a container
- **`airoh/acquisition.py`** — dependency-light data acquisition: download a single file (`download_data`), symlink/copy already-present data or fall back to downloading (`fetch_data`), and git submodule init/update (`ensure_submodule`)
- **`airoh/datalad.py`** — Datalad-backed data retrieval, gated behind the optional `datalad` extra: make a dataset checkout available (`ensure_dataset`/`install_dataset`), retrieve content tolerant of partial failures (`datalad_get`/`get_data`), install/update nested subdatasets (`install_subdataset`/`update_subdataset`/`update_dataset`), a failure cache so repeat fetches skip known-inaccessible files (`load_known_failures`/`save_known_failures`), a generic glob-and-fetch helper for projects to compose their own prefetch step (`prefetch_pattern`), and single tracked-file download (`import_file`). Import always succeeds without the `datalad` CLI on PATH — only calling a task raises.
- **`airoh/provenance.py`** — records what fetch and run actually did: `record_sources` writes `source_data/MANIFEST.json`, `record_run` writes `output_data/PROVENANCE.json`, both checksummed and git/datalad-aware
- **`airoh/figures.py`** — the Inkscape montage/panel-sizing pattern: a hand-authored SVG montage is the single source of truth for a multi-panel figure's layout; `figure_layout` parses it and writes `figures_dir/panel_sizes.json`, notebooks call `panel_size(name, default)` to render each panel at exactly its placed size, and `compose_figure` renders the montage to PNG/PDF/SVG/EPS via Inkscape (optional binary, tolerant if absent)
- **`airoh/verify.py`** — `invoke verify`: a flat list of independent checks that code, config, data and docs still agree (task list vs. docs, dependency files vs. each other, doc paths, `CONTENT.md` coverage, config keys, tracked file sizes, provenance freshness, lint)

**Configuration contract**: every task reads project-specific values from the consumer project's `invoke.yaml` via `c.config.get(key)`. Key names used across modules: `docker_image`, `docker_archive`, `datasets` (dict, either `{name: path}` or `{name: {output_dir, url, source}}`), `files` (dict with `url`/`output_file`), `figures` (dict, `{name: {svg, output, dpi}}`), `notebooks_dir`, `figures_dir`, `output_data_dir`, `source_data_dir`, `verify`, `manifest_file`, `provenance_file`, `provenance_hash_max_bytes`. Tasks raise `ValueError` if a required key is missing. `airoh/verify.py`'s `AIROH_CONFIG_KEYS` must list every key read this way, or `check_config_keys` reports it as an unused project key.

**Container run tasks** (`docker_run`, `apptainer_run`) mount the current working directory into the container at `/home/jovyan/work` and execute an `invoke` task inside it — enabling fully containerized pipeline steps while keeping task definitions in the host `tasks.py`.

**Docs** are generated with `pdoc` from docstrings and deployed to GitHub Pages via `.github/workflows/docs.yml` on every push to `main`.

**The `tasks.py`** at the repo root defines only `make-docs` — it is the library's own build task, not an example for users.

**Testing** uses a single integration smoke test (`tests/test_airoh_template_smoke.py`) that clones the `airoh-template` repo (URL from `invoke.yaml`), installs the local `airoh` editable, and runs `invoke fetch`, `invoke run`, `invoke verify` end-to-end (there is no `invoke setup` task).
