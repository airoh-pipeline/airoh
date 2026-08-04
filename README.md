_Because reproducible science takes clean tasks. And why don't you have a cup of relaxing jasmin tea?_

**airoh** is a lightweight Python task library built with [`invoke`](https://www.pyinvoke.org/), designed for reproducible research workflows. It provides pre-written, modular task definitions that can be easily reused in your own `tasks.py` file — no boilerplate, just useful automation. Access the documentation of the library on the [airoh docs website](https://airoh-pipeline.github.io/airoh/airoh.html) for a list of available airoh tasks. 

## Installation
Installation through PIP:
```bash
pip install airoh
```

For local deployment:

```bash
git clone https://github.com/airoh-pipeline/airoh.git
cd airoh
pip install -e .
```

## Usage

You can use `airoh` in your project simply by importing tasks in your `tasks.py` file.

### Minimal Example

```python
# tasks.py
from airoh.utils import run_notebooks, setup_env_python
```

Now you can call:

```bash
invoke run-notebooks
invoke setup-env-python
```

### Keeping a project honest

Two modules exist for the parts of reproducibility that no pipeline run can
check by itself.

`airoh.verify` compares a project against its own documentation — the task list
in the README, the packages in `requirements.txt` versus `pyproject.toml`, the
paths the docs name, the entries in each data folder versus its `CONTENT.md`,
the size and type of what git tracks. It runs a flat list of independent checks
and exits non-zero when any fails. Wire it up as its own task and run it before
committing; never call it from `run`, so that reproducing results never depends
on documentation hygiene.

`airoh.provenance` writes two records: `record_sources` describes what every
declared asset actually resolved to (a URL, a real path behind a symlink, the
commit of the repository it belongs to), and `record_run` describes what
produced the current outputs (project commit, environment, input manifest,
output checksums). Neither can fail a pipeline — a provenance record is
documentation, not a precondition. Where datalad is in use it remains the only
thing that can *retrieve* a past state; these records are what you get without
it.

```python
# tasks.py
from airoh.verify import verify            # noqa: F401  (exposes `invoke verify`)
from airoh.provenance import record_run, record_sources
```

### The Inkscape montage pattern

`airoh.figures` solves a problem every multi-panel-figure pipeline has: the
layout is authored by hand in Inkscape, but the panels are rendered by
matplotlib, and the two disagree about size. The montage SVG is the single
source of truth for layout — `figure_layout` reads out the box each linked
panel is placed in and writes it to `panel_sizes.json`, then a notebook calls
`panel_size(name, default)` to render that panel at exactly the size it will
be placed at, so text is never stretched. `compose_figure` renders the montage
to PNG/PDF/SVG/EPS via Inkscape, an optional system binary — a missing one
warns and skips the export rather than failing the run.

```python
# tasks.py
from airoh.figures import clean_figure, compose_figure, figure_layout
```

## Requirements

* Python ≥ 3.8
* [`invoke`](https://www.pyinvoke.org/) ≥ 2.0
* Docker (for container tasks)
* Apptainer (optional, for `.sif` support)
* `jupyter` (if using `run-notebooks`)
* Inkscape (optional, only for `compose-figure`)

Note that a few more requirements are required for development, in particular [pdoc](https://pdoc.dev/docs/pdoc.html) which is used to generate the documentation website.

## Philosophy

Inspired by Uncle Iroh from *Avatar: The Last Airbender*, `airoh` aims to bring simplicity, reusability, and clarity to research infrastructure — one well-structured task at a time. It is meant to support a concrete implementation of the [YODA principles](https://tinyurl.com/yoda-datalad).

## License

MIT © airoh contributors
