"""Plotter-oriented SVG writer.

Top-level groups carry inkscape:groupmode="layer", which is what both Inkscape
and `vpype read` use to split an SVG into separate layers (one vpype layer per
top-level group). Dimensions are written in real millimetres with a matching
viewBox so the drawing arrives at the plotter at the intended physical size.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import numpy as np

from .geo import Region

# layer -> (stroke, width mm, draw order)
STYLE = {
    "contours":       ("#8a6a44", 0.25, 10),
    "contours-index": ("#5a3c1a", 0.45, 11),
    "sea-fill":       ("#4a90c4", 0.18, 12),
    "water-fill":     ("#4a90c4", 0.18, 13),
    "glaciers":       ("#6cb6e0", 0.25, 20),
    "sea":            ("#0b3d6b", 0.35, 21),
    "water":          ("#1f6fb5", 0.30, 22),
    "rivers":         ("#1f6fb5", 0.30, 22),
    "coastline":      ("#0b3d6b", 0.50, 23),
    "buildings":      ("#555555", 0.25, 30),
    "railways":       ("#444444", 0.35, 31),
    "roads":          ("#333333", 0.35, 32),
    "paths":          ("#a0522d", 0.25, 33),
    "trip":           ("#d02020", 0.60, 40),
    "peaks":          ("#000000", 0.30, 50),
    "places":         ("#000000", 0.30, 51),
    "labels":         ("#000000", 0.20, 60),
    "frame":          ("#000000", 0.40, 70),
}
NS = ('xmlns="http://www.w3.org/2000/svg" '
      'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
      'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"')


class Page:
    """Maps local ground metres (y up, centred) to page millimetres (y down)."""

    def __init__(self, region: Region, width_mm: float, margin_mm: float = 0.0):
        self.region = region
        self.scale = (width_mm - 2 * margin_mm) / region.width_m
        self.width_mm = width_mm
        self.height_mm = region.height_m * self.scale + 2 * margin_mm
        self.margin = margin_mm

    def xy(self, pts: np.ndarray) -> np.ndarray:
        """Page-frame ground metres (y up, centred) -> page mm (y down)."""
        p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        hw, hh = self.region.half
        return np.column_stack([
            self.margin + (p[:, 0] + hw) * self.scale,
            self.margin + (hh - p[:, 1]) * self.scale,
        ])


def _d(pts: np.ndarray, closed: bool, dp: int = 3) -> str:
    p = np.round(pts, dp)
    body = " ".join(f"{x:g},{y:g}" for x, y in p)
    return f"M {body}" + (" Z" if closed else "")


def _peak_marker(x: float, y: float, r: float = 1.2) -> str:
    return (f'<path d="M {x:g},{y - r:g} {x + r * 0.87:g},{y + r * 0.5:g} '
            f'{x - r * 0.87:g},{y + r * 0.5:g} Z"/>')


SELECTABLE_DEFAULT = frozenset({"paths", "roads"})


def render(features: list[dict], region: Region, *,
           width_mm: float = 297.0,
           margin_mm: float = 0.0,
           frame: bool = True,
           labels: bool = False,
           interactive: bool = False,
           selectable: set[str] | None = None,
           meta: dict | None = None) -> str:
    """interactive=True tags selectable paths with data-id so the browser
    preview can toggle them into the trip layer. Exports omit the tags."""
    page = Page(region, width_mm, margin_mm)
    pickable = set(SELECTABLE_DEFAULT if selectable is None else selectable)
    pickable.add("trip")
    buckets: dict[str, list[str]] = {}
    label_items: list[str] = []

    for f in features:
        layer = f.get("layer", "contours")
        if layer not in STYLE:
            continue
        if "point" in f:
            x, y = page.xy([f["point"]])[0]
            buckets.setdefault(layer, []).append(_peak_marker(x, y))
            if labels and f.get("name"):
                txt = html.escape(f["name"])
                if f.get("ele"):
                    txt += f" {f['ele']:.0f}"
                label_items.append(
                    f'<text x="{x + 2:.2f}" y="{y - 1:.2f}" '
                    f'font-size="2.5" font-family="sans-serif" '
                    f'fill="#000" stroke="none">{txt}</text>')
        elif f.get("pts") is not None and len(f["pts"]) >= 2:
            pts = page.xy(f["pts"])
            attr = ""
            if interactive and layer in pickable:
                attr = (f' data-id="{html.escape(str(f.get("id", "")))}"'
                        f' data-name="{html.escape(f.get("name") or "")}"'
                        f' class="sel"')
            buckets.setdefault(layer, []).append(
                f'<path d="{_d(pts, bool(f.get("closed")))}"{attr}/>')

    if labels and label_items:
        buckets["labels"] = label_items
    if frame:
        w, h = page.width_mm, page.height_mm
        m = page.margin
        buckets["frame"] = [
            f'<rect x="{m:g}" y="{m:g}" width="{w - 2 * m:g}" '
            f'height="{h - 2 * m:g}"/>']

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg {NS} width="{page.width_mm:.4g}mm" height="{page.height_mm:.4g}mm" '
        f'viewBox="0 0 {page.width_mm:.4g} {page.height_mm:.4g}">',
        f'<title>{html.escape((meta or {}).get("title", "Contours"))}</title>',
        f'<desc>{html.escape(_describe(region, meta))}</desc>',
    ]
    for layer in sorted(buckets, key=lambda k: STYLE[k][2]):
        stroke, lw, _ = STYLE[layer]
        items = buckets[layer]
        parts.append(
            f'<g inkscape:groupmode="layer" inkscape:label="{layer}" '
            f'id="{layer}" fill="none" stroke="{stroke}" '
            f'stroke-width="{lw}" stroke-linecap="round" '
            f'stroke-linejoin="round">')
        parts.extend(items)
        parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts)


def _describe(region: Region, meta: dict | None) -> str:
    m = meta or {}
    bits = [
        f"cartofab {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"centre {region.lat:.5f},{region.lon:.5f}",
        f"{region.width_m / 1000:.2f} x {region.height_m / 1000:.2f} km",
    ]
    for k in ("source", "interval", "resolution"):
        if m.get(k) is not None:
            bits.append(f"{k}={m[k]}")
    return " | ".join(bits)
