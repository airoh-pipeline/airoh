_Because reproducible science takes clean tasks. And why don't you have a cup of relaxing jasmin tea?_

**airoh** is a lightweight Python task library built with [`invoke`](https://www.pyinvoke.org/), designed for reproducible research workflows. It provides pre-written, modular task definitions that can be easily reused in your own `tasks.py` file — no boilerplate, just useful automation. Access the documentation of the library on the [airoh docs website](https://airoh-pipeline.github.io/airoh/airoh.html) for a list of available airoh tasks. 

## Installation
Installation through PIP:
```bash
pip install airoh
```

For local deployment:

```bash
git clone https://github.com/simexp/airoh.git
cd airoh
pip install -e .
```

## Usage

You can use `airoh` in your project simply by importing tasks in your `tasks.py` file.

### Minimal Example

```python
# tasks.py
from airoh.utils import run_figures, setup_env_python
```

Now you can call:

```bash
invoke run-figures
invoke setup-env-python
```
## Requirements

* Python ≥ 3.8
* [`invoke`](https://www.pyinvoke.org/) ≥ 2.0
* Docker (for container tasks)
* Apptainer (optional, for `.sif` support)
* `jupyter` (if using `run-figures`)

Note that a few more requirements are required for development, in particular [pdoc](https://pdoc.dev/docs/pdoc.html) which is used to generate the documentation website.

## Philosophy

Inspired by Uncle Iroh from *Avatar: The Last Airbender*, `airoh` aims to bring simplicity, reusability, and clarity to research infrastructure — one well-structured task at a time. It is meant to support a concrete implementation of the [YODA principles](https://handbook.datalad.org/en/latest/basics/101-127-yoda.html).

## License

MIT © airoh contributors
