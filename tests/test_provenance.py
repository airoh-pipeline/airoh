"""Unit tests for airoh.provenance — the records, and their tolerance of missing data."""

import json
import subprocess

import pytest
from invoke import Context
from invoke.config import Config

from airoh.provenance import (
    _parse_pyproject_dependencies,
    describe_path,
    git_info,
    parse_requirements,
    record_run,
    record_sources,
    requirement_name,
    sha256_file,
)

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture
def context(tmp_path, monkeypatch):
    """An invoke Context rooted in an empty project directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source_data").mkdir()
    (tmp_path / "output_data").mkdir()
    return Context(config=Config(overrides={
        "source_data_dir": "source_data",
        "output_data_dir": "output_data",
    }))


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


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def test_sha256_file_hashes_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("")
    assert sha256_file(path) == EMPTY_SHA


def test_sha256_file_skips_oversized(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x" * 100)
    assert sha256_file(path, max_bytes=10) is None


def test_sha256_file_tolerates_missing(tmp_path):
    assert sha256_file(tmp_path / "nope.txt") is None


def test_requirement_name_strips_specifiers():
    assert requirement_name("numpy>=1.3") == "numpy"
    assert requirement_name("git-annex >= 10.2") == "git-annex"
    assert requirement_name('pandas[extra]==2.0; python_version<"3.12"') == "pandas"


def test_parse_requirements_ignores_comments_and_flags(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("# a comment\nnumpy>=1.0\n\n-r other.txt\npandas  # trailing\n")
    assert parse_requirements(path) == ["numpy", "pandas"]


def test_parse_pyproject_dependencies(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "x"\ndependencies = [\n  "numpy",\n  "pandas>=2",\n]\n')
    assert _parse_pyproject_dependencies(path) == ["numpy", "pandas"]


# --------------------------------------------------------------------------- #
# describe_path
# --------------------------------------------------------------------------- #
def test_describe_path_records_absence(tmp_path):
    record = describe_path(tmp_path / "nope")
    assert record["exists"] is False
    assert record["sha256"] is None


def test_describe_path_records_a_file(tmp_path):
    path = tmp_path / "data.tsv"
    path.write_text("")
    record = describe_path(path)
    assert record["exists"] is True
    assert record["size_bytes"] == 0
    assert record["sha256"] == EMPTY_SHA


def test_describe_path_resolves_a_symlink(tmp_path):
    """A symlinked asset must be attributed to what it really points at."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "x.txt").write_text("hello")
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    record = describe_path(link)
    assert record["is_symlink"] is True
    assert record["resolved_path"] == str(real)


def test_describe_path_records_git_commit(tmp_path):
    (tmp_path / "x.txt").write_text("hello")
    git_repo(tmp_path)
    record = describe_path(tmp_path / "x.txt")
    assert record["git"]["commit"]
    assert record["git"]["dirty"] is False


def test_describe_path_records_datalad_id(tmp_path):
    (tmp_path / ".datalad").mkdir()
    (tmp_path / ".datalad" / "config").write_text("[datalad \"dataset\"]\n\tid = abc-123\n")
    assert describe_path(tmp_path)["datalad_id"] == "abc-123"


def test_git_info_returns_none_outside_a_repository(tmp_path):
    assert git_info(tmp_path) is None


# --------------------------------------------------------------------------- #
# record_sources
# --------------------------------------------------------------------------- #
def test_record_sources_describes_each_asset(context, tmp_path):
    (tmp_path / "source_data" / "papers.tsv").write_text("a\tb\n")
    context.config["files"] = {
        "papers": {"url": "https://example.com/p.tsv",
                   "output_file": "source_data/papers.tsv"}}

    record_sources(context)
    manifest = json.loads((tmp_path / "source_data" / "MANIFEST.json").read_text())
    papers = manifest["assets"]["papers"]
    assert papers["exists"] is True
    assert papers["mode"] == "download"
    assert papers["sha256"]


def test_record_sources_records_an_unfetched_asset_as_absent(context, tmp_path):
    context.config["files"] = {
        "papers": {"url": "https://example.com/p.tsv",
                   "output_file": "source_data/papers.tsv"}}

    record_sources(context)
    manifest = json.loads((tmp_path / "source_data" / "MANIFEST.json").read_text())
    assert manifest["assets"]["papers"]["exists"] is False


def test_record_sources_handles_datasets_as_plain_paths(context, tmp_path):
    """airoh.datalad.get_data declares a dataset as a bare path string."""
    (tmp_path / "source_data" / "ds").mkdir()
    context.config["datasets"] = {"ds": "source_data/ds"}

    record_sources(context)
    manifest = json.loads((tmp_path / "source_data" / "MANIFEST.json").read_text())
    assert manifest["assets"]["ds"]["exists"] is True


def test_record_sources_drops_self_attribution(context, tmp_path):
    """An asset inside the project repo must not borrow the project's commit."""
    (tmp_path / "source_data" / "papers.tsv").write_text("a\n")
    git_repo(tmp_path)
    context.config["files"] = {"papers": {"output_file": "source_data/papers.tsv"}}

    record_sources(context)
    manifest = json.loads((tmp_path / "source_data" / "MANIFEST.json").read_text())
    assert manifest["assets"]["papers"]["git"] is None


def test_record_sources_keeps_external_repository_commit(context, tmp_path):
    """The point of the manifest: pin a symlinked external checkout."""
    external = tmp_path.parent / (tmp_path.name + "_external")
    external.mkdir()
    (external / "data.txt").write_text("hello")
    git_repo(external)
    (tmp_path / "source_data" / "linked").symlink_to(external, target_is_directory=True)
    git_repo(tmp_path)
    context.config["datasets"] = {"ext": {"output_dir": "source_data/linked"}}

    record_sources(context)
    manifest = json.loads((tmp_path / "source_data" / "MANIFEST.json").read_text())
    asset = manifest["assets"]["ext"]
    assert asset["mode"] == "symlink"
    assert asset["git"]["commit"]


# --------------------------------------------------------------------------- #
# record_run
# --------------------------------------------------------------------------- #
def test_record_run_checksums_outputs(context, tmp_path):
    (tmp_path / "output_data" / "result.csv").write_text("a,b\n")

    record_run(context, tasks="run-model")
    record = json.loads((tmp_path / "output_data" / "PROVENANCE.json").read_text())
    assert record["tasks"] == ["run-model"]
    assert record["outputs"]["result.csv"]["sha256"]
    assert record["environment"]["python"]


def test_record_run_excludes_itself_from_the_outputs(context, tmp_path):
    (tmp_path / "output_data" / "result.csv").write_text("a,b\n")

    record_run(context)
    record_run(context)  # the first record must not become an input to the second
    record = json.loads((tmp_path / "output_data" / "PROVENANCE.json").read_text())
    assert list(record["outputs"]) == ["result.csv"]


def test_record_run_links_to_the_manifest(context, tmp_path):
    (tmp_path / "source_data" / "papers.tsv").write_text("a\n")
    context.config["files"] = {"papers": {"output_file": "source_data/papers.tsv"}}
    record_sources(context)

    record_run(context)
    record = json.loads((tmp_path / "output_data" / "PROVENANCE.json").read_text())
    assert record["inputs"]["manifest_sha256"]
    assert record["inputs"]["assets"]["papers"]["sha256"]


def test_record_run_tolerates_a_missing_manifest(context, tmp_path):
    """Provenance is documentation; it can never be a precondition."""
    record_run(context)
    record = json.loads((tmp_path / "output_data" / "PROVENANCE.json").read_text())
    assert record["inputs"]["manifest_sha256"] is None


def test_record_run_records_the_project_commit(context, tmp_path):
    (tmp_path / "output_data" / "result.csv").write_text("a,b\n")
    git_repo(tmp_path)

    record_run(context)
    record = json.loads((tmp_path / "output_data" / "PROVENANCE.json").read_text())
    assert record["repository"]["commit"]
    # State is captured before the record is written, so a committed tree is clean.
    assert record["repository"]["dirty"] is False


def test_record_run_flags_an_uncommitted_tree(context, tmp_path):
    """Outputs produced from edited-but-uncommitted code must say so."""
    (tmp_path / "output_data" / "result.csv").write_text("a,b\n")
    git_repo(tmp_path)
    (tmp_path / "output_data" / "result.csv").write_text("a,b,c\n")

    record_run(context)
    record = json.loads((tmp_path / "output_data" / "PROVENANCE.json").read_text())
    assert record["repository"]["dirty"] is True
