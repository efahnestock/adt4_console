#!/usr/bin/env python3
"""Cut a GeoTIFF orthophoto (e.g. flown by the quad) into the console's tile cache.

Writes <tiles-dir>/<layer>/{z}/{x}/{y}.jpg and <layer>/tiles.json, the layout the
console serves with --tiles-dir (docs/API.md §14).

* The zmax tiles are warped from the GeoTIFF (to EPSG:3857, Lanczos); each lower
  zoom is its four children averaged (premultiplied, so transparent pixels do
  not darken the edges).
* Where the ortho is transparent (outside the flown area) the --fallback layer
  shows through: its tile at the same z/x/y, or, past its deepest zoom, the
  matching part of its deepest ancestor, upscaled. Without a fallback, grey.
* The manifest marks the layer as an orthophoto (kind "ortho", --priority) and
  names the fallback, so the console draws this layer by default over the map
  it covers and serves the fallback's tiles around it.

--manifest-only rewrites only the manifest fields of a layer cut earlier
(kind / priority / fallback / attribution), without the GeoTIFF.

Needs rasterio, numpy and Pillow (pip install -e .[geo]); the console itself
does not.

Example:
    make_ortho_tiles.py ortho.tif ~/.adt4_console/tiles quad_site_2024 \\
        --zmin 15 --zmax 22 --fallback city_aerial --attribution "Site quad ortho 2024"
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib

R = 6378137.0
ORTHO_PRIORITY = 10


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    s = 2 * math.pi * R / n
    o = math.pi * R
    return (x * s - o, o - (y + 1) * s, (x + 1) * s - o, o - y * s)


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2**z
    return (
        (lon + 180) / 360 * n,
        (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n,
    )


def fallback_rgb(root: pathlib.Path, layer: str | None, z: int, x: int, y: int):
    """The fallback layer's pixels for tile z/x/y (HxWx3 uint8), or None."""
    import numpy as np
    from PIL import Image

    if not layer:
        return None
    for dz in range(0, z + 1):
        base = root / layer / str(z - dz) / str(x >> dz) / str(y >> dz)
        p = next(
            (q for q in (base.with_suffix(e) for e in (".jpg", ".png", ".jpeg")) if q.exists()),
            None,
        )
        if p is None:
            continue
        im = Image.open(p).convert("RGB")
        if dz:
            k = 256 >> dz
            if k < 1:
                return None
            ox, oy = (x - ((x >> dz) << dz)) * k, (y - ((y >> dz) << dz)) * k
            im = im.crop((ox, oy, ox + k, oy + k)).resize((256, 256), Image.BICUBIC)
        return np.asarray(im)
    return None


def manifest_fields(args) -> dict:
    return {
        "kind": "ortho",
        "priority": args.priority,
        "fallback": args.fallback,
        "default": False,
    }


def cut(args) -> dict:
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject, transform_bounds

    out = args.tiles_dir / args.layer
    rgba = {}  # (x, y) -> HxWx4 uint8 at the current zoom
    with rasterio.open(args.src) as src:
        lon0, lat0, lon1, lat1 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        bands = list(range(1, min(src.count, 4) + 1))
        z = args.zmax
        x0, y0 = map(int, lonlat_to_tile(lon0, lat1, z))
        x1, y1 = map(int, lonlat_to_tile(lon1, lat0, z))
        print(f"z{z}: {(x1 - x0 + 1) * (y1 - y0 + 1)} tiles to warp", flush=True)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                dst = np.zeros((len(bands), 256, 256), np.uint8)
                reproject(
                    rasterio.band(src, bands),
                    dst,
                    dst_transform=from_bounds(*tile_bounds_3857(z, x, y), 256, 256),
                    dst_crs="EPSG:3857",
                    resampling=Resampling.lanczos,
                )
                if len(bands) < 4:  # no alpha band: nodata (all zero) is transparent
                    rgb = dst[:3] if len(bands) >= 3 else np.repeat(dst[:1], 3, 0)
                    alpha = (rgb.max(0, keepdims=True) > 0).astype(np.uint8) * 255
                    dst = np.concatenate([rgb, alpha], 0)
                if dst[3].any():
                    rgba[(x, y)] = np.moveaxis(dst, 0, -1)
    count = 0
    for z in range(args.zmax, args.zmin - 1, -1):
        for (x, y), t in rgba.items():
            alpha = t[..., 3:4].astype(np.float32) / 255.0
            fb = fallback_rgb(args.tiles_dir, args.fallback, z, x, y)
            base = fb.astype(np.float32) if fb is not None else np.full((256, 256, 3), 128, np.float32)
            rgb = (t[..., :3] * alpha + base * (1 - alpha)).round().astype(np.uint8)
            p = out / str(z) / str(x) / f"{y}.jpg"
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(rgb).save(p, quality=args.quality)
            count += 1
        print(f"z{z}: {len(rgba)} tiles written", flush=True)
        if z == args.zmin:
            break
        parents = {}
        for (x, y), t in rgba.items():
            P = parents.setdefault((x >> 1, y >> 1), np.zeros((512, 512, 4), np.uint8))
            P[(y & 1) * 256 : (y & 1) * 256 + 256, (x & 1) * 256 : (x & 1) * 256 + 256] = t
        rgba = {}
        for k, P in parents.items():
            f = P.astype(np.float32)
            al = f[..., 3:4] / 255.0
            pre = np.concatenate([f[..., :3] * al, f[..., 3:4]], -1)
            pre = pre.reshape(256, 2, 256, 2, 4).mean((1, 3))
            a_ = pre[..., 3:4] / 255.0
            rgb = np.where(a_ > 0, pre[..., :3] / np.maximum(a_, 1e-6), 0)
            rgba[k] = np.concatenate([rgb, pre[..., 3:4]], -1).clip(0, 255).round().astype(np.uint8)
    fill = f"transparent areas filled from {args.fallback}" if args.fallback else "no fallback"
    return {
        "layer": args.layer,
        "attribution": args.attribution,
        "zmin": args.zmin,
        "zmax": args.zmax,
        "bbox_lonlat": [lon0, lat0, lon1, lat1],
        "count": count,
        **manifest_fields(args),
        "note": f"cut from {pathlib.Path(args.src).name} by make_ortho_tiles.py; {fill}",
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        usage="%(prog)s [options] SRC.tif TILES_DIR LAYER\n"
        "       %(prog)s --manifest-only [options] TILES_DIR LAYER",
        epilog="See scripts/geo/README.md (orthophoto layers).",
    )
    ap.add_argument(
        "args",
        nargs="+",
        metavar="SRC TILES_DIR LAYER",
        help="GeoTIFF orthophoto (RGB or RGBA, any CRS; left out with --manifest-only), "
        "the console's --tiles-dir, the layer name (letters, digits, _ and -)",
    )
    ap.add_argument("--zmin", type=int, default=15, help="coarsest zoom to write (default 15)")
    ap.add_argument("--zmax", type=int, default=22, help="finest zoom, warped from the GeoTIFF (default 22)")
    ap.add_argument(
        "--fallback",
        help="layer under --tiles-dir that fills transparent pixels and is served around the ortho",
    )
    ap.add_argument("--attribution", default="", help="credit line shown in the console")
    ap.add_argument(
        "--priority",
        type=int,
        default=ORTHO_PRIORITY,
        help=f"default-layer priority; the console draws the highest one covering the map (default {ORTHO_PRIORITY})",
    )
    ap.add_argument("--quality", type=int, default=88, help="JPEG quality (default 88)")
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="only update kind/priority/fallback/attribution in an existing layer's tiles.json",
    )
    args = ap.parse_args(argv)
    want = 2 if args.manifest_only else 3
    if len(args.args) != want:
        ap.error("expected TILES_DIR LAYER" if args.manifest_only else "expected SRC TILES_DIR LAYER")
    args.src = None if args.manifest_only else args.args[0]
    args.tiles_dir, args.layer = pathlib.Path(args.args[-2]), args.args[-1]
    if not args.layer.replace("_", "").replace("-", "").isalnum():
        ap.error(f"bad layer name {args.layer!r}")
    args.tiles_dir = args.tiles_dir.expanduser()
    path = args.tiles_dir / args.layer / "tiles.json"
    if args.manifest_only:
        try:
            m = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            ap.error(f"no manifest to update at {path}: {e}")
        m.update(manifest_fields(args))
        if args.attribution:
            m["attribution"] = args.attribution
    else:
        if args.fallback and not (args.tiles_dir / args.fallback).is_dir():
            ap.error(f"fallback layer {args.fallback!r} is not under {args.tiles_dir}")
        m = cut(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, indent=2) + "\n")
    print(f"wrote {path}" + ("" if args.manifest_only else f" ({m['count']} tiles)"))


if __name__ == "__main__":
    main()
