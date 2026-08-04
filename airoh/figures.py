# src/airoh/figures.py
"""🖼️ Panel geometry read out of a hand-authored Inkscape montage.

A multi-panel figure is often laid out by hand in Inkscape while the panels
themselves are rendered by matplotlib — and the two disagree about size.
Placing a panel scales it, which stretches its text, so the point sizes an
author chose stop being the point sizes that land on the page.

The contract that fixes this: **the hand-authored montage is the single
source of truth for layout.** ``read_panel_sizes``/``write_panel_sizes`` parse
the montage SVG and record the box each linked panel is placed in; notebooks
then call ``panel_size`` to render every panel at exactly that physical size,
so placement is 1:1 and text is never stretched. Resize a box in Inkscape, and
the next ``invoke run`` regenerates that panel at the new size.

Configured under a ``figures:`` mapping in invoke.yaml, alongside ``files:``
and ``datasets:``::

    figures_dir: output_data/figures     # panel_sizes.json lives here

    figures:
      qa_figure:
        svg: output_data/qa_figure.svg   # hand-authored, a pipeline SOURCE
        output: output_data/qa_figure.png
        dpi: 300                          # optional, default 300

The SVG lives under ``output_data/`` (despite being a source, not a computed
result) because it links its panels by relative path and those links resolve
from its own directory — moving it elsewhere would break every panel link.

Tolerant by design — a missing or unparsable SVG yields an empty mapping, and
a missing Inkscape binary warns and returns, rather than breaking ``invoke
run`` for someone who has not authored a montage yet or does not have
Inkscape installed.
"""

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from invoke import task

MM_PER_INCH = 25.4

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# SVG user units per millimetre, for the length units Inkscape may write.
_MM_PER_UNIT = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "px": 25.4 / 96}

_EXPORT_TYPES = {".png": "png", ".pdf": "pdf", ".svg": "svg", ".eps": "eps"}

_PANEL_SIZES_CACHE = None


def _length_in_mm(value):
    """Parse an SVG length such as ``208.26895mm`` into millimetres."""
    match = re.fullmatch(r"\s*([0-9.eE+-]+)\s*([a-z%]*)\s*", value or "")
    if not match:
        return None
    number, unit = match.groups()
    # A unitless length is in user units, which the viewBox then maps to mm.
    return float(number) * _MM_PER_UNIT.get(unit, 1.0)


def _mm_per_user_unit(root):
    """Millimetres per user unit, from the root width against its viewBox."""
    width_mm = _length_in_mm(root.get("width"))
    view_box = (root.get("viewBox") or "").replace(",", " ").split()
    if width_mm is None or len(view_box) != 4:
        return 1.0
    view_box_width = float(view_box[2])
    return width_mm / view_box_width if view_box_width else 1.0


def _transform_scale(transform):
    """Scale factors ``(sx, sy)`` of a transform attribute; translations are 1."""
    scale_x = scale_y = 1.0
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", transform or ""):
        values = [float(v) for v in re.split(r"[\s,]+", args.strip()) if v]
        if name == "scale" and values:
            scale_x *= values[0]
            scale_y *= values[1] if len(values) > 1 else values[0]
        elif name == "matrix" and len(values) == 6:
            scale_x *= values[0]
            scale_y *= values[3]
    return scale_x, scale_y


def read_panel_sizes(svg_path, strip_prefix="figures/"):
    """Map each linked panel to the ``(width_mm, height_mm)`` box it is placed in.

    Keys are the panel's ``xlink:href`` with a leading ``strip_prefix``
    removed, e.g. ``qc_measures/fd_mean_by_dataset.png`` — matching what a
    notebook writes under its own ``figures/{notebook_stem}/`` directory.

    Tolerant: a missing SVG returns ``{}``; an unparsable one warns and
    returns ``{}``.
    """
    svg_path = Path(svg_path)
    if not svg_path.is_file():
        return {}
    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError as error:
        print(f"⚠️  could not parse {svg_path}: {error}")
        return {}

    mm_per_unit = _mm_per_user_unit(root)
    parents = {child: parent for parent in root.iter() for child in parent}

    sizes = {}
    for image in root.iter(SVG_NS + "image"):
        href = image.get(XLINK_HREF) or image.get("href")
        width = _length_in_mm(image.get("width"))
        height = _length_in_mm(image.get("height"))
        if not href or width is None or height is None:
            continue
        # Ancestor layers currently carry translations only, which do not
        # affect size, but a re-save could wrap them in a scaled group
        # (Inkscape's px->mm matrix); fold any such scale in rather than
        # silently ignore it.
        node = parents.get(image)
        while node is not None:
            scale_x, scale_y = _transform_scale(node.get("transform"))
            width *= scale_x
            height *= scale_y
            node = parents.get(node)
        key = href[len(strip_prefix):] if href.startswith(strip_prefix) else href
        sizes[key] = (width * mm_per_unit, height * mm_per_unit)
    return sizes


def write_panel_sizes(svg_paths, out_path, strip_prefix="figures/"):
    """Write panel sizes from one or several montage SVGs to JSON, merged.

    ``svg_paths`` accepts a single path or an iterable of paths, so a project
    composing several montages (``fig1``/``fig2``/``fig3``) gets one merged
    ``panel_sizes.json``. Returns the merged dict.
    """
    if isinstance(svg_paths, (str, Path)):
        svg_paths = [svg_paths]

    sizes = {}
    for svg_path in svg_paths:
        sizes.update(read_panel_sizes(svg_path, strip_prefix=strip_prefix))

    if not sizes:
        print(
            f"⚠️  no panel boxes found in {list(svg_paths)} — notebooks will "
            "fall back to their default figure sizes"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sizes, indent=2, sort_keys=True) + "\n")
    print(f"🖼️  wrote {len(sizes)} panel sizes to {out_path}")
    return sizes


def _panel_sizes_path():
    """``panel_sizes.json`` location: ``$FIGURES_DIR``, else ``$OUTPUT_DATA_DIR/figures``."""
    figures_dir = os.environ.get("FIGURES_DIR")
    if figures_dir:
        return Path(figures_dir) / "panel_sizes.json"
    output_dir = Path(os.environ.get("OUTPUT_DATA_DIR", "../output_data"))
    return output_dir / "figures" / "panel_sizes.json"


def _load_panel_sizes():
    global _PANEL_SIZES_CACHE
    if _PANEL_SIZES_CACHE is None:
        path = _panel_sizes_path()
        try:
            _PANEL_SIZES_CACHE = json.loads(path.read_text())
        except (OSError, ValueError):
            print(f"⚠️  no panel sizes at {path} — run `invoke figure-layout` "
                  "to size panels from the montage; using default figure sizes.")
            _PANEL_SIZES_CACHE = {}
    return _PANEL_SIZES_CACHE


def panel_size(name, default):
    """Figure size in inches for the panel placed as ``name`` in the montage.

    ``name`` is the panel's path relative to the figures directory, e.g.
    ``qc_measures/fd_mean_by_dataset.png``. Panels the montage does not place
    keep ``default``, so every notebook still runs standalone before any
    montage exists. Cached at module scope for the life of the process.
    """
    size_mm = _load_panel_sizes().get(name)
    if size_mm is None:
        return default
    return (size_mm[0] / MM_PER_INCH, size_mm[1] / MM_PER_INCH)


def _figures_config(c):
    """Yield ``(name, svg, output, dpi)`` for every entry in ``figures:``."""
    for name, entry in (c.config.get("figures") or {}).items():
        yield (name, Path(entry["svg"]), Path(entry["output"]),
               entry.get("dpi", 300))


@task(help={"name": "Only this montage's entry in `figures:` (default: all)."})
def figure_layout(c, name=None):
    """🖼️ Write every montage's panel geometry to figures_dir/panel_sizes.json.

    Read by notebooks (via ``panel_size``) so every placed panel renders at
    exactly the physical size its montage allocates it. Always re-runs, never
    skipped — it is cheap, and a box resized in Inkscape must take effect on
    the very next ``invoke run``. This is a deliberate exception to the
    existence-based caching every other step uses.
    """
    entries = list(_figures_config(c))
    if name:
        entries = [entry for entry in entries if entry[0] == name]
        if not entries:
            print(f"⚠️  No figures entry named '{name}'")
            return

    out_path = Path(c.config.get("figures_dir")) / "panel_sizes.json"
    svg_paths = [svg for _, svg, _, _ in entries]
    write_panel_sizes(svg_paths, out_path)


@task(help={"name": "Only this montage's entry in `figures:` (default: all)."})
def compose_figure(c, name=None):
    """🖼️ Render each hand-authored montage with Inkscape.

    Inkscape is an optional external dependency needed only to recompose the
    final figure, never to reproduce a panel, so a missing binary (or a
    failed export) warns and returns rather than failing the run. Skipped per
    montage when its output is already newer than its SVG and every panel it
    links.
    """
    entries = list(_figures_config(c))
    if name:
        entries = [entry for entry in entries if entry[0] == name]
        if not entries:
            print(f"⚠️  No figures entry named '{name}'")
            return

    figures_dir = Path(c.config.get("figures_dir"))
    for figure_name, svg, output, dpi in entries:
        _compose_one(svg, output, dpi, figures_dir, figure_name)


def _compose_one(svg, output, dpi, figures_dir, figure_name):
    if not svg.is_file():
        print(f"⚠️  No montage at {svg} for '{figure_name}' — nothing to export")
        return
    if shutil.which("inkscape") is None:
        print("⚠️  Inkscape not found on PATH — skipping the figure export. "
              f"Install it (https://inkscape.org/release/), or open {svg} and "
              "export it from the GUI.")
        return

    export_type = _EXPORT_TYPES.get(output.suffix.lower())
    if export_type is None:
        print(f"⚠️  Unsupported export extension '{output.suffix}' for "
              f"'{figure_name}' — expected one of {sorted(_EXPORT_TYPES)}")
        return

    # The montage links its panels by relative path, so the sources to
    # compare against are the SVG itself plus every panel it places.
    sources = [svg] + [figures_dir / name for name in read_panel_sizes(svg)]
    newest_source = max((p.stat().st_mtime for p in sources if p.is_file()), default=0)
    if output.is_file() and output.stat().st_mtime >= newest_source:
        print(f"⏭️  {output} is up to date")
        return

    result = subprocess.run(
        ["inkscape", f"--export-type={export_type}",
         f"--export-filename={output}", f"--export-dpi={dpi}", str(svg)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"⚠️  Inkscape export failed for '{figure_name}' "
              f"({result.stderr.strip()}) — open {svg} and export it from the "
              "GUI instead.")
        return
    print(f"🖼️  Exported {output} at {dpi} dpi")


@task(help={"name": "Only this montage's entry in `figures:` (default: all)."})
def clean_figure(c, name=None):
    """🧹 Remove composed montage outputs and panel_sizes.json.

    Never the SVG: that one is hand-authored in Inkscape and is a pipeline
    *source*, despite living under output_data/ (its relative image links
    resolve from there).
    """
    entries = list(_figures_config(c))
    if name:
        entries = [entry for entry in entries if entry[0] == name]
        if not entries:
            print(f"⚠️  No figures entry named '{name}'")
            return

    paths = [output for _, _, output, _ in entries]
    panel_sizes = Path(c.config.get("figures_dir")) / "panel_sizes.json"
    if not name:
        paths.append(panel_sizes)

    for path in paths:
        if path.is_file():
            path.unlink()
            print(f"🧹 Removed {path}")
