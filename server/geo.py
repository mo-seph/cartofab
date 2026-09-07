"""Projection helpers.

Everything in this app moves between three coordinate systems:

  WGS84  (EPSG:4326)  lon/lat degrees   -- what the map UI and OSM speak
  Merc   (EPSG:3857)  web mercator m    -- what DEM rasters are fetched in
  local  (+proj=tmerc) true ground m    -- what the SVG is drawn in

The local transverse-mercator is re-centred on each region, so within a
capture of a few tens of km the scale error is < 1e-4. That is what makes a
"1:1" capture an honest square on the ground rather than a square on screen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer

WGS84 = CRS.from_epsg(4326)
MERC = CRS.from_epsg(3857)

_to_merc = Transformer.from_crs(WGS84, MERC, always_xy=True)
_to_wgs = Transformer.from_crs(MERC, WGS84, always_xy=True)


@lru_cache(maxsize=64)
def _local_crs(lat: float, lon: float) -> CRS:
    return CRS.from_proj4(
        f"+proj=tmerc +lat_0={lat:.8f} +lon_0={lon:.8f} "
        f"+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )


@lru_cache(maxsize=64)
def _transformers(lat: float, lon: float):
    local = _local_crs(lat, lon)
    return (
        Transformer.from_crs(local, WGS84, always_xy=True),
        Transformer.from_crs(WGS84, local, always_xy=True),
        Transformer.from_crs(MERC, local, always_xy=True),
    )


@dataclass(frozen=True)
class Region:
    """A capture area, defined in true ground metres about a centre point."""

    lat: float
    lon: float
    width_m: float
    height_m: float
    rotation_deg: float = 0.0

    # -- coordinate transforms -------------------------------------------------

    @property
    def _t(self):
        # Round the centre so the lru_cache actually hits between requests.
        return _transformers(round(self.lat, 6), round(self.lon, 6))

    def local_to_wgs(self, x, y):
        return self._t[0].transform(x, y)

    def wgs_to_local(self, lon, lat):
        return self._t[1].transform(lon, lat)

    def merc_to_local(self, x, y):
        return self._t[2].transform(x, y)

    # -- the capture rectangle -------------------------------------------------

    @property
    def half(self) -> tuple[float, float]:
        return self.width_m / 2.0, self.height_m / 2.0

    def _rot(self, x: float, y: float) -> tuple[float, float]:
        if not self.rotation_deg:
            return x, y
        a = math.radians(self.rotation_deg)
        c, s = math.cos(a), math.sin(a)
        return x * c - y * s, x * s + y * c

    # -- page frame ------------------------------------------------------------
    # Everything downstream (DEM grid, contours, OSM, SVG) works in the *page*
    # frame: the capture rectangle un-rotated, so it is axis-aligned from
    # -w/2..w/2 and -h/2..h/2 with y up. Rotation then exists only here.

    def page_to_local(self, x, y):
        if not self.rotation_deg:
            return x, y
        a = math.radians(self.rotation_deg)
        c, s = math.cos(a), math.sin(a)
        x = np.asarray(x); y = np.asarray(y)
        return x * c - y * s, x * s + y * c

    def local_to_page(self, x, y):
        if not self.rotation_deg:
            return x, y
        a = math.radians(-self.rotation_deg)
        c, s = math.cos(a), math.sin(a)
        x = np.asarray(x); y = np.asarray(y)
        return x * c - y * s, x * s + y * c

    def page_to_wgs(self, x, y):
        lx, ly = self.page_to_local(x, y)
        return self.local_to_wgs(lx, ly)

    def wgs_to_page(self, lon, lat):
        lx, ly = self.wgs_to_local(lon, lat)
        return self.local_to_page(lx, ly)

    def corners_local(self) -> list[tuple[float, float]]:
        hw, hh = self.half
        return [self._rot(*p) for p in
                ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]

    def outline_wgs(self, per_edge: int = 16) -> list[tuple[float, float]]:
        """Region footprint as lon/lat, edges densified so it draws correctly
        on a mercator map (a ground rectangle is not a screen rectangle)."""
        cs = self.corners_local()
        pts = []
        for i in range(4):
            ax, ay = cs[i]
            bx, by = cs[(i + 1) % 4]
            for k in range(per_edge):
                t = k / per_edge
                pts.append((ax + (bx - ax) * t, ay + (by - ay) * t))
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        lons, lats = self.local_to_wgs(xs, ys)
        return list(zip(lons, lats))

    def bbox_wgs(self, margin: float = 0.02) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat) enclosing the region."""
        pts = self.outline_wgs()
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        dx = (max(lons) - min(lons)) * margin
        dy = (max(lats) - min(lats)) * margin
        return (min(lons) - dx, min(lats) - dy, max(lons) + dx, max(lats) + dy)

    def bbox_merc(self, margin: float = 0.02) -> tuple[float, float, float, float]:
        w, s, e, n = self.bbox_wgs(margin)
        x0, y0 = _to_merc.transform(w, s)
        x1, y1 = _to_merc.transform(e, n)
        return (x0, y0, x1, y1)

    @property
    def merc_scale(self) -> float:
        """Web-mercator metres per true ground metre at this latitude."""
        return 1.0 / math.cos(math.radians(self.lat))


def wgs_to_merc(lon, lat):
    return _to_merc.transform(lon, lat)


def merc_to_wgs(x, y):
    return _to_wgs.transform(x, y)
