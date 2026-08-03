"""Unit tests for airoh.figures — the montage SVG parser and its tolerance."""

import json

import pytest
from invoke import Context
from invoke.config import Config

import airoh.figures as figures_module
from airoh.figures import (
    MM_PER_INCH,
    _length_in_mm,
    _panel_sizes_path,
    _transform_scale,
    clean_figure,
    compose_figure,
    figure_layout,
    panel_size,
    read_panel_sizes,
    write_panel_sizes,
)

SVG_HEADER = (
    'xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)


def write_svg(path, body, width="100mm", height="50mm", view_box="0 0 200 100"):
    path.write_text(
        f'<svg {SVG_HEADER} width="{width}" height="{height}" '
        f'viewBox="{view_box}">{body}</svg>'
    )
    return path


@pytest.fixture(autouse=True)
def _clear_panel_size_cache(monkeypatch):
    """`panel_size` caches at module scope — isolate tests from each other."""
    monkeypatch.setattr(figures_module, "_PANEL_SIZES_CACHE", None)


# --------------------------------------------------------------------------- #
# Unit parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value, expected", [
    ("10mm", 10.0),
    ("1cm", 10.0),
    ("1in", 25.4),
    ("72pt", 25.4),
    ("96px", 25.4),
    ("10", 10.0),
])
def test_length_in_mm_units(value, expected):
    assert _length_in_mm(value) == pytest.approx(expected)


def test_length_in_mm_invalid():
    assert _length_in_mm("not-a-length") is None
    assert _length_in_mm(None) is None


def test_transform_scale_scale_one_arg():
    assert _transform_scale("scale(2)") == (2.0, 2.0)


def test_transform_scale_scale_two_args():
    assert _transform_scale("scale(2,3)") == (2.0, 3.0)


def test_transform_scale_matrix():
    assert _transform_scale("matrix(2,0,0,3,0,0)") == (2.0, 3.0)


def test_transform_scale_translate_is_identity():
    assert _transform_scale("translate(10,10)") == (1.0, 1.0)


def test_transform_scale_empty():
    assert _transform_scale(None) == (1.0, 1.0)


# --------------------------------------------------------------------------- #
# read_panel_sizes
# --------------------------------------------------------------------------- #
def test_read_panel_sizes_viewbox_scaling(tmp_path):
    # width=100mm over a 200-user-unit viewBox -> 0.5 mm per user unit.
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    sizes = read_panel_sizes(svg)
    assert sizes == {"panel_a.png": pytest.approx((10.0, 5.0))}


def test_read_panel_sizes_nested_transform_folding(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<g transform="translate(5,5)"><g transform="scale(2)">'
        '<image xlink:href="figures/panel_a.png" width="10" height="5"/>'
        "</g></g>",
    )
    sizes = read_panel_sizes(svg)
    # base 10x5 user units, doubled by the nested scale -> 20x10, * 0.5 mm/unit.
    assert sizes == {"panel_a.png": pytest.approx((10.0, 5.0))}


def test_read_panel_sizes_href_without_xlink_prefix(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image href="figures/panel_b.png" width="20" height="10"/>',
    )
    sizes = read_panel_sizes(svg)
    assert "panel_b.png" in sizes


def test_read_panel_sizes_skips_missing_dimensions(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="figures/no_size.png"/>'
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    sizes = read_panel_sizes(svg)
    assert list(sizes) == ["panel_a.png"]


def test_read_panel_sizes_strip_prefix_custom(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="assets/panel_a.png" width="20" height="10"/>',
    )
    sizes = read_panel_sizes(svg, strip_prefix="assets/")
    assert list(sizes) == ["panel_a.png"]


def test_read_panel_sizes_no_prefix_match_keeps_full_href(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="other/panel_a.png" width="20" height="10"/>',
    )
    sizes = read_panel_sizes(svg)
    assert list(sizes) == ["other/panel_a.png"]


def test_read_panel_sizes_missing_file(tmp_path):
    assert read_panel_sizes(tmp_path / "missing.svg") == {}


def test_read_panel_sizes_malformed_xml(tmp_path, capsys):
    path = tmp_path / "bad.svg"
    path.write_text("<svg><unclosed>")
    assert read_panel_sizes(path) == {}
    assert "could not parse" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# write_panel_sizes
# --------------------------------------------------------------------------- #
def test_write_panel_sizes_single_path(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    out = tmp_path / "panel_sizes.json"
    sizes = write_panel_sizes(svg, out)
    assert out.is_file()
    assert json.loads(out.read_text()) == {
        "panel_a.png": list(sizes["panel_a.png"])
    }


def test_write_panel_sizes_merges_multiple_svgs(tmp_path):
    svg1 = write_svg(
        tmp_path / "montage1.svg",
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    svg2 = write_svg(
        tmp_path / "montage2.svg",
        '<image xlink:href="figures/panel_b.png" width="40" height="20"/>',
    )
    out = tmp_path / "panel_sizes.json"
    sizes = write_panel_sizes([svg1, svg2], out)
    assert set(sizes) == {"panel_a.png", "panel_b.png"}


def test_write_panel_sizes_empty_warns(tmp_path, capsys):
    out = tmp_path / "panel_sizes.json"
    sizes = write_panel_sizes(tmp_path / "missing.svg", out)
    assert sizes == {}
    assert "no panel boxes found" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# panel_size
# --------------------------------------------------------------------------- #
def test_panel_size_converts_mm_to_inches(tmp_path, monkeypatch):
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    (figures_dir / "panel_sizes.json").write_text(
        json.dumps({"panel_a.png": [25.4, 50.8]})
    )
    monkeypatch.setenv("FIGURES_DIR", str(figures_dir))
    width_in, height_in = panel_size("panel_a.png", default=(1, 1))
    assert (width_in, height_in) == pytest.approx((1.0, 2.0))


def test_panel_size_unknown_name_falls_back(tmp_path, monkeypatch):
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    (figures_dir / "panel_sizes.json").write_text(json.dumps({}))
    monkeypatch.setenv("FIGURES_DIR", str(figures_dir))
    assert panel_size("missing.png", default=(3, 4)) == (3, 4)


def test_panel_size_missing_json_falls_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIGURES_DIR", str(tmp_path / "nowhere"))
    assert panel_size("panel_a.png", default=(3, 4)) == (3, 4)
    assert "no panel sizes at" in capsys.readouterr().out


def test_panel_size_falls_back_to_output_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FIGURES_DIR", raising=False)
    monkeypatch.setenv("OUTPUT_DATA_DIR", str(tmp_path))
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    (figures_dir / "panel_sizes.json").write_text(
        json.dumps({"panel_a.png": [MM_PER_INCH, MM_PER_INCH]})
    )
    assert panel_size("panel_a.png", default=(0, 0)) == pytest.approx((1.0, 1.0))
    assert _panel_sizes_path() == figures_dir / "panel_sizes.json"


# --------------------------------------------------------------------------- #
# Tasks: figure_layout, compose_figure, clean_figure
# --------------------------------------------------------------------------- #
def make_context(tmp_path, figures_config, figures_dir="figures"):
    return Context(config=Config(overrides={
        "figures_dir": str(tmp_path / figures_dir),
        "figures": figures_config,
    }))


def test_figure_layout_writes_merged_panel_sizes(tmp_path):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    c = make_context(tmp_path, {
        "qa_figure": {"svg": str(svg), "output": str(tmp_path / "out.png")},
    })
    figure_layout(c)
    out = tmp_path / "figures" / "panel_sizes.json"
    assert out.is_file()
    assert "panel_a.png" in json.loads(out.read_text())


def test_figure_layout_unknown_name_warns(tmp_path, capsys):
    c = make_context(tmp_path, {})
    figure_layout(c, name="missing")
    assert "No figures entry named" in capsys.readouterr().out


def test_compose_figure_no_inkscape_warns_and_returns(tmp_path, monkeypatch, capsys):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    output = tmp_path / "out.png"
    c = make_context(tmp_path, {
        "qa_figure": {"svg": str(svg), "output": str(output)},
    })
    monkeypatch.setattr(figures_module.shutil, "which", lambda name: None)
    compose_figure(c)
    assert not output.exists()
    assert "Inkscape not found" in capsys.readouterr().out


def test_compose_figure_missing_svg_warns_and_returns(tmp_path, capsys):
    output = tmp_path / "out.png"
    c = make_context(tmp_path, {
        "qa_figure": {"svg": str(tmp_path / "missing.svg"), "output": str(output)},
    })
    compose_figure(c)
    assert not output.exists()
    assert "nothing to export" in capsys.readouterr().out


def test_compose_figure_mtime_skip(tmp_path, monkeypatch):
    svg = write_svg(
        tmp_path / "montage.svg",
        '<image xlink:href="figures/panel_a.png" width="20" height="10"/>',
    )
    output = tmp_path / "out.png"
    c = make_context(tmp_path, {
        "qa_figure": {"svg": str(svg), "output": str(output)},
    })
    monkeypatch.setattr(figures_module.shutil, "which", lambda name: "/usr/bin/inkscape")

    calls = []

    class _StubResult:
        returncode = 0
        stderr = ""

    def _stub_run(args, **kwargs):
        calls.append(args)
        output.write_bytes(b"fake-png")
        return _StubResult()

    monkeypatch.setattr(figures_module.subprocess, "run", _stub_run)

    compose_figure(c)
    assert len(calls) == 1
    assert output.is_file()

    compose_figure(c)
    assert len(calls) == 1, "second call should skip: output is newer than sources"


def test_clean_figure_removes_output_and_panel_sizes(tmp_path):
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    panel_sizes = figures_dir / "panel_sizes.json"
    panel_sizes.write_text("{}")
    output = tmp_path / "out.png"
    output.write_bytes(b"fake-png")
    svg = tmp_path / "montage.svg"
    svg.write_text("<svg/>")

    c = make_context(tmp_path, {
        "qa_figure": {"svg": str(svg), "output": str(output)},
    })
    clean_figure(c)
    assert not output.exists()
    assert not panel_sizes.exists()
    assert svg.is_file(), "the hand-authored SVG must never be removed"
