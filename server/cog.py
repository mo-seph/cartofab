"""Reading remote Cloud-Optimised GeoTIFFs without downloading them whole.

The Scottish LiDAR tiles are 0.25-1 m COGs of 15-60 MB each. We usually want
them at 2-10 m, which is one of the overview levels already inside the file, so
we open the file over HTTP range requests and let tifffile pull only the IFDs
and the overview tiles it actually needs — typically ~1 MB of an 18 MB tile.
"""
from __future__ import annotations

import io

import httpx
import numpy as np
import tifffile


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests, with block caching."""

    def __init__(self, url: str, client: httpx.Client, block: int = 1 << 18):
        self.url, self.client, self.block = url, client, block
        self.pos = 0
        self._blocks: dict[int, bytes] = {}
        self.requests = 0
        self.bytes = 0
        head = self.client.head(url)
        head.raise_for_status()
        self.size = int(head.headers["content-length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = (off if whence == 0 else
                    self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def tell(self) -> int:
        return self.pos

    def _block(self, idx: int) -> bytes:
        if idx not in self._blocks:
            a = idx * self.block
            b = min(a + self.block, self.size) - 1
            r = self.client.get(self.url, headers={"Range": f"bytes={a}-{b}"})
            r.raise_for_status()
            self._blocks[idx] = r.content
            self.requests += 1
            self.bytes += len(r.content)
        return self._blocks[idx]

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        out = bytearray()
        while n > 0:
            chunk = self._block(self.pos // self.block)[self.pos % self.block:][:n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def read_level(url: str, client: httpx.Client, want_res: float
               ) -> tuple[np.ndarray, tuple[float, float, float, float], float]:
    """Read the coarsest overview whose pixel size is still <= want_res.

    Returns (array, (minx, miny, maxx, maxy), pixel_size) using the file's own
    georeferencing rather than anything inferred from its name.
    """
    f = HttpRangeFile(url, client)
    with tifffile.TiffFile(f) as tif:
        page0 = tif.pages[0]
        tie = page0.tags["ModelTiepointTag"].value
        scale = page0.tags["ModelPixelScaleTag"].value
        base = float(scale[0])
        ox, oy = float(tie[3]), float(tie[4])          # top-left of the image

        best = 0
        for i, p in enumerate(tif.pages):
            res = base * (page0.imagewidth / p.imagewidth)
            if res <= want_res + 1e-9:
                best = i
        page = tif.pages[best]
        arr = page.asarray()
        res = base * (page0.imagewidth / page.imagewidth)

    if arr.ndim == 3:
        arr = arr[..., 0]
    arr = arr.astype(np.float32)
    h, w = arr.shape
    bbox = (ox, oy - h * res, ox + w * res, oy)
    return arr, bbox, res
