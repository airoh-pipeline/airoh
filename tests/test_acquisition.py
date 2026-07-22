from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from invoke import Context
from invoke.config import Config

from airoh.acquisition import download_data, fetch_data


def make_context(files_config):
    config = Config(overrides={"files": files_config})
    return Context(config=config)


def mock_response(chunks):
    """Build a fake urlopen context manager that yields chunks then b''."""
    response = MagicMock()
    response.read.side_effect = list(chunks) + [b""]
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


def test_download_data_missing_name():
    c = make_context({})
    with pytest.raises(ValueError, match="No file config found for 'missing'"):
        download_data(c, "missing")


def test_download_data_missing_url(tmp_path):
    c = make_context({"data": {"output_file": str(tmp_path / "out.bin")}})
    with pytest.raises(ValueError, match="must define both 'url' and 'output_file'"):
        download_data(c, "data")


def test_download_data_missing_output_file():
    c = make_context({"data": {"url": "http://example.com/file"}})
    with pytest.raises(ValueError, match="must define both 'url' and 'output_file'"):
        download_data(c, "data")


def test_download_data_skips_existing(tmp_path):
    out = tmp_path / "out.bin"
    out.write_bytes(b"existing content")
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    with patch("airoh.acquisition.urlopen") as mock_open:
        download_data(c, "data")
        mock_open.assert_not_called()


def test_download_data_happy_path(tmp_path):
    out = tmp_path / "sub" / "out.bin"
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    with patch("airoh.acquisition.urlopen", return_value=mock_response([b"hello ", b"world"])):
        download_data(c, "data")
    assert out.exists()
    assert out.read_bytes() == b"hello world"
    assert not out.with_suffix(out.suffix + ".part").exists()


def test_download_data_zero_bytes(tmp_path):
    out = tmp_path / "out.bin"
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    with patch("airoh.acquisition.urlopen", return_value=mock_response([])):
        with pytest.raises(RuntimeError, match="Downloaded 0 bytes"):
            download_data(c, "data")
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".part").exists()


def test_download_data_network_error_cleans_up(tmp_path):
    out = tmp_path / "out.bin"
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    failing_response = MagicMock()
    failing_response.__enter__ = lambda s: s
    failing_response.__exit__ = MagicMock(return_value=False)
    failing_response.read.side_effect = OSError("connection reset")
    with patch("airoh.acquisition.urlopen", return_value=failing_response):
        with pytest.raises(RuntimeError, match="Failed to download"):
            download_data(c, "data")
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".part").exists()


# --- fetch_data: symlink / copy / download-fallback -------------------------


def test_fetch_data_symlinks_from_source_arg(tmp_path):
    src = tmp_path / "existing.tsv"
    src.write_text("real data")
    out = tmp_path / "dest" / "linked.tsv"
    c = make_context({"data": {"output_file": str(out)}})

    fetch_data(c, "data", source=str(src))

    assert out.is_symlink()
    assert out.resolve() == src.resolve()
    assert out.read_text() == "real data"


def test_fetch_data_symlinks_from_config_source(tmp_path):
    src = tmp_path / "existing.tsv"
    src.write_text("real data")
    out = tmp_path / "linked.tsv"
    c = make_context({"data": {"output_file": str(out), "source": str(src)}})

    fetch_data(c, "data")

    assert out.is_symlink()
    assert out.resolve() == src.resolve()


def test_fetch_data_copy_makes_real_file(tmp_path):
    src = tmp_path / "existing.tsv"
    src.write_text("real data")
    out = tmp_path / "copied.tsv"
    c = make_context({"data": {"output_file": str(out)}})

    fetch_data(c, "data", source=str(src), copy=True)

    assert out.exists()
    assert not out.is_symlink()
    assert out.read_text() == "real data"


def test_fetch_data_copies_directory_recursively(tmp_path):
    src = tmp_path / "dataset"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("a")
    out = tmp_path / "dest_dataset"
    c = make_context({"data": {"output_file": str(out)}})

    fetch_data(c, "data", source=str(src), copy=True)

    assert (out / "sub" / "a.txt").read_text() == "a"
    assert not out.is_symlink()


def test_fetch_data_falls_back_to_download(tmp_path):
    out = tmp_path / "out.bin"
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    with patch("airoh.acquisition.urlopen", return_value=mock_response([b"downloaded"])):
        fetch_data(c, "data")
    assert out.exists()
    assert not out.is_symlink()
    assert out.read_bytes() == b"downloaded"


def test_fetch_data_is_idempotent(tmp_path, capsys):
    src = tmp_path / "existing.tsv"
    src.write_text("real data")
    out = tmp_path / "linked.tsv"
    c = make_context({"data": {"output_file": str(out)}})

    fetch_data(c, "data", source=str(src))
    capsys.readouterr()
    fetch_data(c, "data", source=str(src))

    assert "already links to" in capsys.readouterr().out
    assert out.resolve() == src.resolve()


def test_fetch_data_repoints_wrong_link(tmp_path):
    old_src = tmp_path / "old.tsv"
    old_src.write_text("old")
    new_src = tmp_path / "new.tsv"
    new_src.write_text("new")
    out = tmp_path / "linked.tsv"
    c = make_context({"data": {"output_file": str(out)}})

    fetch_data(c, "data", source=str(old_src))
    fetch_data(c, "data", source=str(new_src))

    assert out.resolve() == new_src.resolve()
    assert out.read_text() == "new"


def test_fetch_data_refuses_to_clobber_real_file(tmp_path):
    src = tmp_path / "existing.tsv"
    src.write_text("real data")
    out = tmp_path / "already_here.tsv"
    out.write_text("do not overwrite")
    c = make_context({"data": {"output_file": str(out)}})

    with pytest.raises(ValueError, match="already exists and is not a symlink"):
        fetch_data(c, "data", source=str(src))
    assert out.read_text() == "do not overwrite"


def test_fetch_data_missing_source_path(tmp_path):
    out = tmp_path / "linked.tsv"
    c = make_context({"data": {"output_file": str(out)}})
    with pytest.raises(ValueError, match="does not exist"):
        fetch_data(c, "data", source=str(tmp_path / "nope.tsv"))


def test_fetch_data_missing_output_file():
    c = make_context({"data": {"url": "http://example.com/file"}})
    with pytest.raises(ValueError, match="must define 'output_file'"):
        fetch_data(c, "data", source="/whatever")


def test_fetch_data_missing_name():
    c = make_context({})
    with pytest.raises(ValueError, match="No file config found for 'missing'"):
        fetch_data(c, "missing")


# --- backward-compat: names moved to airoh.acquisition still import from utils --


def test_moved_tasks_remain_importable_from_utils():
    import airoh.acquisition as acquisition
    import airoh.utils as utils

    # Same task objects, re-exported for backward compatibility.
    assert utils.download_data is acquisition.download_data
    assert utils.ensure_submodule is acquisition.ensure_submodule
