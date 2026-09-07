"""Render the sample images used in the README.

Deliberately offline and deterministic: it drives the same code the app does
and shades the mesh itself, so the pictures can be regenerated after a change
without anyone having to take a screenshot at the right moment.

    .venv/bin/python docs/make_samples.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server import mesh as M                                    # noqa: E402
from server.main import Spec, collect, render_svg               # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REGION = dict(lat=45.83262, lon=6.86500, width_m=8000, height_m=8000)


def isometric(Z, xs, ys, exag=2.4, size=(1100, 760), light=(-0.5, -0.7, 0.55)):
    """Flat-shaded isometric view of the terrain grid, painted back to front."""
    ny, nx = Z.shape
    zs = (Z - np.nanmin(Z)) * exag
    span = max(xs.max() - xs.min(), ys.max() - ys.min())
    X, Y = np.meshgrid((xs - xs.mean()) / span, (ys - ys.mean()) / span)
    zn = zs / span

    # a 30-degree isometric, y-up on the page
    ca, sa = np.cos(np.radians(30.0)), np.sin(np.radians(30.0))
    px = (X - Y) * ca
    py = (X + Y) * sa - zn

    w, h = size
    sx = 0.86 * w / max(px.max() - px.min(), 1e-9)
    sy = 0.86 * h / max(py.max() - py.min(), 1e-9)
    s = min(sx, sy)
    ux = (px - px.mean()) * s + w / 2
    uy = (py - py.mean()) * s + h / 2

    # surface normals, for a single directional light
    gy, gx = np.gradient(zs, ys[1] - ys[0] if ny > 1 else 1.0,
                         xs[1] - xs[0] if nx > 1 else 1.0)
    n = np.dstack([-gx, -gy, np.ones_like(zs)])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    L = np.asarray(light, dtype=float)
    L /= np.linalg.norm(L)
    lam = np.clip(n @ L, 0.0, 1.0)

    from PIL import ImageDraw
    img = Image.new("RGB", size, (14, 16, 20))
    dr = ImageDraw.Draw(img)
    base = np.array([200, 190, 172], dtype=float)

    # The model is a solid, not a sheet: carry the near edges down to a floor
    # so it reads as something you could pick up. Which edges face the viewer
    # depends on the grid's direction, so pick the two that project lowest.
    fy = float(uy.max()) + 0.09 * float(uy.max() - uy.min())
    edges = [np.column_stack([ux[0, :], uy[0, :]]),
             np.column_stack([ux[-1, :], uy[-1, :]]),
             np.column_stack([ux[:, 0], uy[:, 0]]),
             np.column_stack([ux[:, -1], uy[:, -1]])]
    edges.sort(key=lambda e: float(e[:, 1].mean()), reverse=True)
    wall = tuple(int(q) for q in base * 0.30)
    for e in edges[:2]:
        pts = [(float(a), float(b)) for a, b in e]
        skirt = pts + [(x, fy) for x, _ in reversed(pts)]
        dr.polygon(skirt, fill=wall)

    # depth order: whatever ends up lowest on the page is nearest the viewer,
    # so paint by increasing page y rather than trusting the grid's direction
    order = sorted(range(ny - 1), key=lambda j: float(uy[j].mean()))
    for j in order:
        for i in range(nx - 1):
            v = 0.28 + 0.72 * float(lam[j, i])
            c = tuple(int(min(255, q)) for q in base * v)
            dr.polygon([(ux[j, i], uy[j, i]), (ux[j, i + 1], uy[j, i + 1]),
                        (ux[j + 1, i + 1], uy[j + 1, i + 1]),
                        (ux[j + 1, i], uy[j + 1, i])], fill=c)
    return img


async def main() -> None:
    # A README image does not need every vertex: simplify harder than a plot
    # would, so the file stays small enough to load comfortably on a page.
    svg_spec = Spec(**REGION, interval=50, resolution_m=20, index_every=5,
                    simplify_m=8.0, min_length_m=120.0, blur_sigma_px=2.0,
                    layers=[], osm_source="overpass")
    c = await collect(svg_spec)
    out = render_svg(svg_spec, c, False)
    (HERE / "sample-contours.svg").write_text(out["svg"], encoding="utf-8")
    print("sample-contours.svg", len(out["svg"]), "bytes")

    dem = c["dem"]
    n = 220
    Z, xs, ys = M.resample(dem, n, n, fill=None)
    img = isometric(np.asarray(Z, dtype=float), np.asarray(xs), np.asarray(ys))
    img.save(HERE / "sample-mesh.png")
    print("sample-mesh.png", img.size)


if __name__ == "__main__":
    asyncio.run(main())
