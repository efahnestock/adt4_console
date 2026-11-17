# Geo anchoring and offline tiles

Use the spark_env python (no ROS):  `P=python3`

Anchor the Pennovation Hydra map to UTM (use `--min-status 2`: status 0 = plain GPS fixes
that are several metres off; status 2 = RTK-corrected):

    D=~/data/pennovation_2025/courtyard-alleyways
    $P anchor_from_gps.py --bag $D --topic /euclid/fix \
        --trajectory $D/output/hydra/backend/trajectory.csv \
        --out $D/output/geo_anchor.json --min-status 2

Writes geo_anchor.json, geo_anchor.png (diagnostic overlay) and geo_anchor_fixes.csv (fix cache).
Map -> UTM: `[E,N] = R(yaw) * [x,y] + t`. `z0` = median trajectory z minus 0.5 m (Spot body height).

Fetch satellite tiles (z15-19) into ~/.adt4_console/tiles/<layer>/{z}/{x}/{y}.jpg:

    $P fetch_tiles.py --anchor $D/output/geo_anchor.json \
        --trajectory $D/output/hydra/backend/trajectory.csv --margin-m 80 --zmin 15 --zmax 19   # Esri has no real z20 here (placeholder tiles)

From a GeoTIFF instead: `$P fetch_tiles.py --geotiff foo.tif --layer mysite --zmin 16 --zmax 21`.

## Orthophoto layers (e.g. flown by the quad)

A georeferenced orthophoto (GeoTIFF, RGB or RGBA, any CRS) is much sharper than the satellite
layers. `make_ortho_tiles.py` cuts it into the console's tile cache as its own layer:

    $P make_ortho_tiles.py ortho.tif ~/.adt4_console/tiles quad_site_2024 \
        --zmin 15 --zmax 22 --fallback city_aerial --attribution "Site quad ortho 2024"

* `--fallback` names a layer already under the tiles dir (e.g. one written by `fetch_tiles.py`).
  Its pixels fill the ortho's transparent areas while cutting, and the console serves its tiles
  around the flown area, so the layer is one seamless composite.
* The manifest (`tiles.json`) gets `kind: "ortho"`, `priority: 10` and `fallback`. With no layer
  picked in the browser, the console draws the highest-priority layer that covers the map, so the
  ortho becomes the default ground imagery over its site (the Layers panel can still pick another).
* `--manifest-only` updates those fields of a layer cut earlier, without the GeoTIFF:
  `$P make_ortho_tiles.py ~/.adt4_console/tiles quad_site_2024 --manifest-only --fallback city_aerial`.
* Needs rasterio, numpy and Pillow (`pip install -e .[geo]`); the console does not.
* A running console picks up a new layer and these manifest fields without a restart (reload the
  browser page).
