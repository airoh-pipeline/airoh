import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from invoke import Context
from invoke.config import Config

from airoh.utils import download_data


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
    with patch("airoh.utils.urlopen") as mock_open:
        download_data(c, "data")
        mock_open.assert_not_called()


def test_download_data_happy_path(tmp_path):
    out = tmp_path / "sub" / "out.bin"
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    with patch("airoh.utils.urlopen", return_value=mock_response([b"hello ", b"world"])):
        download_data(c, "data")
    assert out.exists()
    assert out.read_bytes() == b"hello world"
    assert not out.with_suffix(out.suffix + ".part").exists()


def test_download_data_zero_bytes(tmp_path):
    out = tmp_path / "out.bin"
    c = make_context({"data": {"url": "http://example.com/file", "output_file": str(out)}})
    with patch("airoh.utils.urlopen", return_value=mock_response([])):
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
    with patch("airoh.utils.urlopen", return_value=failing_response):
        with pytest.raises(RuntimeError, match="Failed to download"):
            download_data(c, "data")
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".part").exists()
