"""Contour extraction, smoothing and simplification.

Pipeline: DEM raster -> optional raster blur -> marching squares -> project to
local ground metres -> clip to the capture rectangle -> simplify -> Chaikin.

Blurring the *raster* before contouring gives far better looking lines than
smoothing the polylines afterwards, because it removes the stair-stepping at
source rather than averaging it out. Chaikin then just takes the last edge off.
"""
from __future__ import annotations

import numpy as np
import shapely
from contourpy import LineType, contour_generator

from .dem import Dem
from .geo import Region


# ------------------------------------------------------------------ raster blur

def _box1d(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    if r < 1:
        return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r, r)
    ap = np.pad(a, pad, mode="edge")
    c = np.cumsum(ap, axis=axis, dtype=np.float64)
    zero = np.zeros_like(np.take(c, [0], axis=axis))
    c = np.concatenate([zero, c], axis=axis)
    hi = np.take(c, np.arange(2 * r + 1, n + 2 * r + 1), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur(z: np.ndarray, sigma_px: float) -> np.ndarray:
    """NaN-aware approximate gaussian (three box passes)."""
    if sigma_px <= 0:
        return z
    r = int(round((np.sqrt(1 + 4 * sigma_px ** 2) - 1) / 2))
    if r < 1:
        return z
    ok = np.isfinite(z)
    num = np.where(ok, z, 0.0).astype(np.float32)
    den = ok.astype(np.float32)
    for _ in range(3):
        num = _box1d(_box1d(num, r, 0), r, 1)
        den = _box1d(_box1d(den, r, 0), r, 1)
    out = np.divide(num, den, out=np.full_like(num, np.nan), where=den > 1e-3)
    out[~ok & (den <= 0.5)] = np.nan
    return out


# ---------------------------------------------------------------------- Chaikin

def _chaikin_once(p: np.ndarray, closed: bool) -> np.ndarray:
    if closed:
        a, b = p, np.roll(p, -1, axis=0)
    else:
        a, b = p[:-1], p[1:]
    if len(a) == 0:
        return p
    out = np.empty((len(a) * 2, 2), dtype=np.float64)
    out[0::2] = 0.75 * a + 0.25 * b
    out[1::2] = 0.25 * a + 0.75 * b
    if not closed:
        out = np.vstack([p[:1], out, p[-1:]])
    return out


def chaikin(p: np.ndarray, iters: int, closed: bool) -> np.ndarray:
    for _ in range(max(0, iters)):
        if len(p) < 3:
            break
        p = _chaikin_once(p, closed)
    return p


# --------------------------------------------------------------------- levels

def choose_levels(zmin: float, zmax: float, interval: float,
                  base: float = 0.0) -> list[float]:
    if interval <= 0 or not np.isfinite(zmin) or not np.isfinite(zmax):
        return []
    start = np.ceil((zmin - base) / interval) * interval + base
    n = int(np.floor((zmax - base) / interval - (start - base) / interval)) + 1
    if n <= 0:
        return []
    if n > 2000:
        raise ValueError(
            f"{n} contour levels at {interval} m — raise the interval")
    return [round(start + i * interval, 6) for i in range(n)]


# ------------------------------------------------------------------- extraction

def _explode(geom) -> list[np.ndarray]:
    if geom is None or geom.is_empty:
        return []
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if g.is_empty:
            continue
        if g.geom_type == "LineString":
            out.append(np.asarray(g.coords, dtype=np.float64))
        elif g.geom_type in ("MultiLineString", "GeometryCollection"):
            out.extend(_explode(g))
    return out


def generate(dem: Dem, region: Region, *,
             interval: float = 50.0,
             index_every: int = 5,
             blur_sigma_px: float = 1.0,
             simplify_m: float = 2.0,
             smooth_iters: int = 1,
             min_length_m: float = 30.0,
             level_min: float | None = None,
             level_max: float | None = None,
             sea_fill: bool = False) -> dict:
    """Return contour polylines in local ground metres, split into a normal
    and an index (every Nth) layer.

    sea_fill replaces nodata with -1 m, which makes the 0 m contour close
    around the coast. Useful with IGN, whose grid simply stops at the
    shoreline; the global DEM has real bathymetry and does not need it."""
    z = dem.z
    if sea_fill:
        z = np.where(np.isfinite(z), z, -1.0).astype(np.float32)
    z = blur(z, blur_sigma_px)
    z = np.flipud(z)                      # contourpy wants ascending y
    finite = np.isfinite(z)
    if not finite.any():
        return {"features": [], "levels": [], "stats": dem.stats()}

    zmin = float(np.min(z[finite])) if level_min is None else level_min
    zmax = float(np.max(z[finite])) if level_max is None else level_max
    levels = choose_levels(zmin, zmax, interval)

    cg = contour_generator(
        x=dem.x_coords, y=dem.y_coords,
        z=np.ma.masked_invalid(z),
        line_type=LineType.Separate, corner_mask=True, chunk_size=0,
    )

    features: list[dict] = []
    n = 0
    for level in levels:
        raw = cg.lines(level)
        if not raw:
            continue
        lines = [ln for ln in raw if len(ln) >= 2]
        if not lines:
            continue
        sizes = [len(ln) for ln in lines]
        geoms = shapely.linestrings(
            np.vstack(lines),
            indices=np.repeat(np.arange(len(lines)), sizes),
        )
        if simplify_m > 0:
            geoms = shapely.simplify(geoms, simplify_m, preserve_topology=False)

        is_index = index_every > 1 and abs(
            round(level / (interval * index_every)) * interval * index_every - level
        ) < 1e-6
        layer = "contours-index" if is_index else "contours"

        for g in geoms:
            for pts in _explode(g):
                if len(pts) < 2:
                    continue
                closed = bool(np.allclose(pts[0], pts[-1], atol=1e-9))
                if closed and len(pts) > 3:
                    pts = chaikin(pts[:-1], smooth_iters, True)
                    pts = np.vstack([pts, pts[:1]])
                else:
                    pts = chaikin(pts, smooth_iters, False)
                length = float(np.hypot(*np.diff(pts, axis=0).T).sum())
                if length < min_length_m:
                    continue
                n += 1
                features.append({
                    "id": f"c{n}",
                    "layer": layer,
                    "level": level,
                    "closed": closed,
                    "pts": pts,
                })

    return {"features": features, "levels": levels, "stats": dem.stats()}
