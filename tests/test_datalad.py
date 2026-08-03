import subprocess
from unittest.mock import patch

import pytest
from invoke import Context
from invoke.config import Config

import airoh.datalad as datalad_module
from airoh.datalad import (
    _dataset_entry,
    _require_datalad,
    datalad_get,
    ensure_dataset,
    get_data,
    install_dataset,
    install_subdataset,
    load_known_failures,
    prefetch_pattern,
    save_known_failures,
    update_dataset,
    update_subdataset,
)


def make_context(datasets_config):
    config = Config(overrides={"datasets": datasets_config})
    return Context(config=config)


def completed(returncode, stderr=""):
    return subprocess.CompletedProcess(["datalad"], returncode=returncode, stdout="",
                                       stderr=stderr)


# ---- import / CLI guard ----------------------------------------------------

def test_import_succeeds_without_datalad_cli():
    # If this module imported at all (it did, at collection time), the guard
    # is call-time only — this just documents the intent.
    assert datalad_module is not None


def test_require_datalad_raises_with_hint(monkeypatch):
    monkeypatch.setattr(datalad_module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="pip install airoh\\[datalad\\]"):
        _require_datalad()


def test_require_datalad_passes_when_present(monkeypatch):
    monkeypatch.setattr(datalad_module.shutil, "which", lambda name: "/usr/bin/datalad")
    _require_datalad()  # no raise


# ---- schema normalization ---------------------------------------------------

def test_dataset_entry_unknown_name_raises():
    c = make_context({})
    with pytest.raises(ValueError, match="not found in invoke.yaml"):
        _dataset_entry(c, "missing")


def test_dataset_entry_accepts_plain_string():
    c = make_context({"cneuromod": "/data/cneuromod"})
    entry = _dataset_entry(c, "cneuromod")
    assert entry == {"output_dir": "/data/cneuromod", "url": None, "source": None}


def test_dataset_entry_accepts_mapping():
    c = make_context({
        "cneuromod": {"output_dir": "/data/cneuromod", "url": "https://example.com/ds",
                     "source": "/local/checkout"},
    })
    entry = _dataset_entry(c, "cneuromod")
    assert entry == {"output_dir": "/data/cneuromod", "url": "https://example.com/ds",
                     "source": "/local/checkout"}


def test_dataset_entry_mapping_requires_output_dir():
    c = make_context({"cneuromod": {"url": "https://example.com/ds"}})
    with pytest.raises(ValueError, match="must define 'output_dir'"):
        _dataset_entry(c, "cneuromod")


# ---- datalad_get -------------------------------------------------------------

@patch("airoh.datalad._require_datalad")
def test_datalad_get_noop_outside_dataset(mock_require, tmp_path):
    datalad_get("some/path", tmp_path)  # no .git/.datalad -> tolerant no-op
    mock_require.assert_called_once()


@patch("airoh.datalad._require_datalad")
def test_datalad_get_strict_outside_dataset_raises(mock_require, tmp_path):
    with pytest.raises(RuntimeError, match="not a Datalad dataset"):
        datalad_get("some/path", tmp_path, strict=True)


@patch("airoh.datalad._run")
@patch("airoh.datalad._require_datalad")
def test_datalad_get_arg_construction(mock_require, mock_run, tmp_path):
    (tmp_path / ".git").mkdir()
    mock_run.return_value = completed(0)
    datalad_get(["a", "b"], tmp_path, recursive=True, get_content=False)
    args = mock_run.call_args[0][1]
    assert args == ["get", "-n", "-r", "a", "b"]


@patch("airoh.datalad._run")
@patch("airoh.datalad._require_datalad")
def test_datalad_get_retries_over_https_on_failure(mock_require, mock_run, tmp_path):
    (tmp_path / ".git").mkdir()
    mock_run.side_effect = [completed(1, "auth failed"), completed(0)]
    datalad_get("a", tmp_path)
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[1][0][0] == datalad_module._HTTPS_OVERRIDE


@patch("airoh.datalad._run")
@patch("airoh.datalad._require_datalad")
def test_datalad_get_tolerant_warns_on_persistent_failure(mock_require, mock_run,
                                                           tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    mock_run.return_value = completed(1, "still failing")
    datalad_get("a", tmp_path)
    assert "returned errors" in capsys.readouterr().out


@patch("airoh.datalad._run")
@patch("airoh.datalad._require_datalad")
def test_datalad_get_strict_raises_on_persistent_failure(mock_require, mock_run, tmp_path):
    (tmp_path / ".git").mkdir()
    mock_run.return_value = completed(1, "still failing")
    with pytest.raises(RuntimeError, match="datalad get failed"):
        datalad_get("a", tmp_path, strict=True)


def test_run_returns_failure_on_timeout(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="datalad", timeout=1)
    monkeypatch.setattr(datalad_module.subprocess, "run", fake_run)
    result = datalad_module._run(None, ["get", "x"], tmp_path)
    assert result.returncode == 1
    assert "timed out" in result.stderr


# ---- install_subdataset / update_subdataset ---------------------------------

@patch("airoh.datalad.datalad_get")
@patch("airoh.datalad._require_datalad")
def test_install_subdataset_fresh(mock_require, mock_get, tmp_path):
    install_subdataset("sub", tmp_path)
    mock_get.assert_called_once_with("sub", tmp_path, get_content=False, strict=False)


@patch("airoh.datalad.update_subdataset")
@patch("airoh.datalad._require_datalad")
def test_install_subdataset_already_installed_updates_instead(mock_require, mock_update,
                                                               tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".git").mkdir()
    install_subdataset("sub", tmp_path)
    mock_update.assert_called_once_with("sub", tmp_path, strict=False)


@patch("airoh.datalad.datalad_get")
@patch("airoh.datalad._require_datalad")
def test_install_subdataset_strict_raises_if_still_missing(mock_require, mock_get, tmp_path):
    with pytest.raises(RuntimeError, match="was not installed"):
        install_subdataset("sub", tmp_path, strict=True)


@patch("airoh.datalad._require_datalad")
def test_update_subdataset_noop_when_not_installed(mock_require, tmp_path):
    update_subdataset("sub", tmp_path)  # no .git -> no-op, no error


# ---- ensure_dataset ----------------------------------------------------------

def test_ensure_dataset_noop_when_dest_exists(tmp_path, capsys):
    dest = tmp_path / "existing"
    dest.mkdir()
    ensure_dataset(dest)
    assert "already exists" in capsys.readouterr().out


def test_ensure_dataset_symlinks_from_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    dest = tmp_path / "dest"
    ensure_dataset(dest, source=source)
    assert dest.is_symlink()
    assert dest.resolve() == source.resolve()


def test_ensure_dataset_missing_source_raises(tmp_path):
    dest = tmp_path / "dest"
    with pytest.raises(ValueError, match="does not exist"):
        ensure_dataset(dest, source=tmp_path / "nowhere")


@patch("airoh.datalad.subprocess.run")
@patch("airoh.datalad._require_datalad")
def test_ensure_dataset_clones_from_url(mock_require, mock_run, tmp_path):
    dest = tmp_path / "dest"
    ensure_dataset(dest, url="https://example.com/ds.git")
    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["datalad", "clone", "https://example.com/ds.git",
                                        str(dest)]


def test_ensure_dataset_no_source_or_url_raises(tmp_path):
    dest = tmp_path / "dest"
    with pytest.raises(ValueError, match="neither 'source' nor 'url'"):
        ensure_dataset(dest)


# ---- failure cache -----------------------------------------------------------

def test_failure_cache_round_trip(tmp_path):
    assert load_known_failures(tmp_path) == set()
    save_known_failures(tmp_path, {"a/b.nii.gz", "c/d.json"})
    assert load_known_failures(tmp_path) == {"a/b.nii.gz", "c/d.json"}


# ---- prefetch_pattern --------------------------------------------------------

@patch("airoh.datalad.datalad_get")
def test_prefetch_pattern_counts_already_present_files(mock_get, tmp_path):
    sub = tmp_path / "marker"
    sub.mkdir()
    (sub / "present.json").write_text("{}")
    present, fetched, skipped, new_failures, resolved = prefetch_pattern(
        tmp_path, "*.json", subdir="marker")
    assert (present, fetched, skipped, new_failures, resolved) == (1, 0, 0, set(), set())
    mock_get.assert_not_called()


def test_prefetch_pattern_missing_dir_returns_zeros(tmp_path):
    result = prefetch_pattern(tmp_path, "*.json", subdir="absent")
    assert result == (0, 0, 0, set(), set())


@patch("airoh.datalad.datalad_get")
def test_prefetch_pattern_fetches_missing_and_skips_known_failures(mock_get, tmp_path):
    marker = tmp_path / "marker"
    marker.mkdir()
    # Create broken symlinks so rglob finds them as "missing" (matched but not is_file()).
    missing_target = marker / "missing.json"
    skipped_target = marker / "skipped.json"
    missing_target.symlink_to(tmp_path / "nonexistent-1")
    skipped_target.symlink_to(tmp_path / "nonexistent-2")

    def fake_get(paths, root):
        # datalad_get is asked only for the non-skipped missing file.
        assert [str(p) for p in paths] == ["marker/missing.json"]
        missing_target.unlink()
        missing_target.write_text("{}")
    mock_get.side_effect = fake_get

    present, fetched, skipped, new_failures, resolved = prefetch_pattern(
        tmp_path, "*.json", subdir="marker", skip_set={"marker/skipped.json"})
    assert present == 0
    assert fetched == 1
    assert skipped == 1
    assert new_failures == set()
    assert resolved == {"marker/missing.json"}


# ---- invoke tasks (thin wrappers) -------------------------------------------

@patch("airoh.datalad.ensure_dataset")
def test_install_dataset_task_routes_to_ensure_dataset(mock_ensure):
    c = make_context({"cneuromod": {"output_dir": "/d", "url": "https://x/ds",
                                    "source": None}})
    install_dataset(c, "cneuromod")
    mock_ensure.assert_called_once_with("/d", url="https://x/ds", source=None)


@patch("airoh.datalad.datalad_get")
def test_get_data_task_routes_to_datalad_get(mock_get):
    c = make_context({"cneuromod": {"output_dir": "/d"}})
    get_data(c, "cneuromod", path="sub-01", strict=True)
    mock_get.assert_called_once_with("sub-01", "/d", recursive=False, strict=True)


@patch("airoh.datalad.update_subdataset")
def test_update_dataset_task_routes_to_update_subdataset(mock_update):
    c = make_context({"cneuromod": {"output_dir": "/data/cneuromod"}})
    update_dataset(c, "cneuromod")
    mock_update.assert_called_once_with("cneuromod", "/data", strict=False)
