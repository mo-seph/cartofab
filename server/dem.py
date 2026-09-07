"""Digital elevation model fetching and mosaicking.

Nothing is bulk-downloaded: each source is queried on demand for the requested
region and cached on disk.

Two things here are less obvious than they look.

**Native grids.** Each source is requested in its own projection at its own
grid resolution, snapped to a multiple of that grid. Asking a WMS for an
arbitrary Web-Mercator grid instead makes the *server* resample, and at least
the IGN one appears to do so with a nearest-neighbour kernel: the result is a
moire of horizontal stripes that survives contouring and looks like corrupted
terrain. We fetch native and resample once, ourselves, with bilinear.

**Mosaicking.** Sources are composited in priority order onto a common grid in
the region's page frame, each filling only what previous ones left empty. A
capture spanning the French/Italian border therefore gets 5 m IGN LiDAR on one
side and 10 m TINITALY on the other rather than IGN's own very coarse
out-of-country padding.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
import numpy as np
import shapely
from PIL import Image
from pyproj import Transformer

from .cache import cache_get, cache_put
from .geo import Region

UA = {"User-Agent": "cartofab/0.1 (local plotter tool)"}
MERC_SPAN = 20037508.342789244

IGN_WMS = "https://data.geopf.fr/wms-r/wms"
IGN_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
IGN_MAX_PX = 4000

TINITALY_WCS = "https://tinitaly.pi.ingv.it/TINItaly_1_1/wcs"
TINITALY_COVERAGE = "TINItaly_1_1__tinitaly_dem"

TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRARIUM_MAX_Z = 15
# Terrarium tiles are served at whatever pixel spacing the zoom implies, but
# the data behind them is ~30 m almost everywhere. Report the honest number.
TERRARIUM_TRUE_RES = 30.0

# Scottish Public Sector LiDAR, as Cloud-Optimised GeoTIFFs on AWS Open Data.
# The portal's WMS only renders styled images and its WCS is switched off, so
# the COGs are the only route to raw elevation. Coverage is genuinely patchy —
# excellent around Glencoe, Arran, Orkney, the Outer Hebrides and the central
# belt, absent over Skye, Torridon, the Cairngorms and Assynt — which the
# mosaic handles by simply falling through to the next source.
SCOT_BUCKET = "https://srsp-open-data.s3.eu-west-2.amazonaws.com"
SCOT_SETS = [        # (S3 prefix, native res m, rough lon/lat extent)
    ("lidar/outer-hebrides/2019/dtm/25cm/27700/gridded/", 0.25, (-7.7, 57.0, -6.0, 58.6)),
    ("lidar/national-lidar-programme/dtm/27700/gridded/", 0.5, (-6.9, 54.3, -3.5, 56.2)),
    ("lidar/orkney-islands-council-23/dtm/27700/gridded/", 0.5, (-3.5, 58.7, -2.3, 59.4)),
    ("lidar/phase-6/dtm/27700/gridded/", 0.5, (-5.1, 55.5, -4.1, 56.0)),
    ("lidar/phase-5/dtm/27700/gridded/", 0.5, (-4.8, 55.6, -2.5, 56.5)),
    ("lidar/phase-4/dtm/27700/gridded/", 0.5, (-5.2, 55.0, -2.0, 56.6)),
    ("lidar/phase-3/dtm/27700/gridded/", 0.5, (-5.3, 54.5, -1.7, 56.2)),
    ("lidar/phase-2/dtm/27700/gridded/", 1.0, (-6.8, 55.4, -1.1, 60.3)),
    ("lidar/phase-1/dtm/27700/gridded/", 1.0, (-6.1, 54.7, -1.7, 59.2)),
]
SCOT_FLOOR = -30.0
_OS_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
_QUADRANT = {"SW": (0, 0), "SE": (1, 0), "NW": (0, 1), "NE": (1, 1)}

# IGN's layer answers with a *very* coarse fill outside French territory
# rather than nodata, so it would otherwise silently win priority over a
# better national source (or over the global tiles in, say, Switzerland).
# National sources are therefore clipped to a real territorial polygon.
IGN_FLOOR = -15.0
DEEPEST_OCEAN = -12000.0


@dataclass(frozen=True)
class Territory:
    name: str
    epsg: int
    bbox: tuple[float, float, float, float]   # lon/lat


IGN_TERRITORIES = [
    Territory("metropole", 2154, (-5.5, 41.2, 9.9, 51.3)),
    Territory("guadeloupe", 32620, (-61.9, 15.7, -60.7, 16.6)),
    Territory("martinique", 32620, (-61.3, 14.3, -60.7, 15.0)),
    Territory("guyane", 2972, (-54.7, 2.0, -51.5, 6.0)),
    Territory("reunion", 2975, (55.1, -21.5, 55.9, -20.8)),
    Territory("mayotte", 4471, (44.9, -13.1, 45.4, -12.6)),
]
TINITALY_BBOX = (6.6, 35.4, 18.6, 47.1)

COVERAGE_FILE = Path(__file__).parent / "data" / "coverage.json"


@lru_cache(maxsize=1)
def _coverage_shapes() -> dict:
    raw = json.loads(COVERAGE_FILE.read_text())
    out = {}
    for key, geom in raw.items():
        g = shapely.from_geojson(json.dumps(geom))
        shapely.prepare(g)
        out[key] = g
    return out


def coverage(name: str | None):
    return _coverage_shapes().get(name) if name else None


def covers_region(name: str | None, region: Region) -> bool:
    g = coverage(name)
    if g is None:
        return True
    pts = region.outline_wgs(12)
    poly = shapely.polygons(np.array(pts + [pts[0]]))
    return bool(shapely.intersects(g, poly))


# --------------------------------------------------------------------- results

@dataclass
class Grid:
    """A raster in some projected CRS. Row 0 is north."""
    z: np.ndarray
    bbox: tuple[float, float, float, float]
    epsg: int
    native_m: float = 0.0        # finest real resolution behind this raster


@dataclass
class Dem:
    """Elevation on the region's page-frame grid, in ground metres."""
    z: np.ndarray                 # (ny, nx), row 0 = north (max y)
    region: Region
    res: float
    sources: list[dict]           # {label, share} in the order they filled

    @property
    def x_coords(self) -> np.ndarray:
        hw = self.region.width_m / 2
        return -hw + self.res * (np.arange(self.z.shape[1]) + 0.5)

    @property
    def y_coords(self) -> np.ndarray:
        """Ascending; pair with np.flipud(z)."""
        hh = self.region.height_m / 2
        return -hh + self.res * (np.arange(self.z.shape[0]) + 0.5)

    def stats(self) -> dict:
        ok = np.isfinite(self.z)
        if not ok.any():
            return {"min": None, "max": None, "coverage": 0.0}
        return {"min": float(self.z[ok].min()), "max": float(self.z[ok].max()),
                "coverage": float(ok.mean())}


# ------------------------------------------------------------------- utilities

def _bbox_overlaps(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _clean(a: np.ndarray, floor: float, erode: bool = False) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    ok = np.isfinite(a) & (a >= floor) & (a <= 9000)
    if erode and not ok.all():
        e = ok.copy()
        e[1:, :] &= ok[:-1, :]; e[:-1, :] &= ok[1:, :]
        e[:, 1:] &= ok[:, :-1]; e[:, :-1] &= ok[:, 1:]
        ok = e
    if ok.all():
        return a
    out = a.copy()
    out[~ok] = np.nan
    return out


def _despeckle(a: np.ndarray, tol: float = 60.0,
                cluster_tol: float = 250.0, passes: int = 3) -> np.ndarray:
    """Drop corrupt spikes from an elevation grid.

    The global terrain tiles carry occasional bad pixels: one reading -1880 m
    in the middle of ground that is 6 m all around it (the red channel comes
    back 8 low, which is 2048 m). They are rare — 23 in the 768x768 mosaic at
    Croabh Haven, 91 at the Cuillin — but a model's height is max minus min, so
    a single one turns a 140 m hillside into a 1600 m cube, and any that falls
    below the sea threshold becomes its own little rectangular "lake".

    Two tests, because the spikes sometimes come in touching pairs and then
    each one is its neighbour's alibi:

    * isolated: further than `tol` from *every* one of its eight neighbours.
      Real terrain always continues into at least one neighbour — a sea cliff
      is safe, because its neighbours along the cliff share its height.
    * clustered: further than `cluster_tol` from the neighbourhood median.
      Nothing real moves 250 m within one 30 m cell, but a pair of spikes is
      still surrounded by seven good cells each.

    Repeating the pass peels off larger clumps one rim at a time."""
    a = np.asarray(a, dtype=np.float32)
    if a.size < 9:
        return a
    out = a
    for _ in range(passes):
        pad = np.pad(out, 1, mode="constant", constant_values=np.nan)
        nb = np.stack([pad[dy:dy + out.shape[0], dx:dx + out.shape[1]]
                       for dy in (0, 1, 2) for dx in (0, 1, 2)
                       if (dy, dx) != (1, 1)])
        with np.errstate(invalid="ignore"):
            closest = np.nanmin(np.abs(nb - out[None]), axis=0)
            med = np.nanmedian(nb, axis=0)
            bad = np.isfinite(out) & (
                (np.isfinite(closest) & (closest > tol))
                | (np.isfinite(med) & (np.abs(out - med) > cluster_tol)))
        if not bad.any():
            break
        nxt = out.copy()
        nxt[bad] = med[bad]
        out = nxt
    return out


def _snap(lo: float, hi: float, res: float) -> tuple[float, float, int]:
    a = math.floor(lo / res) * res
    b = math.ceil(hi / res) * res
    return a, b, max(1, int(round((b - a) / res)))


def _split(n: int, chunk: int) -> list[tuple[int, int]]:
    return [(i, min(i + chunk, n)) for i in range(0, n, chunk)]


def _sample(grid: Grid, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """Bilinear sample of `grid` at projected coords X, Y. NaN outside, and
    NaN wherever any contributing cell is NaN, so nodata edges stay honest."""
    minx, miny, maxx, maxy = grid.bbox
    ny, nx = grid.z.shape
    fx = (X - minx) / (maxx - minx) * nx - 0.5
    fy = (maxy - Y) / (maxy - miny) * ny - 0.5
    x0 = np.floor(fx).astype(np.int64); y0 = np.floor(fy).astype(np.int64)
    tx = (fx - x0).astype(np.float32); ty = (fy - y0).astype(np.float32)
    inside = (x0 >= 0) & (y0 >= 0) & (x0 < nx - 1) & (y0 < ny - 1)
    x0 = np.clip(x0, 0, nx - 2); y0 = np.clip(y0, 0, ny - 2)
    z = grid.z
    v = ((z[y0, x0] * (1 - tx) + z[y0, x0 + 1] * tx) * (1 - ty)
         + (z[y0 + 1, x0] * (1 - tx) + z[y0 + 1, x0 + 1] * tx) * ty)
    return np.where(inside, v, np.nan).astype(np.float32)


# ------------------------------------------------------------------------ IGN

def ign_territory(region: Region) -> Territory | None:
    b = region.bbox_wgs(0)
    for t in IGN_TERRITORIES:
        if _bbox_overlaps(b, t.bbox):
            return t
    return None


async def _ign_patch(client, epsg, bbox, w, h) -> np.ndarray:
    import tifffile
    key = "ignN:" + hashlib.sha1(
        f"{IGN_LAYER}|{epsg}|{bbox}|{w}x{h}".encode()).hexdigest()
    blob = cache_get(key, ".tif")
    if blob is None:
        r = await client.get(IGN_WMS, params={
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
            "LAYERS": IGN_LAYER, "STYLES": "", "CRS": f"EPSG:{epsg}",
            "BBOX": ",".join(f"{v:.3f}" for v in bbox),
            "WIDTH": str(w), "HEIGHT": str(h), "FORMAT": "image/geotiff",
        }, headers=UA, timeout=180.0)
        r.raise_for_status()
        if "tiff" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"IGN: {r.text[:200]}")
        blob = r.content
        cache_put(key, ".tif", blob)
    a = tifffile.imread(io.BytesIO(blob))
    return a[..., 0] if a.ndim == 3 else a


async def fetch_ign(region: Region, want_res: float) -> Grid | None:
    terr = ign_territory(region)
    if terr is None:
        return None
    # RGE ALTI is a 1 m grid; step out in whole multiples so the server can
    # decimate cleanly instead of interpolating.
    res = max(1.0, float(round(max(want_res, 1.0))))
    tr = Transformer.from_crs(4326, terr.epsg, always_xy=True)
    pts = region.outline_wgs(24)
    xs, ys = tr.transform([p[0] for p in pts], [p[1] for p in pts])
    pad = res * 4
    minx, maxx, nx = _snap(min(xs) - pad, max(xs) + pad, res)
    miny, maxy, ny = _snap(min(ys) - pad, max(ys) + pad, res)
    if nx * ny > 80_000_000:
        return None

    out = np.full((ny, nx), np.nan, dtype=np.float32)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        sem = asyncio.Semaphore(4)

        async def run(y0, y1, x0, x1):
            sub = (minx + x0 * res, maxy - y1 * res,
                   minx + x1 * res, maxy - y0 * res)
            async with sem:
                out[y0:y1, x0:x1] = await _ign_patch(
                    client, terr.epsg, sub, x1 - x0, y1 - y0)

        await asyncio.gather(*(run(y0, y1, x0, x1)
                               for y0, y1 in _split(ny, IGN_MAX_PX)
                               for x0, x1 in _split(nx, IGN_MAX_PX)))
    return Grid(_clean(out, IGN_FLOOR, erode=True),
                (minx, miny, maxx, maxy), terr.epsg, res)


# ------------------------------------------------------------------- TINITALY

async def fetch_tinitaly(region: Region, want_res: float) -> Grid | None:
    import tifffile
    if not _bbox_overlaps(region.bbox_wgs(0), TINITALY_BBOX):
        return None
    # The WCS always answers at native 10 m regardless of what we ask for, so
    # snap to 10 m and size-guard on the native pixel count, not the request.
    res = 10.0
    tr = Transformer.from_crs(4326, 32632, always_xy=True)
    pts = region.outline_wgs(24)
    xs, ys = tr.transform([p[0] for p in pts], [p[1] for p in pts])
    pad = res * 4
    minx, maxx, nx = _snap(min(xs) - pad, max(xs) + pad, res)
    miny, maxy, ny = _snap(min(ys) - pad, max(ys) + pad, res)
    if nx * ny > 30_000_000:
        raise RuntimeError("region too large for the TINITALY service")

    key = "tinitaly:" + hashlib.sha1(
        f"{minx},{miny},{maxx},{maxy},{nx}x{ny}".encode()).hexdigest()
    blob = cache_get(key, ".tif")
    if blob is None:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(TINITALY_WCS, params={
                "service": "WCS", "version": "2.0.1", "request": "GetCoverage",
                "coverageId": TINITALY_COVERAGE,
                "subset": [f"E({minx:.0f},{maxx:.0f})",
                           f"N({miny:.0f},{maxy:.0f})"],
                "format": "image/tiff",
            }, headers=UA, timeout=180.0)
        if r.status_code != 200 or b"ExceptionReport" in r.content[:2000]:
            raise RuntimeError(f"TINITALY: HTTP {r.status_code}")
        blob = r.content
        cache_put(key, ".tif", blob)
    a = tifffile.imread(io.BytesIO(blob))
    if a.ndim == 3:
        a = a[..., 0]
    # WCS honours the subset but picks its own pixel count; trust its extent.
    return Grid(_clean(a, -100.0, erode=True), (minx, miny, maxx, maxy), 32632, 10.0)


# ------------------------------------------------------------------- Scotland

def os_square_origin(letters: str) -> tuple[int, int]:
    """Origin (easting, northing) of a 100 km OS square such as 'NR'."""
    a, b = _OS_LETTERS.index(letters[0]), _OS_LETTERS.index(letters[1])
    e = ((a % 5) * 5 + (b % 5)) * 100_000 - 1_000_000
    n = (4 - a // 5) * 500_000 + (4 - b // 5) * 100_000 - 500_000
    return e, n


def os_tile_bbox(ref: str) -> tuple[float, float, float, float] | None:
    """Bounding box of a tile named by OS grid reference.

    Handles the three conventions in the bucket: 'HY20' (10 km),
    'NS16NE' (5 km quadrant) and 'NR5807' (1 km)."""
    ref = ref.upper()
    if len(ref) < 4 or ref[0] not in _OS_LETTERS or ref[1] not in _OS_LETTERS:
        return None
    e0, n0 = os_square_origin(ref[:2])
    rest = ref[2:]
    quad = None
    if len(rest) > 2 and rest[-2:] in _QUADRANT:
        quad, rest = _QUADRANT[rest[-2:]], rest[:-2]
    if not rest.isdigit() or len(rest) % 2:
        return None
    h = len(rest) // 2
    size = 10 ** (5 - h)
    e = e0 + int(rest[:h]) * size
    n = n0 + int(rest[h:]) * size
    if quad is not None:
        size //= 2
        e += quad[0] * size
        n += quad[1] * size
    return (e, n, e + size, n + size)


async def _scot_keys(client, prefix: str, square: str) -> list[str]:
    """Tile keys for one dataset within one 100 km square (disk-cached)."""
    key = f"scotidx:{prefix}{square}"
    blob = cache_get(key, ".txt")
    if blob is not None:
        return [k for k in blob.decode().split("\n") if k]
    keys: list[str] = []
    token = None
    for _ in range(20):
        params = {"list-type": "2", "prefix": prefix + square, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = await client.get(SCOT_BUCKET + "/", params=params, timeout=90.0)
        r.raise_for_status()
        body = r.text
        keys += re.findall(r"<Key>([^<]+)</Key>", body)
        m = re.search(r"<NextContinuationToken>([^<]+)<", body)
        if not m:
            break
        token = m.group(1)
    cache_put(key, ".txt", "\n".join(keys).encode())
    return keys


async def fetch_scotland(region: Region, want_res: float) -> Grid | None:
    from .cog import read_level

    overlap = [s for s in SCOT_SETS if _bbox_overlaps(region.bbox_wgs(0), s[2])]
    if not overlap:
        return None

    tr = Transformer.from_crs(4326, 27700, always_xy=True)
    pts = region.outline_wgs(24)
    xs, ys = tr.transform([p[0] for p in pts], [p[1] for p in pts])
    res = max(0.25, want_res)
    pad = res * 4
    minx, maxx, nx = _snap(min(xs) - pad, max(xs) + pad, res)
    miny, maxy, ny = _snap(min(ys) - pad, max(ys) + pad, res)
    if nx * ny > 40_000_000:
        raise RuntimeError("region too large for the Scottish LiDAR source")
    want = (minx, miny, maxx, maxy)

    squares = set()
    for a in range(len(_OS_LETTERS)):
        for b in range(len(_OS_LETTERS)):
            sq = _OS_LETTERS[a] + _OS_LETTERS[b]
            e, n = os_square_origin(sq)
            if _bbox_overlaps(want, (e, n, e + 100_000, n + 100_000)):
                squares.add(sq)
    if not squares:
        return None

    out = np.full((ny, nx), np.nan, dtype=np.float32)
    native = []
    async with httpx.AsyncClient(follow_redirects=True, headers=UA) as client:
        sem = asyncio.Semaphore(6)

        async def keys_for(prefix, sq):
            async with sem:
                try:
                    return await _scot_keys(client, prefix, sq)
                except Exception:                      # noqa: BLE001
                    return []

        listings = await asyncio.gather(*(keys_for(p, sq)
                                          for p, _, _ in overlap
                                          for sq in sorted(squares)))
        # keep dataset priority: iterate the listings in SCOT_SETS order
        per_set: dict[str, list[str]] = {p: [] for p, _, _ in overlap}
        i = 0
        for prefix, _, _ in overlap:
            for _ in sorted(squares):
                per_set[prefix] += listings[i]
                i += 1

        loop = asyncio.get_running_loop()

        def read_one(url):
            """Decoded tiles are cached, not just the S3 listing — otherwise
            every render re-downloads the same COG overviews."""
            ck = "scottile:" + hashlib.sha1(f"{url}|{res}".encode()).hexdigest()
            blob = cache_get(ck, ".npz")
            if blob is not None:
                with np.load(io.BytesIO(blob)) as z:
                    return z["arr"], tuple(z["bbox"].tolist()), float(z["res"])
            with httpx.Client(follow_redirects=True, headers=UA, timeout=90.0) as c:
                arr, bbox, tres = read_level(url, c, res)
            buf = io.BytesIO()
            np.savez_compressed(buf, arr=arr, bbox=np.asarray(bbox, dtype=np.float64),
                                res=np.float64(tres))
            cache_put(ck, ".npz", buf.getvalue())
            return arr, bbox, tres

        for prefix, _, _ in overlap:
            if np.isfinite(out).all():
                break
            todo = []
            for key in per_set[prefix]:
                if not key.endswith(".tif"):
                    continue
                bbox = os_tile_bbox(key.rsplit("/", 1)[-1].split("_")[0])
                if bbox and _bbox_overlaps(want, bbox):
                    todo.append(key)
            if not todo:
                continue

            async def grab(key):
                async with sem:
                    try:
                        return await loop.run_in_executor(
                            None, read_one, f"{SCOT_BUCKET}/{key}")
                    except Exception:                  # noqa: BLE001
                        return None

            for result in await asyncio.gather(*(grab(k) for k in todo[:200])):
                if result is None:
                    continue
                arr, tb, tres = result
                arr = _clean(arr, SCOT_FLOOR)
                native.append(tres)
                _paste(out, want, res, arr, tb, tres)

    if not np.isfinite(out).any():
        return None
    return Grid(out, want, 27700, min(native) if native else res)


def _paste(out, out_bbox, out_res, arr, arr_bbox, arr_res) -> None:
    """Nearest-neighbour paste of a tile into the mosaic, filling gaps only.

    Both grids are axis-aligned in the same CRS, and tiles are at or finer than
    the mosaic, so this is an index remap rather than a resample; the single
    proper interpolation happens later, in fetch_dem."""
    ominx, ominy, omaxx, omaxy = out_bbox
    ny, nx = out.shape
    ah, aw = arr.shape
    x0 = max(0, int(np.floor((arr_bbox[0] - ominx) / out_res)))
    x1 = min(nx, int(np.ceil((arr_bbox[2] - ominx) / out_res)))
    y0 = max(0, int(np.floor((omaxy - arr_bbox[3]) / out_res)))
    y1 = min(ny, int(np.ceil((omaxy - arr_bbox[1]) / out_res)))
    if x1 <= x0 or y1 <= y0:
        return
    cx = ominx + (np.arange(x0, x1) + 0.5) * out_res
    cy = omaxy - (np.arange(y0, y1) + 0.5) * out_res
    ix = ((cx - arr_bbox[0]) / arr_res).astype(np.int64)
    iy = ((arr_bbox[3] - cy) / arr_res).astype(np.int64)
    okx = (ix >= 0) & (ix < aw)
    oky = (iy >= 0) & (iy < ah)
    if not okx.any() or not oky.any():
        return
    sub = arr[np.clip(iy, 0, ah - 1)[:, None], np.clip(ix, 0, aw - 1)[None, :]]
    sub = np.where(oky[:, None] & okx[None, :], sub, np.nan)
    dst = out[y0:y1, x0:x1]
    gap = ~np.isfinite(dst) & np.isfinite(sub)
    dst[gap] = sub[gap]


# ------------------------------------------------------------------ terrarium

def _zoom_for(res_m: float, lat: float) -> int:
    g0 = 156543.03392 * math.cos(math.radians(lat))
    return max(0, min(TERRARIUM_MAX_Z, int(math.ceil(math.log2(g0 / max(res_m, 0.5))))))


async def _terrarium_tile(client, z, x, y) -> np.ndarray | None:
    key = f"terr:{z}/{x}/{y}"
    blob = cache_get(key, ".png")
    if blob is None:
        try:
            r = await client.get(TERRARIUM_URL.format(z=z, x=x, y=y),
                                 headers=UA, timeout=60.0)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        blob = r.content
        cache_put(key, ".png", blob)
    img = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"), dtype=np.float32)
    return img[..., 0] * 256.0 + img[..., 1] + img[..., 2] / 256.0 - 32768.0


async def fetch_terrarium(region: Region, want_res: float) -> Grid | None:
    z = _zoom_for(want_res, region.lat)
    n = 2 ** z
    span = 2 * MERC_SPAN / n
    to_merc = Transformer.from_crs(4326, 3857, always_xy=True)
    pts = region.outline_wgs(24)
    xs, ys = to_merc.transform([p[0] for p in pts], [p[1] for p in pts])
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)

    tx0 = max(0, int((minx + MERC_SPAN) // span))
    tx1 = min(n - 1, int((maxx + MERC_SPAN) // span))
    ty0 = max(0, int((MERC_SPAN - maxy) // span))
    ty1 = min(n - 1, int((MERC_SPAN - miny) // span))
    nxt, nyt = tx1 - tx0 + 1, ty1 - ty0 + 1
    if nxt * nyt > 400:
        raise RuntimeError("region too large for the global DEM at this sampling")

    mosaic = np.full((nyt * 256, nxt * 256), np.nan, dtype=np.float32)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        sem = asyncio.Semaphore(8)

        async def run(tx, ty):
            async with sem:
                t = await _terrarium_tile(client, z, tx, ty)
            if t is not None:
                mosaic[(ty - ty0) * 256:(ty - ty0 + 1) * 256,
                       (tx - tx0) * 256:(tx - tx0 + 1) * 256] = t

        await asyncio.gather(*(run(tx, ty) for ty in range(ty0, ty1 + 1)
                               for tx in range(tx0, tx1 + 1)))
    bbox = (tx0 * span - MERC_SPAN, MERC_SPAN - (ty1 + 1) * span,
            (tx1 + 1) * span - MERC_SPAN, MERC_SPAN - ty0 * span)
    return Grid(_clean(_despeckle(mosaic), DEEPEST_OCEAN), bbox, 3857,
                max(span / 256 / region.merc_scale, TERRARIUM_TRUE_RES))


# ------------------------------------------------------------------- registry

# (key, label, fetcher, territory, best native resolution m) -- finest first.
# Territory names index server/data/coverage.json; None means global.
SOURCES = [
    ("scotland", "Scottish LiDAR (0.25-1 m, where flown)", fetch_scotland, "britain", 2.0),
    ("ign", "IGN RGE ALTI (FR, 1-5 m)", fetch_ign, "france", 5.0),
    ("tinitaly", "TINITALY (IT, 10 m)", fetch_tinitaly, "italy", 10.0),
    ("terrarium", "Global terrain tiles (~30 m)", fetch_terrarium, None, 30.0),
]
SOURCE_LABELS = {k: v for k, v, _, _, _ in SOURCES}
SOURCE_RES = {k: r for k, _, _, _, r in SOURCES}


async def scotland_coverage(region: Region) -> list[dict]:
    """How much of the region each Scottish LiDAR dataset actually tiles.

    Coverage is patchy, so a territorial polygon only says "in Scotland", not
    "surveyed" — and a single tile clipping one corner does not make a map. At
    Croabh Haven exactly one phase-1 tile touches the region, over 13% of it and
    with no usable data even there, while the map is really built from 30 m
    global tiles. Tile listings are disk-cached and their names carry their own
    OS grid reference, so this costs no raster downloads.

    Returns {native_m, share} per dataset, finest first."""
    overlap = [t for t in SCOT_SETS if _bbox_overlaps(region.bbox_wgs(0), t[2])]
    if not overlap:
        return []
    tr = Transformer.from_crs(4326, 27700, always_xy=True)
    pts = region.outline_wgs(24)
    xs, ys = tr.transform([p[0] for p in pts], [p[1] for p in pts])
    want = shapely.box(min(xs), min(ys), max(xs), max(ys))
    squares = sorted({
        _OS_LETTERS[a] + _OS_LETTERS[b]
        for a in range(len(_OS_LETTERS)) for b in range(len(_OS_LETTERS))
        if _bbox_overlaps(want.bounds,
                          (lambda e, n: (e, n, e + 100_000, n + 100_000))(
                              *os_square_origin(_OS_LETTERS[a] + _OS_LETTERS[b])))})
    if not squares:
        return []
    out = []
    async with httpx.AsyncClient(follow_redirects=True, headers=UA) as client:
        for prefix, native, _ in overlap:
            boxes = []
            for sq in squares:
                try:
                    keys = await _scot_keys(client, prefix, sq)
                except Exception:                      # noqa: BLE001
                    continue
                for key in keys:
                    if not key.endswith(".tif"):
                        continue
                    bb = os_tile_bbox(key.rsplit("/", 1)[-1].split("_")[0])
                    if bb and _bbox_overlaps(want.bounds, bb):
                        boxes.append(shapely.box(*bb))
            if boxes:
                share = shapely.union_all(boxes).intersection(want).area / want.area
                if share > 0:
                    out.append({"native_m": native, "share": round(share, 3)})
    out.sort(key=lambda d: d["native_m"])
    return out


# Below this share of the region, a source is a fringe contributor: naming its
# resolution as "what you can get here" would send you sampling at 1 m for a
# map that is 30 m almost everywhere.
DOMINANT_SHARE = 0.6


async def probe_sources(region: Region) -> list[dict]:
    """What each source would really give here, finest first.

    `available()` answers from territory alone, which overstates Scotland: at
    Croabh Haven it promises 2 m LiDAR where one tile clips a corner and the map
    is actually built from 30 m global tiles."""
    out = []
    for key, label, _fn, terr, native in SOURCES:
        if not covers_region(terr, region):
            continue
        entry = {"key": key, "label": label, "nominal_m": native,
                 "native_m": native, "share": 1.0, "available": True}
        if key == "scotland":
            cov = await scotland_coverage(region)
            best = next((c for c in cov if c["share"] >= DOMINANT_SHARE), None)
            partial = cov[0] if cov else None
            if best is not None:
                entry.update(native_m=best["native_m"], share=best["share"])
            elif partial is not None:
                entry.update(native_m=partial["native_m"],
                             share=partial["share"], available=False)
            else:
                entry.update(native_m=None, share=0.0, available=False)
        out.append(entry)
    return out


async def best_resolution(region: Region) -> tuple[float, list[dict]]:
    """The finest sampling worth asking for here, and why."""
    got = await probe_sources(region)
    best = min((g["native_m"] for g in got
                if g["available"] and g["native_m"]), default=30.0)
    return best, got


def available(region: Region) -> list[str]:
    return [k for k, _, _, terr, _ in SOURCES if covers_region(terr, region)]


async def fetch_dem(region: Region, res: float, source: str = "auto") -> Dem:
    """Composite every applicable source, best first, onto the page grid."""
    hw, hh = region.width_m / 2, region.height_m / 2
    nx = max(8, int(round(region.width_m / res)))
    ny = max(8, int(round(region.height_m / res)))
    px = -hw + res * (np.arange(nx) + 0.5)
    py = hh - res * (np.arange(ny) + 0.5)          # row 0 = north
    PX, PY = np.meshgrid(px, py)
    lon, lat = region.page_to_wgs(PX, PY)

    wanted = [k for k, _, _, _, _ in SOURCES] if source == "auto" else [source]
    out = np.full((ny, nx), np.nan, dtype=np.float32)
    used: list[str] = []
    errors: list[str] = []

    for key, label, fn, terr, _ in SOURCES:
        if key not in wanted or np.isfinite(out).all():
            continue
        if not covers_region(terr, region):
            continue
        try:
            grid = await fn(region, res)
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"{key}: {exc}")
            continue
        if grid is None or not np.isfinite(grid.z).any():
            continue
        tr = Transformer.from_crs(4326, grid.epsg, always_xy=True)
        X, Y = tr.transform(lon, lat)
        v = _sample(grid, X, Y)
        g = coverage(terr)
        if g is not None:
            v = np.where(shapely.contains_xy(g, lon, lat), v, np.nan)
        gap = ~np.isfinite(out) & np.isfinite(v)
        if gap.any():
            out[gap] = v[gap]
            used.append({"label": label, "share": round(float(gap.mean()), 4),
                         "native_m": round(grid.native_m, 2) or None})

    if not used and errors:
        raise RuntimeError("; ".join(errors))
    return Dem(out, region, res, used)
