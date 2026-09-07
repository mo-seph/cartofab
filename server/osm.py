"""OpenStreetMap vector layers via Overpass.

One request fetches everything the user asked for; features are classified into
layers from their tags afterwards. Feature ids are derived from OSM way ids so
that a trip selection survives re-generating the map.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import httpx
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box
from shapely.ops import linemerge, polygonize, unary_union

from .cache import cache_get, cache_put
from .geo import Region

# Public Overpass instances are frequently busy; we rotate through them and
# take a second pass after a short backoff before giving up. Contours never
# depend on this, so a total failure degrades to "no vector layers".
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
RETRYABLE = {429, 502, 503, 504}

# A mirror can answer 200 with well-formed JSON that is nonetheless worthless:
# an empty result from a mirror running an empty or half-loaded database. The
# giveaway is osm3s.timestamp_osm_base, which must be a real ISO date. Trusting
# those responses is worse than a hard failure, because they get cached.
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?$")


def _payload_problem(data: object) -> str | None:
    if not isinstance(data, dict) or "elements" not in data:
        return "malformed payload"
    remark = str(data.get("remark") or "")
    if "error" in remark.lower():
        return remark[:120]
    ts = str((data.get("osm3s") or {}).get("timestamp_osm_base", ""))
    if not _TS.match(ts):
        return f"database not loaded (timestamp {ts!r})"
    return None
UA = {"User-Agent": "topofab/0.1 (local plotter tool)"}
OVERPASS_TIMEOUT = 25.0

# Layers whose features describe an area rather than a route. These get
# assembled into real polygons so they can be hatched, and can mask what lies
# underneath them.
AREA_LAYERS = {"water", "glaciers", "buildings"}

ROADS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential"
         "|living_street|service|track|motorway_link|trunk_link|primary_link"
         "|secondary_link|tertiary_link")
PATHS = "path|footway|bridleway|cycleway|steps|via_ferrata"

# layer -> (overpass statements, z-order hint)
LAYER_QUERIES: dict[str, list[str]] = {
    "roads":     [f'way["highway"~"^({ROADS})$"]'],
    "paths":     [f'way["highway"~"^({PATHS})$"]'],
    "rivers":    ['way["waterway"~"^(river|stream|canal)$"]'],
    "water":     ['way["natural"="water"]', 'relation["natural"="water"]',
                  'way["landuse"="reservoir"]', 'relation["landuse"="reservoir"]'],
    "coastline": ['way["natural"="coastline"]'],
    "glaciers":  ['way["natural"="glacier"]', 'relation["natural"="glacier"]'],
    "railways":  ['way["railway"~"^(rail|narrow_gauge|funicular|light_rail)$"]'],
    "peaks":     ['node["natural"~"^(peak|volcano|saddle)$"]'],
    "places":    ['node["place"~"^(city|town|village|hamlet|locality)$"]'],
    "buildings": ['way["building"]', 'relation["building"]'],
}


def _classify(tags: dict, want: set[str]) -> str | None:
    h = tags.get("highway", "")
    if "roads" in want and re.fullmatch(ROADS, h):
        return "roads"
    if "paths" in want and re.fullmatch(PATHS, h):
        return "paths"
    if "coastline" in want and tags.get("natural") == "coastline":
        return "coastline"
    if "rivers" in want and tags.get("waterway") in ("river", "stream", "canal"):
        return "rivers"
    if "water" in want and (tags.get("natural") == "water"
                            or tags.get("landuse") == "reservoir"):
        return "water"
    if "glaciers" in want and tags.get("natural") == "glacier":
        return "glaciers"
    if "railways" in want and tags.get("railway") in (
            "rail", "narrow_gauge", "funicular", "light_rail"):
        return "railways"
    if "buildings" in want and "building" in tags:
        return "buildings"
    if "peaks" in want and tags.get("natural") in ("peak", "volcano", "saddle"):
        return "peaks"
    if "places" in want and "place" in tags:
        return "places"
    return None


def build_query(region: Region, layers: set[str], timeout: int = 60) -> str:
    """Round the bbox so that nudging the centre by a metre still hits cache."""
    w, s, e, n = region.bbox_wgs(0.05)
    bbox = (f"({round(s, 4):.4f},{round(w, 4):.4f},"
            f"{round(n, 4):.4f},{round(e, 4):.4f})")
    stmts = []
    for layer in sorted(layers):
        for q in LAYER_QUERIES.get(layer, []):
            stmts.append(f"  {q}{bbox};")
    return (f"[out:json][timeout:{timeout}];\n(\n"
            + "\n".join(stmts) + "\n);\nout geom;")


_gate = asyncio.Semaphore(2)      # be polite to the public instances

# Public mirrors fail in a very particular way: a dead one answers 502 almost
# instantly, every time, and there is no point spending the budget on it again
# a second later. Remember recent hard failures and deprioritise those hosts.
MIRROR_COOLDOWN = 300.0
_mirror_bad: dict[str, float] = {}

# A query that failed everywhere will fail again if retried immediately, and
# re-waiting the whole budget on every Generate is the single most annoying
# thing this can do. Remember the failure briefly.
FAIL_TTL = 90.0
_query_failed: dict[str, tuple[float, str]] = {}


def _endpoints_by_health() -> list[str]:
    now = asyncio.get_event_loop().time()
    fresh = [u for u in ENDPOINTS if now - _mirror_bad.get(u, -1e9) > MIRROR_COOLDOWN]
    return fresh or list(ENDPOINTS)      # all cold? try them anyway


async def _run_query(query: str) -> dict:
    key = "overpass:" + hashlib.sha1(query.encode()).hexdigest()
    blob = cache_get(key, ".json")
    if blob is not None:
        return json.loads(blob)

    now = asyncio.get_event_loop().time()
    recent = _query_failed.get(key)
    if recent and now - recent[0] < FAIL_TTL:
        raise RuntimeError(f"{recent[1]} (retrying in "
                           f"{FAIL_TTL - (now - recent[0]):.0f}s)")

    last = "no endpoint tried"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for attempt in range(2):
            for url in _endpoints_by_health():
                try:
                    async with _gate:
                        r = await client.post(url, data={"data": query},
                                              headers=UA,
                                              timeout=OVERPASS_TIMEOUT)
                except httpx.HTTPError as exc:
                    last = f"{_host(url)}: {type(exc).__name__}"
                    _mirror_bad[url] = asyncio.get_event_loop().time()
                    continue
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and ctype.startswith("application/json"):
                    try:
                        data = r.json()
                    except ValueError:
                        last = f"{_host(url)}: unparseable JSON"
                        continue
                    problem = _payload_problem(data)
                    if problem is None:
                        cache_put(key, ".json", r.content)
                        return data
                    last = f"{_host(url)}: {problem}"
                    continue
                last = f"{_host(url)}: HTTP {r.status_code}"
            if attempt == 0:
                await asyncio.sleep(2.0)
    msg = f"Overpass unavailable ({last})"
    _query_failed[key] = (asyncio.get_event_loop().time(), msg)
    raise RuntimeError(msg)


def _host(url: str) -> str:
    return url.split("/")[2]


def _element_polygon(el: dict, to_page) -> object | None:
    """Assemble an OSM way or multipolygon relation into a shapely polygon.

    Relation rings are frequently split across several member ways, so the
    members are line-merged and polygonized rather than taken one at a time."""
    def ring(geom):
        # 2 points is legitimate for a relation member: multipolygon rings are
        # split across member ways, and dropping a short one breaks the ring.
        if len(geom) < 2:
            return None
        lon = np.fromiter((g["lon"] for g in geom), dtype=np.float64)
        lat = np.fromiter((g["lat"] for g in geom), dtype=np.float64)
        x, y = to_page(lon, lat)
        return np.column_stack([x, y])

    if el.get("type") == "way":
        pts = ring(el.get("geometry") or [])
        if pts is None or len(pts) < 4:
            return None
        if not np.allclose(pts[0], pts[-1]):
            return None                     # an unclosed way is not an area
        try:
            return Polygon(pts).buffer(0)
        except Exception:                   # noqa: BLE001
            return None

    if el.get("type") == "relation":
        outer, inner = [], []
        for m in el.get("members", []):
            if m.get("type") != "way" or not m.get("geometry"):
                continue
            pts = ring(m["geometry"])
            if pts is None or len(pts) < 2:
                continue
            (inner if m.get("role") == "inner" else outer).append(LineString(pts))
        if not outer:
            return None
        try:
            outs = unary_union(list(polygonize(linemerge(outer))))
            if outs.is_empty:
                return None
            if inner:
                ins = unary_union(list(polygonize(linemerge(inner))))
                if not ins.is_empty:
                    outs = outs.difference(ins)
            return outs.buffer(0)
        except Exception:                   # noqa: BLE001
            return None
    return None


def _rings(el: dict) -> list[tuple[str, list[dict]]]:
    """Yield (suffix, geometry) for a way or a relation's members."""
    if el.get("type") == "way" and el.get("geometry"):
        return [("", el["geometry"])]
    if el.get("type") == "relation":
        out = []
        for i, m in enumerate(el.get("members", [])):
            if m.get("geometry") and m.get("type") == "way":
                out.append((f"-{i}", m["geometry"]))
        return out
    return []


async def fetch(region: Region, layers: set[str], *,
                simplify_m: float = 1.0,
                per_layer_timeout: float = 25.0) -> tuple[list[dict], list[str]]:
    """One Overpass query *per layer*, so toggling a layer in the UI reuses the
    cache for the others and one dead layer does not sink the rest.

    Each layer gets its own time budget rather than sharing a single one: with
    five layers selected a shared budget means each effectively gets a fifth of
    it, and they all fail together."""
    wanted = sorted(l for l in layers if l in LAYER_QUERIES)
    if not wanted:
        return [], []

    async def one(layer: str):
        data = await asyncio.wait_for(
            _run_query(build_query(region, {layer})), timeout=per_layer_timeout)
        return _parse(data, region, {layer}, simplify_m)

    results = await asyncio.gather(*(one(l) for l in wanted),
                                   return_exceptions=True)
    features: list[dict] = []
    warnings: list[str] = []
    for layer, res in zip(wanted, results):
        if isinstance(res, asyncio.TimeoutError):
            warnings.append(f"{layer}: Overpass took longer than "
                            f"{per_layer_timeout:.0f}s")
        elif isinstance(res, BaseException):
            warnings.append(f"{layer}: {res}")
        else:
            features.extend(res)
    return features, warnings


def _parse(data: dict, region: Region, layers: set[str],
           simplify_m: float) -> list[dict]:
    hw, hh = region.width_m / 2, region.height_m / 2
    clip = box(-hw, -hh, hw, hh)
    features: list[dict] = []

    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        layer = _classify(tags, layers)
        if layer is None:
            continue
        name = tags.get("name")

        if el.get("type") == "node":
            x, y = region.wgs_to_page(el["lon"], el["lat"])
            if not clip.contains(shapely.Point(x, y)):
                continue
            features.append({
                "id": f"n{el['id']}", "layer": layer, "name": name,
                "kind": tags.get("natural") or tags.get("place"),
                "ele": _num(tags.get("ele")),
                "point": (float(x), float(y)),
            })
            continue

        if layer in AREA_LAYERS:
            poly = _element_polygon(el, region.wgs_to_page)
            if poly is not None and not poly.is_empty:
                poly = poly.intersection(clip)
                if not poly.is_empty:
                    features.append({
                        "id": f"a{el['id']}", "layer": layer, "name": name,
                        "kind": tags.get("natural") or tags.get("landuse")
                                or tags.get("building"),
                        "poly": poly,
                    })
                    continue

        for suffix, geom in _rings(el):
            if len(geom) < 2:
                continue
            lon = np.fromiter((g["lon"] for g in geom), dtype=np.float64)
            lat = np.fromiter((g["lat"] for g in geom), dtype=np.float64)
            x, y = region.wgs_to_page(lon, lat)
            pts = np.column_stack([x, y])
            line = shapely.linestrings(pts)
            line = shapely.intersection(line, clip)
            if simplify_m > 0:
                line = shapely.simplify(line, simplify_m, preserve_topology=False)
            for k, part in enumerate(_explode(line)):
                if len(part) < 2:
                    continue
                features.append({
                    "id": f"w{el['id']}{suffix}" + (f"-{k}" if k else ""),
                    "layer": layer, "name": name,
                    "kind": tags.get("highway") or tags.get("waterway")
                            or tags.get("natural") or tags.get("railway"),
                    "closed": bool(np.allclose(part[0], part[-1])),
                    "pts": part,
                })
    return features


def _num(v):
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError):
        return None


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
