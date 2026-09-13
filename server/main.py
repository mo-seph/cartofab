"""cartofab — local web app for contour maps and printable terrain."""
from __future__ import annotations

import asyncio
import io
import math
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import shapely
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import contours, localosm, mesh, osm, svg, water
from .cache import cache_clear, cache_size
from .dem import (SOURCE_LABELS, SOURCE_RES, available,
                  best_resolution, fetch_dem)
from .geo import Region

from shapely.ops import unary_union


def _union(geoms):
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return None
    u = unary_union(geoms)
    return None if u.is_empty else u


def _rings_of(geom) -> list:
    """Every ring of a polygon or multipolygon, as coordinate arrays."""
    import numpy as np
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if g.is_empty or g.geom_type != "Polygon":
            continue
        out.append(np.asarray(g.exterior.coords, dtype=np.float64))
        out.extend(np.asarray(r.coords, dtype=np.float64) for r in g.interiors)
    return out


ROOT = Path(__file__).parent.parent
WEB = ROOT / "web"
EXPORTS = ROOT / "exports"
MAX_DEM_PIXELS = 60_000_000
# Public Overpass instances can stall for minutes. Contours never depend on
# them, so the vector layers get a hard wall-clock budget and the map is
# returned without them (with a warning) rather than making the user wait.
# Ceiling for the whole vector phase. Individual layers are capped separately
# (see osm.fetch), so this is a backstop rather than the usual limit.
OSM_DEADLINE_S = 60.0
OSM_LAYER_TIMEOUT_S = 25.0

app = FastAPI(title="cartofab")


class Spec(BaseModel):
    lat: float
    lon: float
    width_m: float = Field(8000, gt=50, le=400_000)
    height_m: float = Field(8000, gt=50, le=400_000)
    rotation_deg: float = 0.0

    resolution_m: float = Field(10.0, ge=0.25, le=500)
    dem_source: str = "auto"

    interval: float = Field(50.0, gt=0)
    index_every: int = Field(5, ge=0, le=50)
    level_min: float | None = None
    level_max: float | None = None

    blur_sigma_px: float = Field(1.5, ge=0, le=32)
    simplify_m: float = Field(2.0, ge=0, le=500)
    smooth_iters: int = Field(1, ge=0, le=5)
    min_length_m: float = Field(40.0, ge=0)
    sea_fill: bool = False

    layers: list[str] = []
    contours_on: bool = True

    # water treatment
    water_hatch: str = "none"                 # none | hatch | cross
    hatch_spacing_mm: float = Field(1.5, gt=0.05, le=50)
    hatch_angle_deg: float = 45.0
    water_mask: bool = False                  # erase contours/rivers beneath
    include_sea: bool = False                 # treat low ground as water
    sea_level: float = 0.0
    sea_source: str = "elevation"             # or "coastline": use OSM's line

    selectable: list[str] = ["paths", "roads"]
    osm_source: str = "auto"                  # auto | overpass | local
    output: str = "svg"                       # svg | mesh

    # ---- mesh output -------------------------------------------------------
    mesh_n: int = Field(400, ge=32, le=1600)   # grid points on the long side
    z_exaggeration: float = Field(3.0, gt=0, le=50)
    base_mm: float = Field(3.0, ge=0, le=100)
    nozzle_mm: float = Field(0.4, gt=0.01, le=5)
    mesh_water: bool = True
    water_style: str = "polygon"              # polygon | grid
    # Smoothing the elevation before meshing, in ground metres. Separate from
    # the contour blur: that one is in pixels and tuned for line work, while
    # this exists because bilinear sampling of a coarse source shows the source
    # cells as flat facets once the mesh grid is finer than the data.
    mesh_smooth_m: float = Field(0.0, ge=0, le=500)
    flat_tolerance: float = Field(0.02, ge=0, le=1)   # slope counted as flat
    water_mm: float = Field(0.4, gt=0, le=20)   # slab thickness, own object
    max_height_mm: float = Field(0.0, ge=0, le=500)   # 0 = no limit
    backplate: bool = False
    backplate_w_mm: float = Field(240.0, gt=5, le=2000)
    backplate_h_mm: float = Field(200.0, gt=5, le=2000)
    backplate_mm: float = Field(0.5, gt=0, le=50)
    mesh_buildings: bool = False
    mesh_roads: bool = False
    buildings_mm: float = Field(2.0, gt=0, le=50)
    roads_mm: float = Field(0.6, gt=0, le=50)
    mesh_trip: bool = False                   # raise the selected trip
    trip_mm: float = Field(1.0, gt=0, le=50)  # how far it stands proud
    trip_w_mm: float = Field(1.5, gt=0, le=50)  # its width on the model

    # The drawing and the model are sized independently. Sharing one field
    # made "Model size" mean the page in SVG mode, which is exactly the kind of
    # double meaning that caused confusion before.
    width_mm: float = Field(200.0, gt=5, le=2000)      # SVG drawing width
    height_mm: float | None = None            # None = follow the capture aspect
    model_w_mm: float = Field(200.0, gt=5, le=2000)    # mesh model width
    model_h_mm: float | None = None
    margin_mm: float = Field(0.0, ge=0, le=200)
    frame: bool = True
    labels: bool = False

    trip_ids: list[str] = []

    def _needs_coastline(self) -> bool:
        return self.include_sea and self.sea_source == "coastline"

    def effective_layers(self) -> set[str]:
        """Layers to fetch, including whatever the mesh options require.

        The Layers chips choose what is fetched and the mesh checkboxes choose
        what is extruded; wanting roads in the model but not having ticked the
        roads layer used to produce a model with no roads and no explanation."""
        want = set(self.layers)
        if self.output == "mesh":
            if self.mesh_buildings:
                want.add("buildings")
            if self.mesh_roads:
                # roads only — pulling every footpath in as well buries the
                # model in a mesh of tracks. Tick the paths layer to add them.
                want.add("roads")
            if self.mesh_water:
                want.add("water")
            if self.mesh_trip and self.trip_ids:
                # a trip is a set of feature ids with no layer of their own, so
                # fetch whatever the trip was allowed to be picked from
                want |= set(self.selectable)
        # Deriving the sea from OSM needs the line itself. Without this the
        # option silently does nothing, which is how the mesh lost its roads.
        if self._needs_coastline():
            want.add("coastline")
        return want

    def region(self) -> Region:
        return Region(self.lat, self.lon, self.width_m, self.height_m,
                      self.rotation_deg)


async def collect(spec: Spec) -> dict:
    """Fetch everything an output needs: elevation and OSM vectors.

    Deliberately knows nothing about contours or meshes — both outputs are
    built from the same collected material."""
    region = spec.region()
    warnings: list[str] = []
    t0 = time.time()

    px = (spec.width_m / spec.resolution_m) * (spec.height_m / spec.resolution_m)
    if px > MAX_DEM_PIXELS:
        raise HTTPException(400, (
            f"{px / 1e6:.0f} Mpx of elevation requested — coarsen the "
            f"resolution or shrink the area (limit {MAX_DEM_PIXELS // 10**6} Mpx)."))

    stats: dict = {}
    dem_info: dict = {}
    dem_obj = None
    osm_info: dict = {}
    timings: dict = {}
    need_dem = (spec.output == "mesh" or spec.contours_on or spec.include_sea)

    async def do_dem():
        nonlocal stats, dem_info, dem_obj
        if not need_dem:
            return
        t_dem = time.time()
        dem = await fetch_dem(region, spec.resolution_m, spec.dem_source)
        timings["elevation"] = round(time.time() - t_dem, 2)
        dem_obj = dem
        dem_info = {
            "source": " + ".join(x["label"] for x in dem.sources) or "none",
            "sources": dem.sources,
            "resolution_m": round(dem.res, 2),
            "grid": list(dem.z.shape)}
        # A high-resolution source covering only part of the area leaves a hard
        # seam where the coarse fallback takes over; say so rather than let it
        # look like bad terrain.
        finest = min((x["native_m"] for x in dem.sources if x.get("native_m")),
                     default=None)
        if finest and spec.resolution_m < finest * 0.99:
            warnings.append(
                f"sampling at {spec.resolution_m:g} m but the best data here is "
                f"{finest:g} m — you get no extra detail for the extra time; "
                f"{finest:g} m gives the same lines")
        # Only worth mentioning when a *much* coarser source fills a
        # *meaningful* share. One national source handing over to another of
        # similar quality, or a 4% sliver at the edge, is not a seam worth
        # warning about.
        best = dem.sources[0] if dem.sources else None
        if best and best.get("native_m"):
            coarse = sum(x["share"] for x in dem.sources[1:]
                         if x.get("native_m", 0) >= best["native_m"] * 3)
            if coarse >= 0.10:
                warnings.append(
                    f"{best['label']} covers {best['share'] * 100:.0f}% of this "
                    f"area; {coarse * 100:.0f}% comes from a much coarser "
                    "source, so expect a visible change in detail there")
        s = dem.stats()
        stats = s
        if s["coverage"] < 0.999:
            warnings.append(f"elevation data covers {s['coverage'] * 100:.0f}% "
                            "of the area")


    async def do_osm():
        """Overpass first; fall back to a local extract if one covers this area.

        Vector layers never hold up the contours, and a failure here is
        reported rather than swallowed, so the UI can offer the download."""
        if not spec.effective_layers():
            return []
        t_o = time.time()
        want = spec.effective_layers()
        simp = min(spec.simplify_m, 3.0)
        store = localosm.find_store(region)

        def done(feats):
            timings["osm"] = round(time.time() - t_o, 2)
            return feats

        async def from_local(reason: str | None = None):
            # A store imported with a layer subset simply has no rows for the
            # others; say so rather than quietly drawing nothing.
            have = store.get("layers")
            if have is not None:
                missing = sorted(want - set(have))
                if missing:
                    warnings.append(
                        f"the local {store['name']} extract was imported "
                        f"without {', '.join(missing)} — extend it to add "
                        "those layers, or switch the source to Overpass")
            feats = await asyncio.to_thread(
                localosm.query, store["id"], region, want, simplify_m=simp)
            osm_info.update(source="local", store=store["id"],
                            store_name=store["name"])
            if reason:
                warnings.append(
                    f"{reason} — drew the OSM layers from your local "
                    f"{store['name']} extract instead")
            return feats

        if spec.osm_source == "local":
            if store is None:
                # "nothing installed" and "installed but clipped short of here"
                # need different actions, so never report them the same way.
                ext = localosm.extendable_store(region)
                if ext and ext["id"] in localosm.active:
                    eta = localosm.jobs.get(
                        localosm.active[ext["id"]], {}).get("eta_s")
                    msg = (f"the local {ext['name']} extract is being imported "
                           "right now — it covers this area only once that "
                           "finishes"
                           + (f" (about {eta}s left)" if eta else ""))
                elif ext:
                    msg = (f"the local {ext['name']} extract reaches here but "
                           "was imported clipped to a smaller area — extend it"
                           + (" (its source file was kept, so no download)"
                              if ext.get("has_source") else ""))
                else:
                    msg = "no local OSM extract covers this area"
                osm_info.update(source="none", failed=msg,
                                extendable=ext["id"] if ext else None)
                warnings.append(msg)
                return done([])
            return done(await from_local())

        # Once an extract is installed it is complete and instant, so prefer it
        # rather than waiting on Overpass every time. "overpass" forces the
        # live service (with the extract still there as a safety net).
        if spec.osm_source == "auto" and store is not None:
            return done(await from_local())

        try:
            feats, warns = await asyncio.wait_for(
                osm.fetch(region, want, simplify_m=simp,
                          per_layer_timeout=OSM_LAYER_TIMEOUT_S),
                timeout=OSM_DEADLINE_S)
            warnings.extend(warns)
            osm_info.update(source="overpass")
            # Overpass often half-works: some layers answer and others time
            # out. That is not a clean failure, but it does mean the map is
            # missing data, so say so — it is the moment a local copy is
            # worth offering.
            if warns:
                osm_info["degraded"] = "; ".join(warns)[:200]
            return done(feats)
        except Exception as exc:                       # noqa: BLE001
            reason = (f"Overpass timed out after {OSM_DEADLINE_S:.0f}s"
                      if isinstance(exc, asyncio.TimeoutError)
                      else f"Overpass unavailable ({exc})")
            # "auto" already returned above if a store existed, so this is
            # the explicit-Overpass path falling back rather than losing the
            # layers entirely.
            if store is not None:
                return done(await from_local(reason))
            osm_info.update(source="none", failed=reason)
            warnings.append(f"{reason} — contours are unaffected")
            return done([])

    try:
        _, of = await asyncio.gather(do_dem(), do_osm())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    coast = await _coast_features(spec, warnings) \
        if spec._needs_coastline() else []

    return {"region": region, "dem": dem_obj, "dem_info": dem_info,
            "elevation": stats, "osm": list(of), "osm_info": osm_info,
            "coast": coast,
            "warnings": warnings, "timings": timings, "t0": t0}


def render_svg(spec: Spec, c: dict, interactive: bool) -> dict:
    """Contours + water treatment + the layered SVG."""
    region, warnings, timings = c["region"], c["warnings"], c["timings"]
    dem_obj, dem_info, stats = c["dem"], c["dem_info"], c["elevation"]
    t0 = c["t0"]

    feats: list[dict] = []
    if spec.contours_on and dem_obj is not None:
        t_c = time.time()
        res = contours.generate(
            dem_obj, region,
            interval=spec.interval, index_every=spec.index_every,
            blur_sigma_px=spec.blur_sigma_px, simplify_m=spec.simplify_m,
            smooth_iters=spec.smooth_iters, min_length_m=spec.min_length_m,
            level_min=spec.level_min, level_max=spec.level_max,
            sea_fill=spec.sea_fill)
        timings["contours"] = round(time.time() - t_c, 2)
        feats.extend(res["features"])
    feats.extend(c["osm"])

    # ---- water ---------------------------------------------------------
    t_w = time.time()
    page = svg.Page(region, spec.width_mm, spec.margin_mm)
    lake = _union([f["poly"] for f in feats
                   if f.get("layer") == "water" and f.get("poly") is not None])
    sea = None
    if spec.include_sea and dem_obj is not None:
        try:
            sea = _derive_sea(spec, dem_obj, region, feats, warnings,
                              c.get("coast"))
        except Exception as exc:                       # noqa: BLE001
            warnings.append(f"could not derive the sea: {exc}")

    if sea is not None:
        for pts in _rings_of(sea):
            feats.append({"id": f"sea{len(feats)}", "layer": "sea",
                          "closed": True, "pts": pts})

    if spec.water_mask:
        blocker = _union([g for g in (lake, sea) if g is not None])
        if blocker is not None:
            feats = water.mask_features(
                feats, blocker, {"contours", "contours-index", "rivers"},
                min_length=spec.min_length_m)

    if spec.water_hatch in ("hatch", "cross"):
        spacing_m = spec.hatch_spacing_mm / page.scale
        cross = spec.water_hatch == "cross"
        for geom, layer in ((lake, "water-fill"), (sea, "sea-fill")):
            if geom is None:
                continue
            for n, pts in enumerate(water.hatch(geom, spacing_m,
                                                spec.hatch_angle_deg, cross)):
                feats.append({"id": f"{layer}{n}", "layer": layer, "pts": pts})

    # polygons are carried through as geometry; turn them into drawable outlines
    expanded: list[dict] = []
    for f in feats:
        if f.get("poly") is not None:
            for n, pts in enumerate(_rings_of(f["poly"])):
                expanded.append({k: v for k, v in f.items() if k != "poly"}
                                | {"pts": pts, "closed": True,
                                   "id": f"{f.get('id','a')}{f'r{n}' if n else ''}"})
        else:
            expanded.append(f)
    feats = expanded

    timings["water"] = round(time.time() - t_w, 2)

    trip = set(spec.trip_ids)
    if trip:
        for f in feats:
            if f.get("id") in trip:
                f["layer"] = "trip"

    t_s = time.time()
    doc = svg.render(
        feats, region, width_mm=spec.width_mm, margin_mm=spec.margin_mm,
        frame=spec.frame, labels=spec.labels, interactive=interactive,
        selectable=set(spec.selectable),
        meta={"title": f"Contours {spec.lat:.4f},{spec.lon:.4f}",
              "source": dem_info.get("source"), "interval": spec.interval,
              "resolution": dem_info.get("resolution_m")})

    timings["svg"] = round(time.time() - t_s, 2)

    per_layer: dict[str, dict] = {}
    for f in feats:
        d = per_layer.setdefault(f["layer"], {"count": 0, "length_m": 0.0})
        d["count"] += 1
        if f.get("pts") is not None and len(f["pts"]) > 1:
            d["length_m"] += float(np.hypot(*np.diff(f["pts"], axis=0).T).sum())

    for d in per_layer.values():
        d["pen_m"] = round(d["length_m"] * page.scale / 1000.0, 1)  # metres of pen
        d["length_m"] = round(d["length_m"])

    return {
        "svg": doc,
        "elevation": stats,
        "dem": dem_info,
        "layers": per_layer,
        "page": {"width_mm": round(page.width_mm, 2),
                 "height_mm": round(page.height_mm, 2),
                 "scale": f"1:{round(1 / page.scale * 1000):,}"},
        "osm": c["osm_info"],
        "timings": timings,
        "warnings": warnings,
        "seconds": round(time.time() - t0, 2),
        "cache_mb": round(cache_size() / 1e6, 1),
    }


# Rough carriageway widths by highway class, in ground metres. Anything
# narrower than the nozzle can print is widened to it rather than dropped.
def _nearest_level(part, water_polys):
    """Which water body a ring belongs to, so each keeps its own level."""
    p = part.representative_point()
    for geom, _m, lvl in water_polys:
        if geom.intersects(p):
            return lvl
    best, bd = None, None
    for geom, _m, lvl in water_polys:
        d = geom.distance(p)
        if bd is None or d < bd:
            best, bd = lvl, d
    return best


def _simplify_area(geom, frame, nozzle_mm: float):
    """Drop boundary detail finer than the printer can lay down.

    Simplifying with preserve_topology keeps every part alive, however small,
    so a shoreline threshold that throws off 0.9 m2 slivers hands the
    triangulator shapes flatter than its own tolerance — degenerate, and enough
    to leave the water shell open, which then silently falls back to the
    stepped grid slab. Anything smaller than the tolerance square cannot be
    printed anyway, so drop it rather than simplify it into a splinter."""
    if geom is None or geom.is_empty:
        return geom
    tol = 0.5 * nozzle_mm / frame.scale
    out = geom.simplify(tol, preserve_topology=True)
    if out.is_empty:
        return geom
    if not out.is_valid:
        out = out.buffer(0)
    floor = tol * tol
    parts = [g for g in _polys_of(out) if g.area >= floor]
    if not parts:
        return None
    kept = [shapely.Polygon(
                g.exterior,
                [r for r in g.interiors if shapely.Polygon(r).area >= floor])
            for g in parts]
    out = kept[0] if len(kept) == 1 else shapely.MultiPolygon(kept)
    return out if out.is_valid else out.buffer(0)


# The weld tolerance is what turns a shared point into a shared edge, so the
# separation has to clear it by a wide margin — at 1x-5x, buffering re-vertexes
# the boundary and *creates* coincidences faster than it removes them (measured
# at Croabh Haven: 285 and 396 bad edges, against 1 before touching anything).
# 20x is comfortably clear and costs 0.002 mm on the model at any scale.
UNPINCH_WELD_MULTIPLE = 20.0


def _unpinch(parts: list, frame) -> list:
    """Pull apart water bodies that meet at a single point.

    Two polygons touching at a corner are perfectly legal — a valid
    MultiPolygon, and shapely will not dissolve them — but extruding both puts
    four wall faces on one vertical edge, and that single non-manifold edge is
    enough to throw the whole smooth shoreline away for the stepped grid slab.
    Nudging only the parts that actually touch keeps the cost to a rounding
    error: eroding every part instead loses sixteen times as much area and
    leaves thousands of bad edges of its own."""
    if len(parts) < 2:
        return parts
    eps = UNPINCH_WELD_MULTIPLE * mesh.WELD_MM / frame.scale
    out = list(parts)
    tree = shapely.STRtree(out)
    for i, geom in enumerate(out):
        for j in tree.query(geom.buffer(eps)):
            j = int(j)
            if j <= i or out[i].distance(out[j]) > eps:
                continue
            small = i if out[i].area <= out[j].area else j
            shrunk = out[small].buffer(-eps)
            if shrunk.is_empty:
                continue
            out[small] = (shrunk if shrunk.geom_type == "Polygon"
                          else max(shrunk.geoms, key=lambda g: g.area))
    return [g for g in out if not g.is_empty]


def _clear_of(parts: list, blockers: list, frame) -> list:
    """Pull `parts` back from `blockers` so their extrusions cannot weld.

    A burn running into a loch touches the loch's slab at a point. Both are
    water, but they are built separately — one levelled, one draped — and two
    slabs sharing a single vertical edge is non-manifold, which reports the
    whole water object as open. Subtracting a hair around the flat bodies keeps
    the streams clear; the gap is the same 0.002 mm on the model as _unpinch."""
    if not blockers or not parts:
        return parts
    eps = UNPINCH_WELD_MULTIPLE * mesh.WELD_MM / frame.scale
    keep_out = shapely.union_all(blockers).buffer(eps)
    out = []
    for g in parts:
        if g.intersects(keep_out):
            g = g.difference(keep_out)
        out += [q for q in _polys_of(g) if q.area > eps * eps]
    return out


def _polys_of(geom):
    """Every Polygon in a geometry, whatever container it arrived in."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, shapely.Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, shapely.Polygon)]


def _print_clean(geom, frame, nozzle_mm: float):
    """Snap geometry to a grid far below the printer's resolution.

    Unioning hundreds of buffered roads leaves hair-thin slivers and vertices a
    micron apart, and those are what pinch an otherwise sound mesh into
    non-manifold edges. `set_precision` snaps to a grid and repairs whatever
    that invalidates, which is exactly the problem — measured on Glencoe's road
    network it takes 12,359 non-manifold edges to zero, and Andorra's from
    92,418 to a handful, while also removing 40% of the triangles. Growing and
    shrinking the outline instead barely helped, and simplifying collapses thin
    features outright.
    """
    if geom is None or geom.is_empty:
        return geom
    grid = 0.1 * nozzle_mm / frame.scale         # ground metres
    try:
        out = shapely.set_precision(geom, grid)
    except Exception:                            # noqa: BLE001
        return geom
    if out.is_empty:
        return geom
    if not out.is_valid:
        out = out.buffer(0)
    return None if out.is_empty else out


ROAD_WIDTH_M = {
    "motorway": 14, "trunk": 12, "primary": 10, "secondary": 9, "tertiary": 8,
    "unclassified": 6, "residential": 6, "living_street": 5, "service": 4,
    "track": 3.5, "cycleway": 2.5, "bridleway": 2, "path": 1.5,
    "footway": 1.5, "steps": 1.5,
}


# How much wider than the map the coastline is read before the sea is cut back
# to size, and the ceiling on that so a small capture cannot turn into a huge
# Overpass query.
SEA_PAD = 3.0
MAX_SEA_SPAN_M = 60_000.0


async def _coast_features(spec: "Spec", warnings: list) -> list:
    """Coastline over a wider box than the map, in the same page frame.

    Fetched separately because layers come back clipped to the region, and the
    orientation vote needs the coast to cross the frame rather than clip a
    corner of it. A concentric region shares the page frame exactly, so nothing
    has to be transformed."""
    big = Region(spec.lat, spec.lon,
                 min(spec.width_m * SEA_PAD, MAX_SEA_SPAN_M),
                 min(spec.height_m * SEA_PAD, MAX_SEA_SPAN_M),
                 spec.rotation_deg)
    store = localosm.find_store(big)
    try:
        if store is not None and spec.osm_source != "overpass":
            return await asyncio.to_thread(
                localosm.query, store["id"], big, {"coastline"}, simplify_m=0.0)
        if spec.osm_source == "local":
            return []                       # local only, and no store reaches
        feats, _ = await asyncio.wait_for(
            osm.fetch(big, {"coastline"}, simplify_m=0.0,
                      per_layer_timeout=OSM_LAYER_TIMEOUT_S),
            timeout=OSM_DEADLINE_S)
        return feats
    except Exception as exc:                           # noqa: BLE001
        warnings.append(f"could not read the coastline beyond the map ({exc}) "
                        "— fell back to the part inside it")
        return []


def _derive_sea(spec: "Spec", dem_obj, region, feats: list, warnings: list,
                coast: list | None = None):
    """The sea, from whichever source the spec asks for.

    The coastline is the better answer wherever OSM has one: it is a surveyed
    line at full detail, where thresholding a 30 m grid gives a staircase of
    100 m blocks — and around an intricate coast the global tiles are simply
    wrong about where the water is."""
    if spec.sea_source == "coastline":
        # Prefer the wider read; fall back to what is inside the map if the
        # extra fetch came back empty.
        wide = bool(coast)
        sea = water.sea_from_coastline(
            coast if wide else feats, region, warnings.append, dem_obj,
            outer=SEA_PAD if wide else 1.0)
        if sea is not None and dem_obj is not None:
            _warn_sea_disagrees(sea, dem_obj, region, warnings)
        return sea

    sea = _sea_or_warn(dem_obj, spec.sea_level, region, warnings)
    # Finding a shore by elevation fails badly on exactly the coasts worth
    # drawing, and it fails quietly — a handful of specks rather than nothing,
    # so the map comes back with a coastline drawn and nothing inside it. If
    # OSM has a surveyed line right here, say so rather than let it look broken.
    area = region.width_m * region.height_m
    got = 0.0 if sea is None or sea.is_empty else sea.area / area
    if got < 0.02 and any(f.get("layer") == "coastline" for f in feats):
        warnings.append(
            "there is a surveyed OSM coastline in this area and the elevation "
            "threshold barely found any water — set \u201cSea from\u201d to "
            "the coastline to use it")
    return sea


def _warn_sea_disagrees(sea, dem_obj, region, warnings: list) -> None:
    """Say when the elevation grid puts hills where OSM puts water.

    Worth knowing before printing: the sea gets flattened to one level, so
    ground the DEM thinks is 40 m high becomes a cliff at the shoreline."""
    X, Y = np.meshgrid(dem_obj.x_coords, dem_obj.y_coords)
    inside = shapely.contains_xy(sea, X.ravel(), Y.ravel()).reshape(X.shape)
    # ascending y, so the grid must be flipped to line up with the mask
    z = np.flipud(np.asarray(dem_obj.z, dtype=float))
    wet = inside & np.isfinite(z)
    if not wet.any():
        return
    high = float((z[wet] > 15.0).mean())
    if high > 0.05:
        warnings.append(
            f"the elevation data disagrees with the coastline over "
            f"{high * 100:.0f}% of the sea — it reads that ground as land, and "
            "it will be flattened to sea level")


def _sea_or_warn(dem_obj, level: float, region, warnings: list):
    """The sea by elevation threshold, saying so when that finds nothing.

    Over water the global terrain tiles are radar noise, not a flat plane: at
    Croabh Haven the sea reads anywhere from 0.5 m to 8 m, so a 0 m threshold
    picks up a few square metres and the model comes back with no sea at all.
    There is no plateau to detect — the area just grows with the threshold — so
    the honest thing is to report it and name a level that would work."""
    sea = water.sea_polygon(dem_obj, level)
    area = region.width_m * region.height_m
    got = 0.0 if sea is None or sea.is_empty else sea.area / area
    if got >= 0.005:
        return sea
    suggestion = None
    for cand in np.arange(level + 0.5, level + 8.01, 0.5):
        try:
            s2 = water.sea_polygon(dem_obj, float(cand))
        except Exception:                              # noqa: BLE001
            break
        if s2 is not None and not s2.is_empty and s2.area / area >= 0.02:
            suggestion = float(cand)
            break
    msg = (f"almost no sea found at {level:g} m "
           f"({got * 100:.2f}% of the map)")
    if suggestion is not None:
        msg += (f" — this elevation data reads the water surface as noise a few "
                f"metres above zero, so try a sea level of about "
                f"{suggestion:g} m")
    warnings.append(msg)
    return sea


def render_mesh(spec: Spec, c: dict) -> tuple[list, dict]:
    """Terrain solid, flat water, and optional raised buildings and roads."""
    region, dem = c["region"], c["dem"]
    warnings, timings = c["warnings"], c["timings"]
    if dem is None:
        raise HTTPException(400, "no elevation data for this area")
    t_m = time.time()

    # keep the mesh grid square on the ground, whatever the aspect
    long_n = spec.mesh_n
    if region.width_m >= region.height_m:
        nx = long_n
        ny = max(2, round(long_n * region.height_m / region.width_m))
    else:
        ny = long_n
        nx = max(2, round(long_n * region.width_m / region.height_m))

    Z, xs, ys = mesh.resample(
        dem, nx, ny, fill=spec.sea_level if spec.include_sea else None)
    # Smooth before anything reads the surface. Water is flattened from Z and
    # the terrain is recessed to match, so blurring afterwards would round the
    # water off and lift the recess back through the slab.
    if spec.mesh_smooth_m > 0 and Z.shape[1] > 1:
        cell = (xs[-1] - xs[0]) / max(1, Z.shape[1] - 1)
        sigma_px = spec.mesh_smooth_m / max(cell, 1e-9)
        # the box-blur approximation rounds its radius down to zero below
        # about 0.8 cells, so small values would otherwise do nothing quietly
        if sigma_px >= 0.8:
            Z = contours.blur(Z, sigma_px)
        else:
            warnings.append(
                f"mesh smoothing of {spec.mesh_smooth_m:g} m is below one mesh "
                f"cell ({cell:.1f} m) and had no effect — raise it, or lower "
                "the mesh resolution")
    if spec.level_min is not None:
        Z = np.maximum(Z, spec.level_min)
    if spec.level_max is not None:
        Z = np.minimum(Z, spec.level_max)

    lake = _union([f["poly"] for f in c["osm"]
                   if f.get("layer") == "water" and f.get("poly") is not None])
    sea = None
    if spec.include_sea:
        try:
            sea = _derive_sea(spec, dem, region, c["osm"], warnings,
                              c.get("coast"))
        except Exception as exc:                           # noqa: BLE001
            warnings.append(f"could not derive the sea: {exc}")

    water_info = []
    water_polys = []
    flowing = []            # both are read below whether or not water is on
    if spec.mesh_water:
        # Each body gets its own level. Sharing one across every lake sinks the
        # high ones: at Arthur's Seat that is a 71 m hole through Dunsapie Loch.
        bodies = []
        if lake is not None and not lake.is_empty:
            bodies += [(g, None, "lake") for g in
                       (lake.geoms if hasattr(lake, "geoms") else [lake])]
        if sea is not None and not sea.is_empty:
            bodies.append((sea, spec.sea_level, "sea"))
        levels = []
        skipped = 0
        dem_level = mesh.dem_level_in(dem)
        for geom, level, label in bodies:
            m = mesh.water_mask(xs, ys, geom)
            raw_mask = m.copy()
            n = int(m.sum())
            # Grow the flat region so it fully covers the polygon. The mask is
            # rasterised at mesh vertices, so without this the surface slopes
            # up between a flattened vertex and its unflattened neighbour and
            # breaks through the slab: measured at Arthur's Seat, one cell
            # still leaves 0.2 mm proud, two leaves nothing.
            m = mesh.dilate(m, 2)
            # A pond smaller than a mesh cell grabs whichever vertex happens to
            # fall in it — often one sitting on the bank — and drags it to a
            # wrong height, leaving a spike. It cannot be represented at this
            # resolution anyway.
            if n < 3:
                skipped += n > 0
                continue
            # Not everything tagged as water is a lake. A burn falling down a
            # hillside spans a large height range, and flattening it to one
            # level carves a hole at the top and a ridge at the bottom. Judge
            # by how far the terrain under it actually falls, relative to how
            # far it reaches: a real water surface is flat, so any spread is
            # error, while a mountain stream has real gradient.
            zs = Z[raw_mask]
            spread = float(zs.max() - zs.min())
            mnx, mny, mxx, mxy = geom.bounds
            extent = max(mxx - mnx, mxy - mny, 1.0)
            if spread > max(1.5, spec.flat_tolerance * extent):
                flowing.append((geom, raw_mask, spread / extent))
                continue

            # take the level from the full-resolution DEM, not the coarse mesh
            lvl = level if level is not None else dem_level(geom)
            flat = mesh.flatten_water(Z, m, lvl)
            if flat is None:
                continue
            levels.append((label, flat, float(m.mean())))
            # keep the undilated mask for the slab, so the shoreline follows
            # the water itself rather than the grown flat region
            water_polys.append((geom, raw_mask, flat))
        if skipped:
            warnings.append(f"{skipped} water bodies are smaller than a mesh "
                            "cell and were left as terrain")
        for label in ("lake", "sea"):
            group = [x for x in levels if x[0] == label]
            if not group:
                continue
            share = sum(x[2] for x in group)
            lo = min(x[1] for x in group)
            hi = max(x[1] for x in group)
            water_info.append({
                "what": "lakes" if label == "lake" else "sea",
                "count": len(group),
                "level_m": (round(lo, 1) if lo == hi
                            else f"{lo:.1f}–{hi:.1f}"),
                "share": round(share, 4)})

    # ---- vertical scale -------------------------------------------------
    scale = spec.model_w_mm / region.width_m
    relief_m = float(Z.max() - Z.min())
    exag = spec.z_exaggeration
    if spec.max_height_mm and relief_m > 1e-6:
        # Everything that adds height has to come out of the budget: the base,
        # the backplate, the water recess (which drops the floor by exactly one
        # slab thickness), and whichever of buildings or roads stands tallest.
        proud = max(spec.buildings_mm if spec.mesh_buildings else 0.0,
                    spec.roads_mm if spec.mesh_roads else 0.0,
                    spec.trip_mm if spec.mesh_trip else 0.0)
        room = (spec.max_height_mm - spec.base_mm
                - (spec.backplate_mm if spec.backplate else 0.0)
                - (spec.water_mm if water_polys else 0.0)
                - proud)
        if room <= 0:
            raise HTTPException(400, (
                f"a {spec.max_height_mm:g} mm ceiling leaves no room above a "
                f"{spec.base_mm:g} mm base"))
        fit = room / (relief_m * scale)
        if fit < exag:
            warnings.append(
                f"exaggeration reduced from {exag:g}x to {fit:.2f}x to keep the "
                f"model under {spec.max_height_mm:g} mm")
            exag = fit

    # Water is its own object, so the terrain is recessed by its thickness and
    # the slab drops into the gap — the visible surface still sits at the true
    # water level.
    plate_t = spec.backplate_mm if spec.backplate else 0.0
    recess_m = (spec.water_mm / (scale * exag)
                if (water_polys or flowing) else 0.0)
    surface = Z.copy()               # the terrain before the channels are cut
    for _, m, _ in water_polys:
        Z[m] -= recess_m
    for _, m, _ in flowing:
        Z[mesh.dilate(m, 1)] -= recess_m

    frame = mesh.Frame(region, spec.model_w_mm, exag,
                       spec.base_mm + plate_t, float(Z.min()),
                       height_mm=spec.model_h_mm)
    objects = []
    if spec.backplate:
        model_h = (spec.model_h_mm
                   or spec.model_w_mm * region.height_m / region.width_m)
        if (spec.backplate_w_mm < spec.model_w_mm - 1e-6
                or spec.backplate_h_mm < model_h - 1e-6):
            warnings.append("the backplate is smaller than the model, so the "
                            "terrain will overhang it")
        objects.append(mesh.plate(spec.backplate_w_mm, spec.backplate_h_mm,
                                  plate_t, "backplate", (0.18, 0.18, 0.20)))
    objects.append(mesh.terrain_solid(Z, xs, ys, frame, floor_mm=plate_t))

    water_parts: list = []
    if water_polys:
        # One slab per body, each at its own level, built on the mesh grid so
        # it is manifold however tangled the shoreline is and lines up exactly
        # with the recess underneath.
        def grid_water():
            all_mask = np.zeros_like(Z, dtype=bool)
            tops = np.zeros_like(Z, dtype=np.float64)
            for _g, m, lvl in water_polys:
                all_mask |= m
                # heights on a grown mask, so a cell the rasteriser fills in to
                # break a diagonal pinch still has a level to sit at
                tops[mesh.dilate(m, 2)] = frame.z(lvl)
            return mesh.grid_slab(all_mask, xs, ys, frame, tops,
                                  spec.water_mm + 0.05, "water",
                                  (0.16, 0.45, 0.70))

        def polygon_water():
            # The terrain recess is dilated two cells past the polygon, so the
            # slab's own smooth edge sits inside the flattened region and the
            # stepped recess stays hidden underneath it.
            geoms, lv = [], {}
            for g, _m, lvl in water_polys:
                # Water wants simplifying but not snapping: it is a smooth
                # area, and snapping it to a grid creates the coincident
                # vertices that snapping *fixes* on a tangle of road buffers.
                # Measured on Loch Lomond: simplify alone is closed and 63%
                # cheaper, snapping leaves 236 open edges.
                cleaned = _simplify_area(g, frame, spec.nozzle_mm)
                if cleaned is None or cleaned.is_empty:
                    continue
                geoms.append(cleaned)
                lv[id(cleaned)] = lvl
            if not geoms:
                return None
            geoms = _unpinch([g for gg in geoms for g in _polys_of(gg)], frame)
            if not geoms:
                return None
            return mesh.extrude(
                geoms, lambda part: _nearest_level(part, water_polys), frame,
                0.0, "water", (0.16, 0.45, 0.70),
                sink_mm=spec.water_mm + 0.05)

        o = None
        if spec.water_style == "polygon":
            o = polygon_water()
            if o is not None and not mesh.edge_report(o)["closed"]:
                warnings.append(
                    "the water outline was too tangled to build cleanly from "
                    "its polygon, so it was rasterised to the mesh grid — the "
                    "shoreline will look stepped")
                o = None
        if o is None:
            o = grid_water()
        if o:
            water_parts.append(o)

    if flowing:
        # Streams keep the shape of the ground: the slab is draped rather than
        # levelled, sitting in the shallow channel cut for it.
        drape = mesh.bilinear_sampler(surface, xs, ys)
        geoms = [g for g in (_simplify_area(g, frame, spec.nozzle_mm)
                             for g, _m, _s in flowing) if g is not None]
        # Streams meet each other at confluences as well as meeting the flat
        # bodies, and either contact welds two slabs onto one vertical edge.
        geoms = _unpinch(_clear_of([q for g in geoms for q in _polys_of(g)],
                                   [g for g, _m, _l in water_polys], frame),
                         frame)
        o = mesh.extrude(geoms, lambda p: None, frame, 0.0, "water",
                         (0.16, 0.45, 0.70), sink_mm=spec.water_mm + 0.05,
                         sample=drape,
                         densify_m=max(1.0, (spec.width_m / max(1, nx - 1)) * 0.5))
        if o:
            water_parts.append(o)
        steep = max(s for _g, _m, s in flowing)
        water_info.append({"what": "flowing", "count": len(flowing),
                           "level_m": f"draped (to {steep:.2f} slope)",
                           "share": 0.0})

    merged_water = mesh.merge(water_parts, "water", (0.16, 0.45, 0.70))
    if merged_water:
        objects.append(merged_water)

    cell_mm = (spec.width_m / max(1, nx - 1)) * frame.scale
    if cell_mm < spec.nozzle_mm:
        warnings.append(
            f"mesh cells are {cell_mm:.2f} mm but the nozzle is "
            f"{spec.nozzle_mm:g} mm — the extra detail cannot print")

    def ground_under(poly):
        mnx, mny, mxx, mxy = poly.bounds
        i0 = max(0, int(np.searchsorted(xs, mnx)) - 1)
        i1 = min(nx, int(np.searchsorted(xs, mxx)) + 1)
        j0 = max(0, int(np.searchsorted(ys, mny)) - 1)
        j1 = min(ny, int(np.searchsorted(ys, mxy)) + 1)
        sub = Z[j0:max(j1, j0 + 1), i0:max(i1, i0 + 1)]
        return float(sub.min()) if sub.size else None

    min_area_mm2 = (2 * spec.nozzle_mm) ** 2
    cell_m = spec.width_m / max(1, nx - 1)
    if spec.mesh_buildings:
        polys, dropped = [], 0
        for f in c["osm"]:
            if f.get("layer") != "buildings" or f.get("poly") is None:
                continue
            if f["poly"].area * frame.scale ** 2 < min_area_mm2:
                dropped += 1
                continue
            polys.append(f["poly"])
        # Terraces share walls, and two prisms meeting on a wall weld into an
        # edge with four faces — non-manifold. Unioning first removes the
        # internal walls; a terrace is one solid lump when printed anyway.
        merged = _print_clean(_union(polys), frame, spec.nozzle_mm)
        if merged is not None:
            o = mesh.extrude(
                [merged], ground_under, frame, spec.buildings_mm,
                "buildings", (0.78, 0.34, 0.24),
                sample=mesh.bilinear_sampler(Z, xs, ys),
                densify_m=max(1.0, cell_m * 0.5), flat_top=True)
            if o:
                objects.append(o)
        if dropped:
            warnings.append(f"{dropped} buildings are smaller than the nozzle "
                            "can print and were left out")

    if spec.mesh_roads:
        bufs = []
        min_w = 2 * spec.nozzle_mm / frame.scale        # ground metres
        for f in c["osm"]:
            if f.get("layer") not in ("roads", "paths", "railways"):
                continue
            pts = f.get("pts")
            if pts is None or len(pts) < 2:
                continue
            w = max(ROAD_WIDTH_M.get(f.get("kind") or "", 5.0), min_w)
            bufs.append(shapely.linestrings(pts).buffer(w / 2, cap_style=2))
        merged = _print_clean(_union(bufs), frame, spec.nozzle_mm)
        if merged is not None:
            # Roads run across hillsides, so they are draped: every vertex
            # takes its own ground height. The boundary is densified to about
            # half a mesh cell first, or the slab spans the terrain instead of
            # following it.
            o = mesh.extrude(
                [merged], ground_under, frame, spec.roads_mm, "roads",
                (0.25, 0.25, 0.28), sink_mm=0.3,
                sample=mesh.bilinear_sampler(Z, xs, ys),
                densify_m=max(1.0, cell_m * 0.5))
            if o:
                objects.append(o)

    if spec.mesh_trip:
        # The trip is the same draped extrusion as a road, but its width is
        # given on the model rather than on the ground: it is a drawn route,
        # not a real object, so it should look the same at any capture scale.
        want = set(spec.trip_ids)
        w = max(spec.trip_w_mm, 2 * spec.nozzle_mm) / frame.scale
        bufs = []
        for f in c["osm"]:
            if f.get("id") not in want:
                continue
            pts = f.get("pts")          # numpy: test for None, never truthiness
            if pts is None or len(pts) < 2:
                continue
            bufs.append(shapely.linestrings(pts).buffer(w / 2, cap_style=2))
        if not bufs:
            warnings.append("no trip is selected, so nothing was raised for it "
                            "— pick a route in the SVG preview first")
        else:
            merged = _print_clean(_union(bufs), frame, spec.nozzle_mm)
            if merged is not None:
                o = mesh.extrude(
                    [merged], ground_under, frame, spec.trip_mm, "trip",
                    (0.85, 0.36, 0.22), sink_mm=0.3,
                    sample=mesh.bilinear_sampler(Z, xs, ys),
                    densify_m=max(1.0, cell_m * 0.5))
                if o:
                    objects.append(o)

    # Repair any holes left in the extruded parts before reporting.
    repaired = 0
    for o in objects:
        if not mesh.edge_report(o)["closed"]:
            repaired += mesh.close_holes(o)
    if repaired:
        warnings.append(f"filled {repaired} small holes left by the "
                        "triangulation")

    timings["mesh"] = round(time.time() - t_m, 2)
    reports = {o.name: mesh.edge_report(o) for o in objects}
    stats = {
        "grid": [int(nx), int(ny)],
        "cell_mm": round(cell_mm, 3),
        "size_mm": [round(spec.model_w_mm, 1),
                    round(spec.model_h_mm or
                          spec.model_w_mm * region.height_m / region.width_m, 1),
                    round(float(max(o.verts[:, 2].max() for o in objects)), 1)],
        "objects": [{"name": o.name, "triangles": int(o.triangles),
                     "colour": list(o.colour),
                     "closed": reports[o.name]["closed"]} for o in objects],
        "triangles": int(sum(o.triangles for o in objects)),
        "watertight": all(r["closed"] for r in reports.values()),
        "mesh_check": {k: {"boundary": v["boundary"],
                           "nonmanifold": v["nonmanifold"],
                           "degenerate": v["degenerate"]}
                       for k, v in reports.items()},
        "water": water_info,
        "exaggeration": round(exag, 3),
        "exaggeration_asked": spec.z_exaggeration,
        "backplate": ([spec.backplate_w_mm, spec.backplate_h_mm, plate_t]
                      if spec.backplate else None),
    }
    return objects, stats


async def build(spec: Spec, interactive: bool) -> dict:
    return render_svg(spec, await collect(spec), interactive)


@app.post("/api/generate")
async def api_generate(spec: Spec):
    return await build(spec, interactive=True)


@app.post("/api/export")
async def api_export(spec: Spec):
    out = await build(spec, interactive=False)
    name = f"contours_{spec.lat:.4f}_{spec.lon:.4f}_{int(spec.width_m)}m.svg"
    EXPORTS.mkdir(exist_ok=True)
    (EXPORTS / name).write_text(out["svg"], encoding="utf-8")
    return Response(
        out["svg"], media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "X-Saved-To": str(EXPORTS / name)})


@app.post("/api/mesh")
async def api_mesh(spec: Spec):
    """Indexed geometry for the browser viewer — the same mesh that exports."""
    c = await collect(spec)
    objects, stats = render_mesh(spec, c)
    stats.update(warnings=c["warnings"], timings=c["timings"],
                 dem=c["dem_info"], elevation=c["elevation"],
                 osm=c["osm_info"], seconds=round(time.time() - c["t0"], 2),
                 cache_mb=round(cache_size() / 1e6, 1))
    return Response(mesh.write_viewer(objects, stats),
                    media_type="application/octet-stream")


@app.post("/api/mesh/export")
async def api_mesh_export(spec: Spec, format: str = "stl"):
    c = await collect(spec)
    objects, stats = render_mesh(spec, c)
    stem = f"terrain_{spec.lat:.4f}_{spec.lon:.4f}_{int(spec.width_m)}m"
    EXPORTS.mkdir(exist_ok=True)

    if format == "stl":
        data = mesh.write_stl(objects)
        name = f"{stem}.stl"
        (EXPORTS / name).write_bytes(data)
        return Response(data, media_type="model/stl", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Triangles": str(stats["triangles"])})

    if format == "obj":
        obj, mtl = mesh.write_obj(objects, f"{stem}.mtl")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{stem}.obj", obj)
            z.writestr(f"{stem}.mtl", mtl)
        data = buf.getvalue()
        name = f"{stem}.zip"
        (EXPORTS / name).write_bytes(data)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Triangles": str(stats["triangles"])})

    if format == "3mf":
        data = mesh.write_3mf(objects)
        name = f"{stem}.3mf"
        (EXPORTS / name).write_bytes(data)
        return Response(data, media_type="model/3mf", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Triangles": str(stats["triangles"])})

    raise HTTPException(400,
                        f"unknown mesh format {format!r} (stl, obj or 3mf)")


class InstallSpec(BaseModel):
    extract_id: str
    layers: list[str] | None = None          # None = everything
    clip_km: float | None = None             # half-width around lat/lon
    lat: float | None = None
    lon: float | None = None
    keep_source: bool = False                # keep the .pbf for future re-clips
    extend: bool = False                     # widen an existing store's clip


@app.get("/api/osmdata")
async def api_osmdata(lat: float, lon: float,
                      width_m: float = 5000, height_m: float = 5000,
                      candidates: bool = False):
    """What local OSM data exists, and what could cover this area.

    Looking up candidates means reaching Geofabrik and sizing each extract, so
    it only happens when we are actually about to offer a download."""
    r = Region(lat, lon, width_m, height_m)
    covering = localosm.find_store(r)
    importing = [{"extract": k, "job": v,
                  "phase": localosm.jobs.get(v, {}).get("phase"),
                  "eta_s": localosm.jobs.get(v, {}).get("eta_s"),
                  "extending": localosm.jobs.get(v, {}).get("extending")}
                 for k, v in localosm.active.items()]
    out = {"installed": localosm.installed(), "covering": covering,
           "extendable": None if covering else localosm.extendable_store(r),
           "importing": importing, "candidates": []}
    if covering is None and candidates:
        try:
            out["candidates"] = await localosm.candidates(r)
        except Exception as exc:                       # noqa: BLE001
            out["error"] = f"could not reach the Geofabrik index: {exc}"
    return out


@app.post("/api/osmdata/install")
async def api_osmdata_install(body: InstallSpec):
    try:
        extract = localosm.safe_id(body.extract_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    clip = None
    if body.clip_km and body.lat is not None and body.lon is not None:
        # a degree box big enough to contain the requested radius at this latitude
        dlat = body.clip_km / 111.32
        dlon = body.clip_km / max(1e-6, 111.32 * math.cos(math.radians(body.lat)))
        clip = [body.lon - dlon, body.lat - dlat,
                body.lon + dlon, body.lat + dlat]
    layers = set(body.layers) if body.layers else None

    if body.extend:
        # Extending must never take away what the store already has: widen the
        # clip to cover both areas, and keep every layer it was imported with.
        prev = next((i for i in localosm.installed() if i["id"] == extract), None)
        if prev is None:
            raise HTTPException(404, "nothing installed to extend")
        clip = localosm.union_bbox(prev.get("bbox"), clip)
        if prev.get("layers") is None or layers is None:
            layers = None                    # one side wanted everything
        else:
            layers |= set(prev["layers"])

    job_id = uuid4().hex[:12]
    # claim before the task starts: two POSTs would otherwise both begin, share
    # one temp store, and the first to finish would rename the other's
    # half-written file into place
    busy = localosm.claim(extract, job_id)
    if busy is not None:
        prev = localosm.jobs.get(busy, {})
        raise HTTPException(409, {
            "error": f"{extract} is already being imported",
            "job": busy, "phase": prev.get("phase"),
            "eta_s": prev.get("eta_s")})
    localosm.jobs[job_id] = {"phase": "queued", "done": False,
                             "extract": extract,
                             "extending": bool(body.extend)}
    asyncio.create_task(localosm.install(
        extract, job_id, layers, tuple(clip) if clip else None,
        keep_source=body.keep_source))
    return {"job": job_id}


@app.get("/api/osmdata/job/{job_id}")
async def api_osmdata_job(job_id: str):
    job = localosm.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


@app.delete("/api/osmdata/{extract_id}")
async def api_osmdata_delete(extract_id: str):
    try:
        p = localosm.store_path(extract_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not p.exists():
        raise HTTPException(404, "not installed")
    freed = p.stat().st_size
    p.unlink()
    src = localosm.source_pbf(extract_id)
    if src.exists():
        freed += src.stat().st_size
        src.unlink()
    return {"removed": extract_id, "freed_mb": round(freed / 1e6, 1)}


@app.delete("/api/osmdata/{extract_id}/source")
async def api_osmdata_drop_source(extract_id: str):
    """Free a kept .pbf without losing the imported store."""
    try:
        src = localosm.source_pbf(extract_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not src.exists():
        raise HTTPException(404, "no source file kept")
    freed = src.stat().st_size
    src.unlink()
    return {"removed": extract_id, "freed_mb": round(freed / 1e6, 1)}


@app.get("/api/cache")
async def api_cache():
    return {"cache_mb": round(cache_size() / 1e6, 1)}


@app.delete("/api/cache")
async def api_cache_clear():
    return cache_clear()


@app.get("/api/search")
async def api_search(q: str, limit: int = 8):
    async with httpx.AsyncClient(follow_redirects=True) as c:
        r = await c.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": limit},
            headers={"User-Agent": "cartofab/0.1 (local plotter tool)"},
            timeout=30.0)
    if r.status_code != 200:
        raise HTTPException(502, "geocoder unavailable")
    return [{"name": d.get("display_name"), "lat": float(d["lat"]),
             "lon": float(d["lon"]), "type": d.get("type"),
             "bbox": [float(v) for v in d.get("boundingbox", [])]}
            for d in r.json()]


@app.get("/api/coverage")
async def api_coverage(lat: float, lon: float, width_m: float = 5000,
                       height_m: float = 5000):
    r = Region(lat, lon, width_m, height_m)
    # Territory alone overstates Scotland — it promises 2 m LiDAR wherever the
    # polygon says "Scotland", including places none was ever flown. The probe
    # reads the real (disk-cached) tile listings instead.
    try:
        best, probed = await best_resolution(r)
    except Exception:                                  # noqa: BLE001
        keys = available(r)
        return {"sources": [{"key": k, "label": SOURCE_LABELS[k]} for k in keys],
                "best_resolution_m": min((SOURCE_RES[k] for k in keys),
                                         default=30.0)}
    return {"sources": probed, "best_resolution_m": best,
            "partial": [p for p in probed
                        if not p["available"] and p.get("share")]}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


class _FreshStatic(StaticFiles):
    """Serve the UI with revalidation rather than blind caching.

    Browsers otherwise hold on to a cached style.css or app.js across an
    update, and the app comes back subtly wrong — a reordered panel with the
    old stylesheet, say — with nothing to suggest why. ETags still make the
    revalidation cheap."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", _FreshStatic(directory=WEB), name="static")
EXPORTS.mkdir(exist_ok=True)
app.mount("/exports", StaticFiles(directory=EXPORTS), name="exports")
