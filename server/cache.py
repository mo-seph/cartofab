"""Tiny content-addressed disk cache so repeat exports do not refetch."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

CACHE_DIR = Path(os.environ.get("MAPCONTOURS_CACHE", Path(__file__).parent.parent / "cache"))


def _path(key: str, suffix: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()
    return CACHE_DIR / h[:2] / f"{h}{suffix}"


def cache_get(key: str, suffix: str = "") -> bytes | None:
    p = _path(key, suffix)
    try:
        return p.read_bytes()
    except OSError:
        return None


def cache_put(key: str, suffix: str, data: bytes) -> None:
    p = _path(key, suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def cache_size() -> int:
    return sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())


def cache_clear() -> dict:
    """Delete everything cached. Purely a disk-space operation — anything
    removed is refetched on demand."""
    removed = 0
    freed = 0
    for f in CACHE_DIR.rglob("*"):
        if f.is_file():
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                pass
    for d in sorted((d for d in CACHE_DIR.rglob("*") if d.is_dir()),
                    key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    return {"removed": removed, "freed_mb": round(freed / 1e6, 1)}
