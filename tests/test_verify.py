"""Unit tests for airoh.verify — each check in its passing and failing shape."""

import subprocess

import pytest

from airoh.verify import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    Project,
    check_config_keys,
    check_content_md,
    check_dependencies,
    check_doc_paths,
    check_provenance,
    check_task_list,
    check_tracked_size,
    config_keys_used,
    documented_task_names,
    run_checks,
    task_names,
)

CONFIG = {"source_data_dir": "source_data", "output_data_dir": "output_data"}

TASKS_PY = '''
from invoke import task

@task
def fetch(c):
    """Fetch."""
    print(c.config.get("source_data_dir"))

@task(pre=[fetch])
def run_model(c):
    """Run."""
    print(c.config.get("output_data_dir"))
'''


@pytest.fixture
def project(tmp_path):
    """A minimal, internally consistent airoh project."""
    (tmp_path / "tasks.py").write_text(TASKS_PY)
    (tmp_path / "invoke.yaml").write_text(
        "source_data_dir: source_data\noutput_data_dir: output_data\n")
    (tmp_path / "README.md").write_text(
        "Run `invoke fetch` then `invoke run-model`.\n")
    (tmp_path / "CLAUDE.md").write_text("See `tasks.py`.\n")
    for name in ("source_data", "output_data"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CONTENT.md").write_text(f"# {name}\n")
    return tmp_path


def make(root, config=None):
    return Project(config or CONFIG, root)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def test_task_names_reads_decorated_functions(project):
    assert task_names(project / "tasks.py") == ["fetch", "run-model"]


def test_task_names_returns_none_on_unparseable_file(tmp_path):
    (tmp_path / "tasks.py").write_text("def broken(:\n")
    assert task_names(tmp_path / "tasks.py") is None


def test_documented_task_names_ignores_prose_and_placeholders():
    text = ("This repo has reusable invoke tasks.\n"
            "Run `invoke fetch` and `invoke run-model`.\n"
            "Each asset gets an `invoke fetch-{name}` task.\n")
    assert documented_task_names(text) == {"fetch", "run-model"}


def test_config_keys_used_ignores_chained_lookups(tmp_path):
    (tmp_path / "tasks.py").write_text(
        'x = c.config.get("datasets", {}).get("inner", {})\n'
        'y = c.config.get("output_data_dir")\n')
    assert config_keys_used(tmp_path / "tasks.py") == {"datasets", "output_data_dir"}


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def test_task_list_passes_when_docs_match(project):
    assert check_task_list(make(project)).status == PASS


def test_task_list_fails_on_undocumented_task(project):
    (project / "README.md").write_text("Run `invoke fetch`.\n")
    finding = check_task_list(make(project))
    assert finding.status == FAIL
    assert any("run-model" in detail for detail in finding.details)


def test_task_list_fails_on_documented_phantom(project):
    """The drift that motivated this check: a task the docs invent."""
    (project / "README.md").write_text(
        "Run `invoke fetch`, `invoke run-model`, `invoke run-smoke`.\n")
    finding = check_task_list(make(project))
    assert finding.status == FAIL
    assert any("run-smoke" in detail for detail in finding.details)


def test_task_list_accepts_a_table_row(project):
    """A README table names a task without spelling out `invoke`."""
    (project / "README.md").write_text(
        "Run `invoke fetch`.\n\n| Task | Description |\n"
        "| --- | --- |\n| `run-model` | Fits the model |\n")
    assert check_task_list(make(project)).status == PASS


def test_task_list_still_flags_a_phantom_invocation(project):
    """Mentioning a name is documentation; `invoke <name>` is a claim it exists."""
    (project / "README.md").write_text(
        "Run `invoke fetch`, `invoke run-model`.\n"
        "The `conda-forge` channel is fine, but `invoke run-nothing` is not.\n")
    finding = check_task_list(make(project))
    assert finding.status == FAIL
    assert finding.details == ["documented but not defined: run-nothing"]


def test_dependencies_pass_when_files_agree(project):
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["numpy>=1.0", "pandas"]\n')
    (project / "requirements.txt").write_text("numpy>=1.0\npandas\n")
    assert check_dependencies(make(project)).status == PASS


def test_dependencies_fail_when_requirements_lags(project):
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["numpy", "pandas", "nilearn"]\n')
    (project / "requirements.txt").write_text("numpy\n")
    finding = check_dependencies(make(project))
    assert finding.status == FAIL
    assert any("nilearn" in detail for detail in finding.details)


def test_dependencies_skip_conda_channels(project):
    (project / "requirements.txt").write_text("numpy\n")
    (project / "environment.yml").write_text(
        "channels:\n  - conda-forge\ndependencies:\n  - python=3.12\n  - numpy\n")
    assert check_dependencies(make(project)).status == PASS


def test_dependencies_skip_with_one_file(project):
    (project / "requirements.txt").write_text("numpy\n")
    assert check_dependencies(make(project)).status == SKIP


def test_doc_paths_pass_for_existing_paths(project):
    (project / "CLAUDE.md").write_text("See `tasks.py` and `invoke.yaml`.\n")
    assert check_doc_paths(make(project)).status == PASS


def test_doc_paths_fail_for_missing_source_file(project):
    (project / "CLAUDE.md").write_text("Delete `analysis/simulation.py`.\n")
    finding = check_doc_paths(make(project))
    assert finding.status == FAIL
    assert any("simulation.py" in detail for detail in finding.details)


def test_doc_paths_warn_for_unproduced_output(project):
    (project / "output_data" / "CONTENT.md").write_text("- `scatter.png` — a plot.\n")
    finding = check_doc_paths(make(project))
    assert finding.status == WARN
    assert any("scatter.png" in detail for detail in finding.details)


def test_doc_paths_ignore_placeholders_and_conventions(project):
    (project / "CLAUDE.md").write_text(
        "Writes `output_data/tables/{dataset}.tsv` from `*_bold.json` files.\n")
    assert check_doc_paths(make(project)).status == PASS


def test_doc_paths_resolve_bare_basenames(project):
    """Docs name a file without saying where it lives."""
    (project / "analysis").mkdir()
    (project / "analysis" / "model.py").write_text("")
    (project / "CLAUDE.md").write_text("The heavy lifting is in `model.py`.\n")
    assert check_doc_paths(make(project)).status == PASS


def test_doc_paths_warn_for_a_missing_bare_filename(project):
    """A bare name claims a file exists somewhere, not that it exists here."""
    (project / "CLAUDE.md").write_text("`run` writes `PROVENANCE.json`.\n")
    finding = check_doc_paths(make(project))
    assert finding.status == WARN
    assert any("PROVENANCE.json" in detail for detail in finding.details)


def test_doc_paths_keep_leading_dot_directories(project):
    """`.claude/skills` must not be mangled into `claude/skills`."""
    (project / ".claude" / "skills").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("Skills live in `.claude/skills/`.\n")
    assert check_doc_paths(make(project)).status == PASS


def test_doc_paths_strip_a_leading_dot_slash(project):
    (project / "CLAUDE.md").write_text("See `./tasks.py`.\n")
    assert check_doc_paths(make(project)).status == PASS


def test_doc_paths_honour_ignore_list(project):
    (project / "CLAUDE.md").write_text("See `analysis/simulation.py`.\n")
    config = dict(CONFIG, verify={"ignore_paths": ["analysis/*"]})
    assert check_doc_paths(make(project, config)).status == PASS


def test_content_md_passes_when_entries_are_described(project):
    (project / "output_data" / "result.csv").write_text("a,b\n")
    (project / "output_data" / "CONTENT.md").write_text("- `result.csv` — output.\n")
    assert check_content_md(make(project)).status == PASS


def test_content_md_fails_on_undocumented_entry(project):
    (project / "output_data" / "surprise.csv").write_text("a,b\n")
    finding = check_content_md(make(project))
    assert finding.status == FAIL
    assert any("surprise.csv" in detail for detail in finding.details)


def test_content_md_ignores_provenance_records(project):
    (project / "output_data" / "PROVENANCE.json").write_text("{}")
    (project / "source_data" / "MANIFEST.json").write_text("{}")
    assert check_content_md(make(project)).status == PASS


def test_config_keys_pass_when_declared(project):
    assert check_config_keys(make(project)).status == PASS


def test_config_keys_fail_on_undeclared_key(project):
    (project / "invoke.yaml").write_text("source_data_dir: source_data\n")
    finding = check_config_keys(make(project))
    assert finding.status == FAIL
    assert any("output_data_dir" in detail for detail in finding.details)


def test_config_keys_warn_on_unused_key(project):
    (project / "invoke.yaml").write_text(
        "source_data_dir: source_data\noutput_data_dir: output_data\ncode_dir: analysis\n")
    finding = check_config_keys(make(project))
    assert finding.status == WARN
    assert any("code_dir" in detail for detail in finding.details)


def test_provenance_warns_when_record_missing(project):
    (project / "output_data" / "result.csv").write_text("a,b\n")
    finding = check_provenance(make(project))
    assert finding.status == WARN
    assert any("PROVENANCE.json" in detail for detail in finding.details)


def test_provenance_skips_on_empty_project(project):
    assert check_provenance(make(project)).status == SKIP


def test_provenance_ignores_bookkeeping_when_dating_outputs(project):
    """Editing CONTENT.md must not make the record look stale."""
    (project / "output_data" / "result.csv").write_text("a,b\n")
    (project / "output_data" / "CONTENT.md").write_text("- `result.csv`\n")
    (project / "source_data" / "MANIFEST.json").write_text("{}")
    record = project / "output_data" / "PROVENANCE.json"
    record.write_text("{}")
    (project / "output_data" / "CONTENT.md").touch()  # edited after the run

    assert check_provenance(make(project)).status == PASS


# --------------------------------------------------------------------------- #
# git-backed check
# --------------------------------------------------------------------------- #
def git_repo(root):
    """Initialize and commit everything in ``root``; skip if git is absent."""
    try:
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t",
                      "commit", "-qm", "init"]):
            subprocess.run(["git"] + args, cwd=str(root), check=True,
                           capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"git unavailable: {error}")


def test_tracked_size_skips_outside_a_repo(project):
    assert check_tracked_size(make(project)).status == SKIP


def test_tracked_size_passes_on_small_repo(project):
    git_repo(project)
    assert check_tracked_size(make(project)).status == PASS


def test_tracked_size_fails_on_oversized_file(project):
    (project / "big.txt").write_text("x" * 4096)
    git_repo(project)
    config = dict(CONFIG, verify={"max_tracked_bytes": 1024})
    finding = check_tracked_size(make(project, config))
    assert finding.status == FAIL
    assert any("big.txt" in detail for detail in finding.details)


def test_tracked_size_fails_on_risky_extension(project):
    (project / "source_data" / "brain.nii.gz").write_text("x")
    (project / "source_data" / "CONTENT.md").write_text("- `brain.nii.gz`\n")
    git_repo(project)
    finding = check_tracked_size(make(project))
    assert finding.status == FAIL
    assert any("brain.nii.gz" in detail for detail in finding.details)


def test_tracked_size_allows_archives_under_dot_directories(project):
    skill = project / ".claude" / "skills"
    skill.mkdir(parents=True)
    (skill / "init.zip").write_text("x")
    git_repo(project)
    assert check_tracked_size(make(project)).status == PASS


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def test_run_checks_reports_every_check(project):
    findings = run_checks(CONFIG, root=project)
    assert {f.check for f in findings} == {
        "content_md", "task_list", "dependencies", "doc_paths", "config_keys",
        "tracked_size", "provenance", "lint"}


def test_run_checks_honours_skip(project):
    findings = {f.check: f for f in run_checks(CONFIG, root=project, skip=["lint"])}
    assert findings["lint"].status == SKIP
    assert "configuration" in findings["lint"].message


def test_run_checks_survives_a_broken_check(project, monkeypatch):
    """One exploding check must not hide the others."""
    def explode(_):
        raise RuntimeError("boom")

    monkeypatch.setattr("airoh.verify.CHECKS", (explode, check_task_list))
    explode.__name__ = "check_explode"
    findings = run_checks(CONFIG, root=project)
    assert findings[0].status == SKIP and "boom" in findings[0].message
    assert findings[1].status == PASS
