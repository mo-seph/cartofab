"""Water treatment: deriving the sea, hatching, and masking what lies beneath.

Lakes and sea arrive (or are derived) as shapely polygons in the page frame.
From there three things are possible, independently:

  * hatch them, so they read as water on a pen plotter rather than as outlines;
  * subtract them from the contour and river layers, so nothing is drawn
    underneath — the plotter would otherwise scribble lake-bed contours and
    run rivers straight across a loch;
  * both.
"""
from __future__ import annotations

import numpy as np
import shapely
from contourpy import FillType, contour_generator
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.ops import polygonize, unary_union


# ------------------------------------------------------------------- the sea

def sea_polygon(dem, level: float = 0.0):
    """Everything at or below `level` in the elevation grid, as a polygon.

    Uses filled contours rather than tracing the 0 m line, so islands come back
    as holes instead of separate rings that would then be hatched over."""
    z = np.flipud(dem.z)                    # contourpy wants ascending y
    if not np.isfinite(z).any():
        return None
    cg = contour_generator(
        x=dem.x_coords, y=dem.y_coords,
        z=np.ma.masked_invalid(z),
        fill_type=FillType.OuterOffset, chunk_size=0,
    )
    points, offsets = cg.filled(-1e6, level)
    polys = []
    for pts, offs in zip(points, offsets):
        if pts is None or len(pts) < 4:
            continue
        rings = [pts[offs[i]:offs[i + 1]] for i in range(len(offs) - 1)]
        rings = [r for r in rings if len(r) >= 4]
        if not rings:
            continue
        try:
            p = Polygon(rings[0], rings[1:]).buffer(0)
        except Exception:                    # noqa: BLE001
            continue
        if not p.is_empty:
            polys.append(p)
    if not polys:
        return None
    out = unary_union(polys)
    return None if out.is_empty else out


# A face whose terrain sits higher than this is not the sea, however the
# coastline is drawn round it. Over water the global tiles read as noise a few
# metres above zero, so the bar has to clear that without reaching real land.
SEA_MEDIAN_MAX_M = 20.0


def _face_medians(faces, dem):
    """Median terrain height under each face, or None without a DEM."""
    if dem is None:
        return None
    X, Y = np.meshgrid(dem.x_coords, dem.y_coords)
    # y_coords ascends while row 0 of z is north, so z has to be flipped to
    # match — exactly as every other consumer does. Pairing them unflipped
    # samples the north-south mirror of the polygon, which is how a sea loch
    # came back reading 447 m.
    z = np.flipud(np.asarray(dem.z, dtype=float))
    out = []
    for f in faces:
        m = shapely.contains_xy(f, X.ravel(), Y.ravel()).reshape(X.shape)
        v = z[m & np.isfinite(z)]
        out.append(float(np.median(v)) if v.size else None)
    return out


def sea_from_coastline(features: list[dict], region, warn=None, dem=None,
                       outer: float = 1.0):
    """The sea as OSM draws it, rather than as the elevation grid guesses it.

    Thresholding elevation cannot find a coastline in the global tiles: over
    water they are radar noise, not a plane — at Croabh Haven the surface reads
    0.5 m to 8 m, so the sea area just grows with the threshold and 0 m finds
    nothing. OSM's coastline is a real line, drawn at full detail, and it
    carries its own orientation: land lies to the left of the way, sea to the
    right.

    The region boundary closes whatever the coastline leaves open, so the
    result is a proper polygon even where the coast merely crosses the frame.
    Islands come back as holes because they are their own closed ways.

    `outer` widens the box the sea is worked out on, and the answer is cut back
    to the region afterwards. Orientation only decides land from sea where the
    coast properly crosses the frame; on a small capture it often just clips a
    corner, the vote settles on the wrong side, and the whole thing is refused.
    Giving the line a wider box to divide fixes that without changing what you
    asked for. `features` must reach that far — a concentric region shares this
    one's page frame, so their coordinates are directly comparable."""
    hw, hh = region.width_m / 2, region.height_m / 2
    box = shapely.box(-hw, -hh, hw, hh)
    work = (box if outer <= 1.0
            else shapely.box(-hw * outer, -hh * outer, hw * outer, hh * outer))
    lines = []
    for f in features:
        if f.get("layer") != "coastline":
            continue
        pts = np.asarray(f.get("pts", ()), dtype=float)
        if len(pts) < 2:
            continue
        clipped = LineString(pts).intersection(work)
        if clipped.is_empty:
            continue
        lines += [g for g in _lines_of(clipped) if g.length > 0]
    if not lines:
        if warn is not None:
            warn("no OSM coastline crosses this area, so no sea was drawn from "
                 "it — either there is none here, or the layer was not fetched")
        return None

    faces = list(polygonize(unary_union(lines + [work.boundary])))
    if not faces:
        if warn is not None:
            warn("the OSM coastline here does not close into any area")
        return None

    # Vote each face sea or land from the side of the line it sits on. One
    # ambiguous segment does not decide anything; the whole coast does.
    tree = shapely.STRtree(faces)
    score = [0] * len(faces)
    for line in lines:
        pts = np.asarray(line.coords, dtype=float)
        d = np.diff(pts, axis=0)
        seg_len = np.hypot(d[:, 0], d[:, 1])
        keep = seg_len > 1e-9
        if not keep.any():
            continue
        mid = (pts[:-1] + pts[1:]) / 2.0
        # right of travel is sea, in a y-up frame
        nx = (d[:, 1] / np.where(keep, seg_len, 1.0))
        ny = (-d[:, 0] / np.where(keep, seg_len, 1.0))
        for i in np.nonzero(keep)[0]:
            # step the probe out until it clears the line it came from; a
            # sliver of a face is still a face, so start close
            for eps in (0.5, 2.0, 8.0):
                landed = False
                for side, vote in ((1.0, 1), (-1.0, -1)):
                    p = shapely.Point(mid[i, 0] + side * nx[i] * eps,
                                      mid[i, 1] + side * ny[i] * eps)
                    for j in tree.query(p):
                        if faces[int(j)].contains(p):
                            score[int(j)] += vote
                            landed = True
                            break
                if landed:
                    break

    # Orientation is the only thing that actually knows which side is the sea:
    # it is the coastline's own convention, and where the line properly divides
    # the frame it is exact (verified at Croabh Haven, where the resulting
    # boundary lies on the coastline to within 0 mm).
    wet = [f for f, sc in zip(faces, score) if sc > 0]

    sea = wet
    if not sea:
        if warn is not None:
            warn("the OSM coastline here did not enclose any sea — its "
                 "direction may be inconsistent in this area")
        return None
    out = unary_union(sea)
    if out.is_empty:
        return None
    if outer > 1.0:
        # worked out wide, delivered to size
        out = out.intersection(box)
        if out.is_empty:
            if warn is not None:
                warn("the coastline nearby does not reach into this area, so "
                     "no sea was drawn — it is all on one side")
            return None
    # Where the coastline only clips a corner of the frame rather than dividing
    # it, the vote can settle on the wrong side and hand back a confidently
    # wrong sea — 91% of a mountain valley, at 189 m. Two attempts to repair
    # that from the terrain (flipping the groups, thresholding each face) both
    # made verified-good cases worse, so this refuses instead of guessing.
    med = _face_medians([out, box.difference(out)], dem)
    if med and med[0] is not None and med[1] is not None and med[0] > med[1]:
        if warn is not None:
            warn(f"could not tell land from sea along this coastline — the side "
                 f"it marks as water sits {med[0]:.0f} m up against {med[1]:.0f} m "
                 "for the other, so no sea was drawn. It works where the coast "
                 "crosses the whole frame; here it only clips part of it.")
        return None
    return out


def _lines_of(geom) -> list:
    """Every LineString in a geometry, whatever container it arrived in."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, LineString)]


# --------------------------------------------------------------------- hatch

def hatch(geom, spacing_m: float, angle_deg: float = 45.0,
          cross: bool = False) -> list[np.ndarray]:
    """Parallel (or crossed) fill lines clipped to `geom`."""
    if geom is None or geom.is_empty or spacing_m <= 0:
        return []
    angles = [angle_deg, angle_deg + 90.0] if cross else [angle_deg]
    out: list[np.ndarray] = []
    origin = geom.centroid
    for ang in angles:
        rot = affinity.rotate(geom, -ang, origin=origin)
        minx, miny, maxx, maxy = rot.bounds
        n = int((maxy - miny) / spacing_m) + 2
        if n > 20000:
            continue
        ys = miny + spacing_m * (np.arange(n) + 0.5)
        rows = MultiLineString([LineString([(minx - spacing_m, y),
                                            (maxx + spacing_m, y)]) for y in ys])
        cut = rot.intersection(rows)
        if cut.is_empty:
            continue
        cut = affinity.rotate(cut, ang, origin=origin)
        out.extend(_explode(cut))
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


# ---------------------------------------------------------------------- mask

def mask_features(features: list[dict], mask, layers: set[str],
                  min_length: float = 0.0) -> list[dict]:
    """Cut `mask` out of every feature in `layers`, dropping what vanishes.

    Batched through shapely's vectorised difference: a detailed map carries
    thousands of contour fragments and doing this one at a time is slow.

    `min_length` discards the slivers the cut itself creates. A contour that
    runs along the waterline — the 0 m one, when the sea is derived from the
    same grid — otherwise shatters into hundreds of sub-millimetre pieces, each
    costing a pen-up and pen-down for nothing."""
    if mask is None or mask.is_empty:
        return features
    idx = [i for i, f in enumerate(features)
           if f.get("layer") in layers and f.get("pts") is not None
           and len(f["pts"]) >= 2]
    if not idx:
        return features

    sizes = [len(features[i]["pts"]) for i in idx]
    geoms = shapely.linestrings(
        np.vstack([features[i]["pts"] for i in idx]),
        indices=np.repeat(np.arange(len(idx)), sizes))
    shapely.prepare(mask)
    cut = {i: g for i, g in zip(idx, shapely.difference(geoms, mask))}

    result: list[dict] = []
    for i, f in enumerate(features):
        if i not in cut:
            result.append(f)
            continue
        kept = 0
        for pts in _explode(cut[i]):
            if min_length > 0 and len(pts) >= 2:
                if float(np.hypot(*np.diff(pts, axis=0).T).sum()) < min_length:
                    continue
            result.append({**f, "pts": pts,
                           "id": f"{f.get('id', 'f')}{f'm{kept}' if kept else ''}",
                           "closed": bool(np.allclose(pts[0], pts[-1]))})
            kept += 1
    return result
