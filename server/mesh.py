"""Terrain meshes from the same elevation grid the contours come from.

The DEM is already a regular grid in the region's page frame, in ground
metres, north-up — which is exactly a heightfield. This turns it into a
watertight solid, optionally with flat water and with buildings and roads as
separate raised objects so a slicer can colour them independently.

Units: X and Y use the page scale (width_mm / width_m), so a model is the same
physical width as the equivalent SVG. Z uses that scale times an exaggeration
factor, because true 1:1 relief looks disappointingly flat at map scales.
"""
from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import Polygon


# Vertices closer than this in *model* millimetres are the same point as far
# as a slicer is concerned, so that is the scale a pinch has to be judged at —
# not in ground metres, which at map scales differ by three orders of
# magnitude. Easing a pinched polygon in by ten times this reliably separates
# the rings while staying far below one printed layer.
WELD_MM = 1e-4


@dataclass
class MeshObject:
    name: str
    colour: tuple[float, float, float]
    verts: np.ndarray                     # (n, 3) float32, millimetres
    faces: np.ndarray                     # (m, 3) int32
    meta: dict = field(default_factory=dict)

    @property
    def triangles(self) -> int:
        return len(self.faces)


# --------------------------------------------------------------- heightfield

def resample(dem, nx: int, ny: int, fill: float | None = None
             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bilinearly resample the DEM onto an nx x ny mesh grid.

    Mesh resolution is deliberately independent of DEM sampling: a 1600x1600
    capture is 5.1 M triangles and a 256 MB STL, which is not a useful object.
    """
    hw = dem.region.width_m / 2
    hh = dem.region.height_m / 2
    xs = np.linspace(-hw, hw, nx)
    ys = np.linspace(-hh, hh, ny)          # ascending north

    src = np.flipud(dem.z)                 # ascending y to match
    sy, sx = src.shape
    fx = (xs + hw) / (2 * hw) * (sx - 1)
    fy = (ys + hh) / (2 * hh) * (sy - 1)
    x0 = np.clip(np.floor(fx).astype(int), 0, sx - 2)
    y0 = np.clip(np.floor(fy).astype(int), 0, sy - 2)
    tx = (fx - x0).astype(np.float32)[None, :]
    ty = (fy - y0).astype(np.float32)[:, None]

    a = src[np.ix_(y0, x0)]
    b = src[np.ix_(y0, x0 + 1)]
    c = src[np.ix_(y0 + 1, x0)]
    d = src[np.ix_(y0 + 1, x0 + 1)]
    Z = ((a * (1 - tx) + b * tx) * (1 - ty)
         + (c * (1 - tx) + d * tx) * ty).astype(np.float32)

    bad = ~np.isfinite(Z)
    if bad.any():
        good = Z[~bad]
        Z[bad] = fill if fill is not None else (
            float(good.min()) if good.size else 0.0)
    return Z, xs, ys


def water_mask(xs: np.ndarray, ys: np.ndarray, geom) -> np.ndarray:
    """Which mesh vertices fall inside a water polygon."""
    X, Y = np.meshgrid(xs, ys)
    shapely.prepare(geom)
    return shapely.contains_xy(geom, X.ravel(), Y.ravel()).reshape(X.shape)


def dem_level_in(dem):
    """Water level from the full-resolution DEM inside a polygon.

    The mesh grid is far coarser than the elevation grid, so reading the level
    off the mesh means a small lake is judged from a handful of vertices."""
    zz = np.flipud(dem.z)
    hw = dem.region.width_m / 2
    hh = dem.region.height_m / 2
    ny, nx = zz.shape
    xs = -hw + (np.arange(nx) + 0.5) * (2 * hw / nx)
    ys = -hh + (np.arange(ny) + 0.5) * (2 * hh / ny)

    def level(geom):
        mnx, mny, mxx, mxy = geom.bounds
        i0, i1 = np.searchsorted(xs, [mnx, mxx])
        j0, j1 = np.searchsorted(ys, [mny, mxy])
        i0, j0 = max(0, i0 - 1), max(0, j0 - 1)
        i1, j1 = min(nx, i1 + 1), min(ny, j1 + 1)
        if i1 <= i0 or j1 <= j0:
            return None
        X, Y = np.meshgrid(xs[i0:i1], ys[j0:j1])
        shapely.prepare(geom)
        inside = shapely.contains_xy(geom, X.ravel(), Y.ravel())
        vals = zz[j0:j1, i0:i1].ravel()[inside]
        vals = vals[np.isfinite(vals)]
        return float(np.median(vals)) if vals.size else None
    return level


def dilate(mask: np.ndarray, n: int = 1) -> np.ndarray:
    """Grow a boolean mask by n cells in each direction.

    A water polygon is rasterised at mesh *vertices*, but the surface between a
    flattened vertex and its unflattened neighbour slopes up through the water
    slab. Growing the mask by a cell means the flat region fully covers the
    polygon, so nothing pokes through at the shore."""
    out = mask.copy()
    for _ in range(max(0, n)):
        g = out.copy()
        g[1:, :] |= out[:-1, :]
        g[:-1, :] |= out[1:, :]
        g[:, 1:] |= out[:, :-1]
        g[:, :-1] |= out[:, 1:]
        out = g
    return out


def flatten_water(Z: np.ndarray, mask: np.ndarray,
                  level: float | None = None) -> float | None:
    """Set every masked vertex to one level, so the surface is truly flat.

    The level is the median of the terrain underneath rather than its minimum:
    DEMs already render lakes close to flat, and the median ignores the few
    shoreline vertices that catch the bank."""
    if not mask.any():
        return None
    if level is None:
        level = float(np.median(Z[mask]))
    Z[mask] = level
    return level


# ------------------------------------------------------------------ geometry

class Frame:
    """Ground metres (page frame) to model millimetres.

    X and Y scale independently, so a model can be given an aspect that differs
    from the capture. Height uses the geometric mean of the two, which reduces
    to the ordinary scale whenever the aspect is left locked."""

    def __init__(self, region, width_mm: float, exaggeration: float,
                 base_mm: float, z_ref: float, height_mm: float | None = None):
        self.scale_x = width_mm / region.width_m
        self.scale_y = ((height_mm / region.height_m) if height_mm
                        else self.scale_x)
        self.scale = float(np.sqrt(self.scale_x * self.scale_y))
        self.exag = exaggeration
        self.base = base_mm
        self.z_ref = z_ref

    def x(self, v):
        a = np.asarray(v, dtype=np.float64)
        if a.ndim == 2 and a.shape[1] >= 2:
            out = a.copy()
            out[:, 0] *= self.scale_x
            out[:, 1] *= self.scale_y
            return out
        return a * self.scale_x

    def xv(self, v):
        """A 1-D array of x coordinates."""
        return np.asarray(v, dtype=np.float64) * self.scale_x

    def yv(self, v):
        """A 1-D array of y coordinates."""
        return np.asarray(v, dtype=np.float64) * self.scale_y

    def z(self, elev):
        return (np.asarray(elev) - self.z_ref) * self.scale * self.exag + self.base


def terrain_solid(Z: np.ndarray, xs: np.ndarray, ys: np.ndarray,
                  frame: Frame, name="terrain",
                  colour=(0.62, 0.58, 0.50),
                  floor_mm: float = 0.0) -> MeshObject:
    """Top surface, four dropped walls and a flat bottom: a closed solid.

    `floor_mm` lifts the underside, so the terrain can sit on a backplate
    instead of intersecting it."""
    ny, nx = Z.shape
    hw = xs[-1]
    X, Y = np.meshgrid(frame.xv(xs), frame.yv(ys))
    top = np.column_stack([X.ravel(), Y.ravel(), frame.z(Z).ravel()])

    # top surface
    i = np.arange(nx - 1)
    j = np.arange(ny - 1)
    J, I = np.meshgrid(j, i, indexing="ij")
    v00 = (J * nx + I).ravel()
    v10 = v00 + 1
    v01 = v00 + nx
    v11 = v01 + 1
    top_faces = np.concatenate([
        np.column_stack([v00, v10, v11]),
        np.column_stack([v00, v11, v01]),
    ])

    # boundary ring of the top grid, anticlockwise seen from above
    bottom_row = np.arange(nx)                             # y = ys[0]
    right_col = np.arange(1, ny) * nx + (nx - 1)
    top_row = (ny - 1) * nx + np.arange(nx - 2, -1, -1)
    left_col = np.arange(ny - 2, 0, -1) * nx
    ring = np.concatenate([bottom_row, right_col, top_row, left_col])

    skirt = top[ring].copy()
    skirt[:, 2] = floor_mm
    verts = np.vstack([top, skirt]).astype(np.float32)
    off = len(top)
    n = len(ring)

    a = ring
    b = np.roll(ring, -1)
    sa = off + np.arange(n)
    sb = off + (np.arange(n) + 1) % n
    walls = np.concatenate([
        np.column_stack([a, sa, sb]),
        np.column_stack([a, sb, b]),
    ])

    # the skirt ring is a rectangle outline, so a fan from one corner closes it
    fan = np.column_stack([
        np.full(n - 2, off), off + np.arange(2, n), off + np.arange(1, n - 1)])

    faces = np.vstack([top_faces, walls, fan]).astype(np.int32)
    return MeshObject(name, colour, verts, faces,
                      {"grid": [int(nx), int(ny)]})


def grid_slab(mask: np.ndarray, xs: np.ndarray, ys: np.ndarray, frame: Frame,
              top_mm, thickness_mm: float, name: str, colour
              ) -> MeshObject | None:
    """A flat slab covering the cells of a rasterised mask.

    Water is built this way rather than by extruding its polygon. The sea
    traced from an elevation grid has tens of thousands of sub-millimetre
    wiggles and hundreds of island rings, and extruding that reliably produces
    a pinched, non-manifold mess. A slab built on the mesh grid is manifold by
    construction and lines up exactly with the terrain recess underneath it.
    The cost is a shoreline only as crisp as the mesh, which is what the
    terrain underneath has anyway.

    `top_mm` may be a per-vertex array. Building every water body as one slab
    with varying height, rather than one slab each, is what keeps it manifold
    where two bodies abut: their walls would otherwise coincide and weld into
    an edge with four faces. Where the levels differ the surface simply steps,
    which is what two adjacent bodies at different levels look like anyway.
    """
    ny, nx = mask.shape
    cell = mask[:-1, :-1] & mask[:-1, 1:] & mask[1:, :-1] & mask[1:, 1:]
    if not cell.any():
        return None

    # Two cells touching only at a corner put four wall faces on the one
    # vertical edge between them, which is non-manifold. Filling the gap turns
    # the diagonal pinch into an ordinary edge contact; it adds at most a cell
    # of water and converges in a couple of passes.
    for _ in range(8):
        a = cell[:-1, :-1] & cell[1:, 1:] & ~cell[:-1, 1:] & ~cell[1:, :-1]
        b = cell[:-1, 1:] & cell[1:, :-1] & ~cell[:-1, :-1] & ~cell[1:, 1:]
        if not (a.any() or b.any()):
            break
        cell[:-1, 1:] |= a
        cell[:-1, :-1] |= b

    used = np.zeros((ny, nx), dtype=bool)
    used[:-1, :-1] |= cell
    used[:-1, 1:] |= cell
    used[1:, :-1] |= cell
    used[1:, 1:] |= cell
    n = int(used.sum())
    idx = np.full((ny, nx), -1, dtype=np.int64)
    idx[used] = np.arange(n)

    X, Y = np.meshgrid(frame.xv(xs), frame.yv(ys))
    tz = (np.full(n, float(top_mm)) if np.isscalar(top_mm)
          else np.asarray(top_mm)[used].astype(np.float64))
    top = np.column_stack([X[used], Y[used], tz])
    bot = np.column_stack([X[used], Y[used], tz - thickness_mm])
    verts = np.vstack([top, bot]).astype(np.float32)

    J, I = np.nonzero(cell)
    v00, v10 = idx[J, I], idx[J, I + 1]
    v01, v11 = idx[J + 1, I], idx[J + 1, I + 1]
    faces = [np.column_stack([v00, v10, v11]), np.column_stack([v00, v11, v01]),
             np.column_stack([v00 + n, v11 + n, v10 + n]),
             np.column_stack([v00 + n, v01 + n, v11 + n])]

    pad = np.zeros((cell.shape[0] + 2, cell.shape[1] + 2), dtype=bool)
    pad[1:-1, 1:-1] = cell
    inner = pad[1:-1, 1:-1]
    for dj, di, a, b in ((0, -1, v00, v01), (0, 1, v11, v10),
                         (-1, 0, v10, v00), (1, 0, v01, v11)):
        nb = pad[1 + dj:cell.shape[0] + 1 + dj, 1 + di:cell.shape[1] + 1 + di]
        e = ~nb[inner[inner]] if False else ~nb[J, I]
        if not e.any():
            continue
        aa, bb = a[e], b[e]
        faces.append(np.column_stack([aa, bb, bb + n]))
        faces.append(np.column_stack([aa, bb + n, aa + n]))

    obj = MeshObject(name, colour, verts,
                     np.vstack(faces).astype(np.int32))
    if signed_volume(obj) < 0:            # keep normals pointing outward
        obj.faces = obj.faces[:, ::-1].copy()
    return obj


def merge(objs: list[MeshObject], name: str, colour) -> MeshObject | None:
    """Combine several parts into one named object."""
    objs = [o for o in objs if o is not None and len(o.faces)]
    if not objs:
        return None
    verts, faces, base = [], [], 0
    for o in objs:
        verts.append(o.verts)
        faces.append(o.faces + base)
        base += len(o.verts)
    return MeshObject(name, colour, np.vstack(verts).astype(np.float32),
                      np.vstack(faces).astype(np.int32))


def plate(w_mm: float, h_mm: float, t_mm: float, name: str, colour,
          z0: float = 0.0) -> MeshObject:
    """A plain rectangular slab, centred on the origin."""
    hw, hh = w_mm / 2, h_mm / 2
    z1 = z0 + t_mm
    v = np.array([
        [-hw, -hh, z0], [hw, -hh, z0], [hw, hh, z0], [-hw, hh, z0],
        [-hw, -hh, z1], [hw, -hh, z1], [hw, hh, z1], [-hw, hh, z1],
    ], dtype=np.float32)
    f = np.array([
        [0, 2, 1], [0, 3, 2],          # bottom
        [4, 5, 6], [4, 6, 7],          # top
        [0, 1, 5], [0, 5, 4],
        [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6],
        [3, 0, 4], [3, 4, 7],
    ], dtype=np.int32)
    return MeshObject(name, colour, v, f)


def _dedupe_ring(c: np.ndarray, tol: float = 1e-6) -> np.ndarray:
    """Drop repeated vertices, at a tolerance well below anything printable.

    Densifying a buffered polygon emits coincident points, and earcut turns
    those into zero-area triangles. Those cannot simply be deleted afterwards —
    the walls still enclose that sliver, so removing the triangle opens a hole —
    so they have to be prevented here instead."""
    if len(c) < 2:
        return c
    keep = np.ones(len(c), dtype=bool)
    keep[1:] = np.any(np.abs(np.diff(c, axis=0)) > tol, axis=1)
    c = c[keep]
    if len(c) > 1 and np.all(np.abs(c[0] - c[-1]) <= tol):
        c = c[:-1]
    return c


def _rings_of(part) -> tuple[np.ndarray, np.ndarray]:
    """Ring vertices (exterior then holes) and the ring end offsets earcut wants."""
    if getattr(part, "geom_type", None) != "Polygon":
        return np.zeros((0, 2)), np.zeros(0, dtype=np.int64)
    rings = [_dedupe_ring(np.asarray(part.exterior.coords)[:-1])]
    rings += [_dedupe_ring(np.asarray(r.coords)[:-1]) for r in part.interiors]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return np.zeros((0, 2)), np.zeros(0, dtype=np.int64)
    return np.vstack(rings), np.cumsum([len(r) for r in rings])


def extrude(polys, ground_fn, frame: Frame, height_mm: float,
            name: str, colour, sink_mm: float = 0.4,
            sample=None, densify_m: float | None = None,
            flat_top: bool = False) -> MeshObject | None:
    """Prisms standing on the terrain, manifold by construction.

    Caps and walls index the *same* ring vertices, so there is nothing to weld
    and no chance of a seam. Triangulation is ear clipping, which respects
    concavity and holes exactly — a Delaunay-of-the-vertices approach filtered
    by centroid silently leaves gaps and overlaps on real footprints, and those
    become the open edges a slicer complains about.

    With `sample` the prism is draped: every vertex takes its own ground
    height, so a road follows the hillside instead of sitting at the lowest
    point under the whole network.

    `flat_top` drapes only the underside — the base follows the ground like a
    foundation while the roof stays level, which is how a building actually
    sits on a slope. Without it both surfaces follow the terrain, which is
    what a road wants.
    """
    import mapbox_earcut as earcut

    V: list[np.ndarray] = []
    F: list[np.ndarray] = []
    base = 0
    stack = list(polys)
    while stack:
        poly = stack.pop()
        for part in (poly.geoms if hasattr(poly, "geoms") else [poly]):
            if part.is_empty or part.geom_type != "Polygon":
                continue
            if densify_m:
                part = shapely.segmentize(part, densify_m)
            # a self-touching ring makes earcut produce overlapping triangles;
            # buffer(0) splits it into clean parts first
            if not part.is_valid:
                fixed = part.buffer(0)
                if fixed.is_empty:
                    continue
                if fixed.geom_type != "Polygon":
                    stack.append(fixed)      # re-queue the split pieces
                    continue
                part = fixed
            part = shapely.geometry.polygon.orient(part, 1.0)   # ext CCW
            if part.geom_type != "Polygon":       # orient can hand back a multi
                stack.append(part)
                continue
            verts, ends = _rings_of(part)
            if len(verts) < 3:
                continue
            # A hole that touches the exterior is valid geometry but pinches:
            # both rings put a wall on the same edge, which welds into an edge
            # with four faces. Judged in model units, since that is what welds.
            model = frame.x(verts) / WELD_MM
            if len(np.unique(np.round(model), axis=0)) != len(verts):
                eased = part.buffer(-WELD_MM / frame.scale)
                if eased.is_empty:
                    continue
                if eased.geom_type != "Polygon":
                    stack.append(eased)
                    continue
                part = shapely.geometry.polygon.orient(eased, 1.0)
                verts, ends = _rings_of(part)
                if len(verts) < 3:
                    continue
            try:
                tri = earcut.triangulate_float64(
                    np.ascontiguousarray(verts), ends).reshape(-1, 3)
            except Exception:                                   # noqa: BLE001
                continue
            if not len(tri):
                continue

            if sample is not None:
                g = sample(verts)
            else:
                gv = ground_fn(part)
                if gv is None:
                    continue
                g = np.full(len(verts), gv, dtype=np.float64)

            zb = frame.z(g) - sink_mm
            zt = (np.full(len(g), float(frame.z(g).max()) + height_mm)
                  if flat_top else frame.z(g) + height_mm)
            xy = frame.x(verts)
            n = len(verts)
            V.append(np.column_stack([xy[:, 0], xy[:, 1], zb]))
            V.append(np.column_stack([xy[:, 0], xy[:, 1], zt]))

            F.append(tri[:, ::-1] + base)                       # bottom, faces down
            F.append(tri + base + n)                            # top, faces up
            start_i = 0
            for e in ends:
                idx = np.arange(start_i, e)
                nxt = np.roll(idx, -1)
                F.append(np.column_stack([idx, nxt, nxt + n]) + base)
                F.append(np.column_stack([idx, nxt + n, idx + n]) + base)
                start_i = e
            base += 2 * n

    if not V:
        return None
    return MeshObject(name, colour, np.vstack(V).astype(np.float32),
                      np.vstack(F).astype(np.int32))


def bilinear_sampler(Z: np.ndarray, xs: np.ndarray, ys: np.ndarray):
    """Terrain height at arbitrary page-frame points, for draping."""
    ny, nx = Z.shape
    x0v, x1v = xs[0], xs[-1]
    y0v, y1v = ys[0], ys[-1]

    def sample(pts: np.ndarray) -> np.ndarray:
        fx = np.clip((pts[:, 0] - x0v) / (x1v - x0v), 0, 1) * (nx - 1)
        fy = np.clip((pts[:, 1] - y0v) / (y1v - y0v), 0, 1) * (ny - 1)
        i = np.clip(np.floor(fx).astype(int), 0, nx - 2)
        j = np.clip(np.floor(fy).astype(int), 0, ny - 2)
        tx, ty = fx - i, fy - j
        return ((Z[j, i] * (1 - tx) + Z[j, i + 1] * tx) * (1 - ty)
                + (Z[j + 1, i] * (1 - tx) + Z[j + 1, i + 1] * tx) * ty)
    return sample


# -------------------------------------------------------------------- checks

def close_holes(obj: MeshObject, max_loop: int = 2000) -> int:
    """Fill any remaining boundary loops, and report how many were closed.

    Ear clipping occasionally leaves a hole where a cleaned road union has a
    ring it cannot handle. Rather than chase every such case in the geometry,
    find the open edges directly, walk them into loops and fan-fill each one.
    A hole in a mesh is a well-defined thing to repair, whatever produced it.
    """
    v = np.round(obj.verts / WELD_MM).astype(np.int64)
    _, first, inv = np.unique(v, axis=0, return_index=True, return_inverse=True)
    f = inv[obj.faces]

    directed = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    keys = directed[:, 0].astype(np.int64) * (len(first) + 1) + directed[:, 1]
    rev = directed[:, 1].astype(np.int64) * (len(first) + 1) + directed[:, 0]
    open_edges = directed[~np.isin(keys, rev)]
    if not len(open_edges):
        return 0

    nxt: dict[int, list[int]] = {}
    for a, b in open_edges:
        nxt.setdefault(int(a), []).append(int(b))

    new_faces = []
    filled = 0
    while nxt:
        start = next(iter(nxt))
        loop = [start]
        cur = start
        while True:
            outs = nxt.get(cur)
            if not outs:
                break
            nb = outs.pop()
            if not outs:
                nxt.pop(cur, None)
            if nb == start:
                break
            if nb in loop or len(loop) > max_loop:
                break
            loop.append(nb)
            cur = nb
        if len(loop) >= 3:
            idx = first[np.array(loop)]          # back to original vertices
            fan = np.column_stack([np.full(len(idx) - 2, idx[0]),
                                   idx[2:], idx[1:-1]])
            new_faces.append(fan)
            filled += 1

    if not new_faces:
        return 0
    obj.faces = np.vstack([obj.faces, np.vstack(new_faces)]).astype(np.int32)
    if signed_volume(obj) < 0:
        obj.faces = obj.faces[:, ::-1].copy()
    return filled


def signed_volume(obj: MeshObject) -> float:
    """Positive for outward-facing normals on a closed mesh."""
    t = obj.verts[obj.faces].astype(np.float64)
    return float(np.einsum("ij,ij->i",
                           t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def edge_report(obj: MeshObject, weld_tol: float = 1e-4) -> dict:
    """Every edge of a closed solid is shared by exactly two faces.

    Welded by position, because that is how a slicer reads an STL: index
    sharing in our arrays is irrelevant to whether the file is watertight."""
    v = np.round(obj.verts / weld_tol).astype(np.int64)
    _, inv = np.unique(v, axis=0, return_inverse=True)
    f = inv[obj.faces]
    degenerate = int(((f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2])
                      | (f[:, 0] == f[:, 2])).sum())
    e = np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return {"edges": int(len(counts)),
            "boundary": int((counts == 1).sum()),
            "nonmanifold": int((counts > 2).sum()),
            "degenerate": degenerate,
            "closed": bool((counts == 2).all() and degenerate == 0)}


def _edge_report_indexed(obj: MeshObject) -> dict:
    f = obj.faces
    e = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    e = np.sort(e, axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return {"edges": int(len(counts)),
            "boundary": int((counts == 1).sum()),
            "nonmanifold": int((counts > 2).sum()),
            "closed": bool((counts == 2).all())}


# -------------------------------------------------------------------- output

def write_stl(objects: list[MeshObject]) -> bytes:
    """Binary STL. No colour and no object names — one soup of triangles."""
    tris = []
    for o in objects:
        tris.append(o.verts[o.faces])
    t = np.concatenate(tris) if tris else np.zeros((0, 3, 3), np.float32)
    n = len(t)
    out = bytearray(b"cartofab terrain mesh".ljust(80, b" "))
    out += struct.pack("<I", n)
    rec = np.zeros((n, 50), dtype=np.uint8)
    v = t.astype("<f4")
    a, b, c = v[:, 0], v[:, 1], v[:, 2]
    nrm = np.cross(b - a, c - a)
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.divide(nrm, ln, out=np.zeros_like(nrm), where=ln > 0)
    block = np.concatenate([nrm.astype("<f4"), v.reshape(n, 9)], axis=1)
    rec[:, :48] = block.astype("<f4").view(np.uint8).reshape(n, 48)
    out += rec.tobytes()
    return bytes(out)


def write_obj(objects: list[MeshObject], mtl_name="model.mtl"
              ) -> tuple[str, str]:
    """OBJ with one named object per part and an MTL giving each a colour —
    which is what lets a slicer treat them as separate, paintable bodies."""
    obj = [f"# cartofab\nmtllib {mtl_name}\n"]
    mtl = ["# cartofab\n"]
    base = 1
    for o in objects:
        r, g, b = o.colour
        mtl.append(f"newmtl {o.name}\nKd {r:.3f} {g:.3f} {b:.3f}\nKa 0 0 0\n"
                   f"Ks 0 0 0\nd 1\nillum 1\n")
        obj.append(f"o {o.name}\nusemtl {o.name}")
        obj.append("\n".join(f"v {x:.4f} {y:.4f} {z:.4f}" for x, y, z in o.verts))
        f = o.faces + base
        obj.append("\n".join(f"f {a} {b} {c}" for a, b, c in f))
        base += len(o.verts)
    return "\n".join(obj) + "\n", "\n".join(mtl) + "\n"


def write_3mf(objects: list[MeshObject]) -> bytes:
    """3MF: multiple named objects, each with its own colour, in one file.

    Worth having alongside OBJ because slicers built around 3MF — Bambu Studio
    especially — read per-object colour from it reliably, where an OBJ+MTL
    often arrives as a single uncoloured body.
    """
    import zipfile

    # 3MF is happiest with the model sitting in positive space
    lo = np.min([o.verts.min(axis=0) for o in objects], axis=0)
    shift = np.array([-lo[0], -lo[1], 0.0], dtype=np.float64)

    mats = "".join(
        f'<base name="{o.name}" displaycolor="'
        f'#{int(o.colour[0]*255):02X}{int(o.colour[1]*255):02X}'
        f'{int(o.colour[2]*255):02X}FF"/>'
        for o in objects)

    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             '<metadata name="Application">cartofab</metadata>'
             f'<resources><basematerials id="1">{mats}</basematerials>']
    for i, o in enumerate(objects):
        v = (o.verts.astype(np.float64) + shift)
        parts.append(f'<object id="{i + 2}" type="model" pid="1" pindex="{i}" '
                     f'name="{o.name}"><mesh><vertices>')
        parts.append("".join(
            f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v))
        parts.append("</vertices><triangles>")
        parts.append("".join(
            f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in o.faces))
        parts.append("</triangles></mesh></object>")
    parts.append("</resources><build>")
    parts.extend(f'<item objectid="{i + 2}"/>' for i in range(len(objects)))
    parts.append("</build></model>")
    model = "".join(parts)

    ct = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
          "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


def write_viewer(objects: list[MeshObject], stats: dict) -> bytes:
    """Compact indexed geometry for the browser viewer: a JSON header followed
    by float32 positions and uint32 indices per object. The viewer therefore
    shows exactly the mesh that gets exported, not an approximation of it."""
    header = {"objects": [], "stats": stats}
    body = bytearray()
    for o in objects:
        v = o.verts.astype("<f4").tobytes()
        f = o.faces.astype("<u4").tobytes()
        header["objects"].append({
            "name": o.name, "colour": list(o.colour),
            "verts": len(o.verts), "faces": len(o.faces),
            "voff": len(body), "vlen": len(v),
            "foff": len(body) + len(v), "flen": len(f),
        })
        body += v
        body += f
    head = json.dumps(header).encode()
    return struct.pack("<I", len(head)) + head + bytes(body)
