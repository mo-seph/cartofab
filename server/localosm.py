"""Local OpenStreetMap extracts, for when Overpass will not play.

Public Overpass instances are frequently busy, and a busy instance is worst
exactly when you are exploring a new area — the cache cannot help you there.
This module lets a region be downloaded once from Geofabrik and queried
locally forever after, with byte-identical tags and geometry.

The download is deliberately a conscious act: it is hundreds of megabytes, so
nothing here happens without the user asking for it.

The `.osm.pbf` is imported into a small SQLite store (geometry as WKB, indexed
by an R*Tree) and then deleted — querying the pbf directly would mean rereading
hundreds of MB per request, and the store is a fraction of the size.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx
import numpy as np
import shapely
from shapely.geometry import box

from .geo import Region
from .osm import AREA_LAYERS, LAYER_QUERIES, _classify, _num

DATA_DIR = Path(__file__).parent.parent / "osmdata"
INDEX_URL = "https://download.geofabrik.de/index-v1.json"
INDEX_TTL = 14 * 24 * 3600
UA = {"User-Agent": "topofab/0.1 (local plotter tool)"}

# Only these come from ways; the rest are areas or nodes. Without this a way
# tagged place=island would be filed as a "places" line.
LINE_LAYERS = {"roads", "paths", "rivers", "coastline", "railways"}
NODE_LAYERS = {"peaks", "places"}
TAG_KEYS = ("highway", "building", "natural", "waterway",
            "landuse", "railway", "place")

jobs: dict[str, dict] = {}


# ---------------------------------------------------------------- catalogue

def _index_path() -> Path:
    return DATA_DIR / "geofabrik-index.json"


async def catalogue(force: bool = False) -> list[dict]:
    """Geofabrik's extract index, cached on disk."""
    p = _index_path()
    if not force and p.exists() and time.time() - p.stat().st_mtime < INDEX_TTL:
        return json.loads(p.read_text())["features"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, headers=UA) as c:
        r = await c.get(INDEX_URL, timeout=120.0)
        r.raise_for_status()
    p.write_text(r.text)
    return r.json()["features"]


async def size_of(url: str) -> int | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=UA) as c:
            r = await c.get(url, headers={"Range": "bytes=0-0"}, timeout=60.0)
        cr = r.headers.get("content-range", "")
        return int(cr.rsplit("/", 1)[-1]) if "/" in cr else None
    except httpx.HTTPError:
        return None


async def candidates(region: Region, limit: int = 3) -> list[dict]:
    """Smallest extracts that fully contain the region, smallest first."""
    feats = await catalogue()
    w, s, e, n = region.bbox_wgs(0.05)
    want = box(w, s, e, n)
    hits = []
    for f in feats:
        try:
            g = shapely.from_geojson(json.dumps(f["geometry"]))
        except Exception:                              # noqa: BLE001
            continue
        if g is None or g.is_empty or not g.contains(want):
            continue
        p = f["properties"]
        url = (p.get("urls") or {}).get("pbf")
        if url:
            hits.append({"id": p["id"], "name": p["name"], "url": url,
                         "extent": float(g.area)})
    hits.sort(key=lambda h: h["extent"])
    hits = hits[:limit]
    sizes = await asyncio.gather(*(size_of(h["url"]) for h in hits))
    for h, sz in zip(hits, sizes):
        h["bytes"] = sz
        h["mb"] = round(sz / 1e6, 1) if sz else None
        h.pop("extent", None)
    return hits


# -------------------------------------------------------------------- store

def safe_id(extract_id: str) -> str:
    """Extract ids come in over HTTP, so keep them to the Geofabrik alphabet."""
    clean = "".join(c for c in extract_id if c.isalnum() or c in "-_")
    if not clean or clean != extract_id:
        raise ValueError(f"bad extract id {extract_id!r}")
    return clean


# One import per extract at a time. Two of them share a temp store and a node
# cache, so a second one started while the first is running destroys both: the
# loser renames the winner's half-written file into place. Nothing about that
# is recoverable afterwards, so refuse it at the door.
active: dict[str, str] = {}


def claim(extract_id: str, job_id: str) -> str | None:
    """Reserve this extract, or return the job id already importing it."""
    busy = active.get(extract_id)
    if busy is not None:
        return busy
    active[extract_id] = job_id
    return None


def release(extract_id: str) -> None:
    active.pop(extract_id, None)


def source_pbf(extract_id: str) -> Path:
    """Where a kept .pbf lives, so an extract can be re-clipped without
    downloading it again."""
    return DATA_DIR / f"{safe_id(extract_id)}.osm.pbf"


def store_path(extract_id: str) -> Path:
    return DATA_DIR / f"{safe_id(extract_id)}.sqlite"


def installed() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    out = []
    for p in sorted(DATA_DIR.glob("*.sqlite")):
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
            n = con.execute("SELECT count(*) FROM feat").fetchone()[0]
            con.close()
        except sqlite3.Error:
            continue
        out.append({"id": p.stem, "name": meta.get("name", p.stem),
                    "features": n, "mb": round(p.stat().st_size / 1e6, 1),
                    "bbox": json.loads(meta.get("bbox", "null")),
                    "layers": json.loads(meta.get("layers", "null")),
                    "full_bbox": json.loads(meta.get("full_bbox", "null")),
                    "clipped": meta.get("clipped") == "1",
                    "has_source": source_pbf(p.stem).exists(),
                    "imported": meta.get("imported")})
    return out


def find_store(region: Region) -> dict | None:
    """An installed store whose extract covers this region."""
    w, s, e, n = region.bbox_wgs(0)
    for item in installed():
        b = item.get("bbox")
        if not b:
            continue
        if b[0] <= w and b[1] <= s and b[2] >= e and b[3] >= n:
            return item
    return None


def extendable_store(region: Region) -> dict | None:
    """An installed store that *could* cover this region but doesn't.

    Extracts are clipped on import to keep them small, so a country-sized
    download can still miss a town 40 km away. That is not the same as having
    no data for the area, and the difference matters: re-clipping a kept
    source file needs no download at all."""
    w, s_, e, n = region.bbox_wgs(0)
    best = None
    for item in installed():
        b, fb = item.get("bbox"), item.get("full_bbox")
        if not fb or not item.get("clipped"):
            continue
        if b and b[0] <= w and b[1] <= s_ and b[2] >= e and b[3] >= n:
            return None                      # already covered; nothing to do
        if fb[0] <= w and fb[1] <= s_ and fb[2] >= e and fb[3] >= n:
            # prefer one whose source is still on disk — that extend is free
            if best is None or (item["has_source"] and not best["has_source"]):
                best = item
    return best


def union_bbox(a, b):
    """Smallest box holding both, so extending never loses what is there."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


# ------------------------------------------------------------------- import

# Above this size the node-location index goes to disk rather than RAM. A
# country-sized extract holds tens of millions of node positions and an
# in-memory index for one would be a gigabyte or more.
DISK_INDEX_ABOVE = 80 * 1024 * 1024


# Counting the tagged objects without building locations or assembling areas
# is cheap — about 4% of a full import — and the ratio between that count and
# what the real pass yields is remarkably steady: 1.519 for Andorra, 1.536 for
# Luxembourg. Estimating from file size instead is up to 82% out, because the
# tagged fraction varies hugely between a city and a mountain range.
AREA_EXPANSION = 1.53


def count_objects(pbf: Path) -> int:
    """Roughly how many objects the import will walk, for the progress bar."""
    import osmium
    import osmium.filter as F
    n = 0
    for _ in (osmium.FileProcessor(
                str(pbf), osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION)
              .with_filter(F.KeyFilter(*TAG_KEYS))):
        n += 1
    return int(n * AREA_EXPANSION)


def import_pbf(pbf: Path, extract_id: str, name: str,
               bbox: list[float], progress=None,
               layers: set[str] | None = None,
               clip: tuple | None = None) -> Path:
    """One pass over the extract, writing the layers we draw into SQLite."""
    import osmium
    import osmium.filter as F

    dst = store_path(extract_id)
    tmp = dst.with_suffix(".building")
    # an import killed part-way (a crash, or the server reloading under it)
    # leaves both of these behind, and the node cache is the larger by far
    tmp.unlink(missing_ok=True)
    (DATA_DIR / f"{extract_id}.nodes.tmp").unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, "
                "osmid TEXT, name TEXT, kind TEXT, ele REAL, wkb BLOB)")
    con.execute("CREATE VIRTUAL TABLE feat_idx USING "
                "rtree(id, minx, maxx, miny, maxy)")

    wkbf = osmium.geom.WKBFactory()
    want = set(layers) & set(LAYER_QUERIES) if layers else set(LAYER_QUERIES)
    rows, idx, next_id, seen = [], [], 1, 0
    kept_out = 0

    def flush():
        con.executemany("INSERT INTO feat VALUES (?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO feat_idx VALUES (?,?,?,?,?)", idx)
        rows.clear()
        idx.clear()

    big = pbf.stat().st_size > DISK_INDEX_ABOVE
    node_cache = DATA_DIR / f"{extract_id}.nodes.tmp"
    storage = f"sparse_file_array,{node_cache}" if big else "flex_mem"
    fp = (osmium.FileProcessor(str(pbf),
                               osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION)
          .with_locations(storage)
          .with_areas()
          .with_filter(F.KeyFilter(*TAG_KEYS)))

    for obj in fp:
        seen += 1
        if progress and seen % 5_000 == 0:
            progress(seen, len(rows) + next_id - 1)
        tags = dict(obj.tags)
        layer = _classify(tags, want)
        if layer is None:
            continue
        kind = type(obj).__name__
        try:
            if kind == "Area" and layer in AREA_LAYERS:
                blob = bytes.fromhex(wkbf.create_multipolygon(obj))
                osmid = f"a{obj.orig_id()}"
            elif kind == "Way" and layer in LINE_LAYERS:
                blob = bytes.fromhex(wkbf.create_linestring(obj))
                osmid = f"w{obj.id}"
            elif kind == "Node" and layer in NODE_LAYERS:
                blob = bytes.fromhex(wkbf.create_point(obj))
                osmid = f"n{obj.id}"
            else:
                continue
        except Exception:                              # noqa: BLE001
            continue
        g = shapely.from_wkb(blob)
        if g is None or g.is_empty:
            continue
        mnx, mny, mxx, mxy = g.bounds
        if clip is not None and (mxx < clip[0] or mnx > clip[2]
                                 or mxy < clip[1] or mny > clip[3]):
            kept_out += 1
            continue
        rows.append((next_id, layer, osmid, tags.get("name"),
                     tags.get("highway") or tags.get("waterway")
                     or tags.get("natural") or tags.get("railway")
                     or tags.get("landuse") or tags.get("place"),
                     _num(tags.get("ele")), blob))
        idx.append((next_id, mnx, mxx, mny, mxy))
        next_id += 1
        if len(rows) >= 20_000:
            flush()
    flush()

    con.execute("CREATE INDEX feat_layer ON feat(layer)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("id", extract_id), ("name", name),
        ("bbox", json.dumps(list(clip) if clip else bbox)),
        ("full_bbox", json.dumps(bbox)),
        ("layers", json.dumps(sorted(want))),
        ("clipped", "1" if clip else "0"),
        ("imported", time.strftime("%Y-%m-%d %H:%M")),
    ])
    con.commit()
    con.execute("VACUUM")
    con.close()
    node_cache.unlink(missing_ok=True)
    tmp.replace(dst)
    return dst


# -------------------------------------------------------------------- query

def query(extract_id: str, region: Region, layers: set[str], *,
          simplify_m: float = 1.0) -> list[dict]:
    """Same feature shape as osm.fetch, straight out of the local store."""
    layers = {l for l in layers if l in LAYER_QUERIES}
    if not layers:
        return []
    p = store_path(extract_id)
    if not p.exists():
        return []
    w, s, e, n = region.bbox_wgs(0.05)
    hw, hh = region.width_m / 2, region.height_m / 2
    clip = box(-hw, -hh, hw, hh)

    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    q = (f"SELECT f.layer, f.osmid, f.name, f.kind, f.ele, f.wkb "
         f"FROM feat f JOIN feat_idx i ON i.id = f.id "
         f"WHERE f.layer IN ({','.join('?' * len(layers))}) "
         f"AND i.maxx >= ? AND i.minx <= ? AND i.maxy >= ? AND i.miny <= ?")
    cur = con.execute(q, (*sorted(layers), w, e, s, n))

    out: list[dict] = []
    for layer, osmid, name, kind, ele, blob in cur:
        g = shapely.from_wkb(blob)
        if g is None or g.is_empty:
            continue
        g = shapely.transform(
            g, lambda c: np.column_stack(region.wgs_to_page(c[:, 0], c[:, 1])))
        if g.geom_type == "Point":
            if not clip.contains(g):
                continue
            out.append({"id": osmid, "layer": layer, "name": name,
                        "kind": kind, "ele": ele,
                        "point": (float(g.x), float(g.y))})
            continue
        if layer in AREA_LAYERS:
            g = g.buffer(0).intersection(clip)
            if not g.is_empty:
                out.append({"id": osmid, "layer": layer, "name": name,
                            "kind": kind, "poly": g})
            continue
        g = g.intersection(clip)
        if simplify_m > 0:
            g = shapely.simplify(g, simplify_m, preserve_topology=False)
        for k, part in enumerate(_explode(g)):
            out.append({"id": f"{osmid}{f'-{k}' if k else ''}", "layer": layer,
                        "name": name, "kind": kind, "closed":
                        bool(np.allclose(part[0], part[-1])), "pts": part})
    con.close()
    return out


def _explode(geom) -> list[np.ndarray]:
    if geom is None or geom.is_empty:
        return []
    res = []
    for g in getattr(geom, "geoms", [geom]):
        if g.is_empty:
            continue
        if g.geom_type == "LineString":
            a = np.asarray(g.coords, dtype=np.float64)
            if len(a) >= 2:
                res.append(a)
        elif g.geom_type in ("MultiLineString", "GeometryCollection"):
            res.extend(_explode(g))
    return res


# ------------------------------------------------------------------ install

async def install(extract_id: str, job_id: str,
                  layers: set[str] | None = None,
                  clip: tuple | None = None,
                  keep_source: bool = False) -> None:
    """Download an extract and import it, reporting progress into `jobs`.

    `layers` and `clip` keep the store to what will actually be drawn: a whole
    dense region imported in full is several times the size of its .pbf and
    takes minutes, and most of that is usually buildings far from the area
    being worked on."""
    job = jobs[job_id]
    try:
        feats = await catalogue()
        f = next((x for x in feats if x["properties"]["id"] == extract_id), None)
        if f is None:
            raise RuntimeError(f"unknown extract {extract_id!r}")
        url = f["properties"]["urls"]["pbf"]
        name = f["properties"]["name"]
        g = shapely.from_geojson(json.dumps(f["geometry"]))
        bbox = list(g.bounds)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pbf = source_pbf(extract_id)
        if pbf.exists():
            # kept from a previous install: re-clipping costs no download
            got = pbf.stat().st_size
            job.update(name=name, total=got, reused_source=True)
        else:
            job.update(phase="downloading", name=name)
            part = pbf.with_suffix(".part")
            async with httpx.AsyncClient(follow_redirects=True, headers=UA) as c:
                async with c.stream("GET", url, timeout=None) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    job["total"] = total
                    got = 0
                    with part.open("wb") as fh:
                        async for chunk in r.aiter_bytes(1 << 20):
                            fh.write(chunk)
                            got += len(chunk)
                            job["downloaded"] = got
            if total and got != total:
                raise RuntimeError(f"download truncated: {got} of {total} bytes")
            # only now is it a complete file — a half-download left under the
            # real name would be silently reused as a source next time
            part.rename(pbf)
        job.update(phase="counting", downloaded=got,
                   pbf_mb=round(got / 1e6, 1))
        est = await asyncio.to_thread(count_objects, pbf)
        job.update(phase="importing", scanned=0, kept=0,
                   total_objects_est=est, started=time.time())

        def prog(seen, kept):
            job["scanned"] = seen
            job["kept"] = kept
            el = time.time() - job["started"]
            if el > 2 and seen:
                rate = seen / el
                job["rate"] = int(rate)
                job["eta_s"] = max(0, int((est - seen) / rate)) if est > seen else 0

        dst = await asyncio.to_thread(import_pbf, pbf, extract_id, name, bbox,
                                      prog, layers, clip)
        if not keep_source:
            pbf.unlink(missing_ok=True)      # the store is what we query
        job.update(phase="done", done=True,
                   store_mb=round(dst.stat().st_size / 1e6, 1))
    except Exception as exc:                           # noqa: BLE001
        # do not leave a part-file or an unwanted source behind
        source_pbf(extract_id).with_suffix(".part").unlink(missing_ok=True)
        if not keep_source:
            source_pbf(extract_id).unlink(missing_ok=True)
        job.update(phase="failed", done=True, error=str(exc))
    finally:
        release(extract_id)
