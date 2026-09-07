# How cartofab works

Notes on the internals: where the data comes from, what happens to it, and the
things that turned out not to be true. Most of these sections exist because
something went wrong and the fix is not obvious from the code alone.

To install and use it, see the [README](../README.md).

## Elevation sources

| Source | Where | Resolution | Notes |
|---|---|---|---|
| **Scottish LiDAR** | Scotland, **partial** | 0.25–1 m | COGs on AWS Open Data, read by range request |
| **IGN RGE ALTI** | France + DOM | 1–5 m | Géoplateforme WMS, float32 GeoTIFF, native Lambert-93 |
| **TINITALY 1.1** | Italy | 10 m | INGV WCS, float32, CC-BY 4.0 |
| **AWS Terrarium** | global | ~30 m | carries bathymetry, but see below — the 0 m contour is *not* reliably a coastline |

Sources are **mosaicked**, best resolution first, each filling only what the
previous ones left empty. A capture on the French/Italian border gets IGN on one
side and TINITALY on the other, in one seamless grid. The **Result** panel lists
which sources actually contributed.

Each is clipped to a real territorial polygon (`server/data/coverage.json`,
simplified Natural Earth). This matters more than it sounds: IGN's WMS answers
*outside* France with a very coarse fill rather than nodata, so without the clip
it would silently win priority over TINITALY in Italy, or over the global tiles
in Switzerland, and you would get blocky ~25 m terrain while being told it was
5 m LiDAR. That is exactly the artefact this app was reported for.

The **Sampling** field is the distance between elevation samples in metres — 5 m
over French mountains is where the wiggly lines come from; drop it to 1 m for
small areas if you want everything the dataset has.

### The global tiles have corrupt pixels

Rare, isolated, and disproportionately destructive. A single pixel comes back
with its red channel eight low — exactly 2048 m — so it reads as -1880 m in the
middle of ground that is 6 m all around it. There were 23 in the mosaic at
Croabh Haven, 91 at the Cuillin, 114 at Glencoe, and none at all at Ben Nevis,
Arthur's Seat, Leith or Mont Blanc.

One is enough to ruin a model, because a model's height is its maximum minus its
minimum: at Croabh Haven that turned a 143 m coastline into a 1602 m range, and
a 27 mm model into a 268 mm one — taller than it was wide. Any that fall below
the sea threshold also become their own little rectangular "lakes" on the
seabed.

They are rejected by disagreement rather than by value: a cell further than
60 m from *every* one of its eight neighbours is not terrain, because real
terrain always continues into at least one neighbour — a sea cliff is safe,
since its neighbours along the cliff share its height. A second test catches
the pairs, where each spike is the other's alibi: further than 250 m from the
neighbourhood median. Across seven test regions no maximum moved by a
millimetre, and at Glencoe 760 of the 811 corrections were on cells reading
below -1000 m.

### How fine is worth sampling

The **Sampling** box measures what is actually available and offers it as a
button. It reads the real tile listings rather than the territorial polygons, so
it will not promise LiDAR that was never flown: at Croabh Haven one phase-1 tile
clips 13% of the frame and contributes nothing, and the honest answer is 30 m,
not the 2 m the polygon implies. A source covering less than 60% of the frame is
reported as a partial contributor rather than as the limit.

Going finer than the source **does change the drawing, but adds no
information**. Sampling Croabh Haven's 30 m data at 5 m gives:

```
             lines   vertices   total length
30 m (native)   49      2,173       61.0 km
 5 m            68      3,685       79.2 km     +39% lines, +30% length
```

Most of that is not detail but smoothing: **terrain blur is set in pixels**, so
finer sampling silently means less blur on the ground. Holding the blur constant
at 30 m of ground, the same comparison is 52 lines and +11% length, with a
median shift of 9 m — well inside one source cell. What remains is the finer
grid tracing the curve of the bilinear surface where the coarse one cuts a chord
across the cell. Smoother lines of the same terrain, for 143× the samples.

So: sample at the figure the box gives you. If you want smoother contours, that
is what **Terrain blur** and **Curve** are for.

### The sea is not at zero

Over water these tiles are radar noise, not a flat plane. At Croabh Haven the
surface reads anywhere from 0.5 m to 8 m, so **treat missing data as sea** at
0 m finds 0.03% of the map — a few square metres — while the sea area just
grows smoothly with the threshold, with no plateau to lock on to:

```
level 0.0 m   0.03% of map        level 2.0 m    8.22%
level 1.0 m   2.29%               level 4.0 m   22.06%
```

There is no right answer to detect, so the app says what it found and suggests
a level that would work rather than silently handing back a model with no sea.

**Sea from → OSM coastline** avoids the question entirely. The coastline is a
surveyed line at full detail, and it carries its own orientation: land to the
left of the way, sea to the right. Closing it against the frame turns it into a
polygon, with islands as holes because they are their own closed ways. At Croabh
Haven that gives one polygon with 13 island holes covering 66% of the frame,
against 0.03% from the 0 m threshold, and its boundary lies on the coastline to
within 0 mm.

It also shows how wrong the elevation data is about water here: the global tiles
call 24% of that sea "land", by a median of 183 m inland. Since the sea is
flattened to one level, you are warned when they disagree — that ground becomes
a cliff at the shoreline. The coastline layer is fetched automatically when this
option is chosen.

**It needs the coast to cross the frame.** Where the coastline only clips a
corner, the orientation vote can settle on the wrong side: at Glencoe that made
91% of a mountain valley into sea at 189 m, and Leith came out inverted too. Two
ways of repairing that from the terrain — flipping the two sides, and judging
each face by its own median height — each fixed those cases while breaking
Croabh Haven, which was verified correct. So it does not guess: when the side it
marks as water sits higher than the other, it draws nothing and tells you why.
Widen the frame so the coast runs across it, or use the elevation threshold.

### Reading the coastline wider than the map

Orientation is the only thing that knows which side of a coastline is the sea,
and it is exact where the line properly divides the frame. Two things break that
on a small capture: the coast merely clips a corner, so the vote settles on the
wrong side; or the box sits entirely in the water and contains no coastline at
all, so there is nothing to vote on and nothing is drawn.

Both are fixed by giving the line more room. The coastline is fetched over a box
three times the width of the map — a concentric region shares the page frame
exactly, so no coordinates have to be transformed — the sea is worked out there,
and the answer is intersected back to the region. Measured over eighteen small
captures around the Croabh Haven coast, at 600 m and 1200 m square:

```
fixed 3    unchanged 15    broken 0
```

All three that changed were squares lying out in the bay, which previously
returned nothing and now correctly come back as 100% sea. Layers arrive clipped
to the region, so this needs its own fetch; it asks for the coastline alone,
which is small, and the span is capped so a tiny capture cannot turn into a huge
Overpass query.

It does not rescue every case. Where the coast clips a corner *and* the terrain
disagrees, the sanity check still refuses rather than handing back a confidently
wrong sea — Glencoe is unchanged.

### Water meeting at a point

Two water bodies that touch at a single corner are perfectly legal geometry —
a valid MultiPolygon, and shapely will not dissolve them — but extruding both
puts four wall faces on one vertical edge. That single non-manifold edge used
to throw the entire smooth shoreline away and fall back to the stepped grid
slab, which is the one thing the polygon path exists to avoid. At Croabh Haven
it cost a 37-part coastline for *one* bad edge.

Only the parts that actually touch are nudged apart, by twenty times the weld
tolerance — 0.002 mm on the model at any scale, and 0.004% of the water area.
Eroding every part instead, which is the obvious fix, loses sixteen times as
much area and *creates* thousands of bad edges of its own, because buffering
re-vertexes each boundary into fresh coincidences.

### Scottish LiDAR coverage is patchy

Worth knowing before you plan a trip around it. The Scottish Public Sector
LiDAR programmes were flown over selected areas, not the whole country, and
they largely **miss the big hill country**:

| Covered | Not covered |
|---|---|
| Glencoe, Arran, Orkney, Outer Hebrides, central belt, SW Scotland | Skye, Torridon, the Cairngorms, Assynt, Galloway hills; Ben Nevis only clips a corner |

Where it exists it is superb — 0.5 m, with individual gullies, field walls and
tracks visible. Where it does not, the mosaic falls through to the global tiles
and you get a **hard seam** between 0.5 m and ~30 m data. The app warns when
the best source covers less than 98% of your area, and the Result panel gives
the exact share per source, so you can nudge the region to sit inside coverage.

The portal's WMS only renders styled images and its WCS is switched off, so
this source reads the Cloud-Optimised GeoTIFFs on AWS directly, picking the
overview level that matches your sampling — typically ~1 MB out of an 18 MB
tile.

### Adding a country

Most of Europe has better data than the global fallback, but there is no single
pan-European elevation service, so each country is its own integration. To add
one, write a `fetch_*` returning a `Grid` in the source's **native** projection
and resolution, add its polygon to `coverage.json`, and add a row to `SOURCES`
in `server/dem.py`. The mosaic and the UI pick it up automatically.

Worth knowing before you do: Spain (PNOA), Austria, Germany (per-Bundesland),
Belgium/Flanders, Estonia and Norway publish 1 m LiDAR; the Netherlands (AHN)
and Scotland 0.5 m; Denmark 0.4 m. Switzerland's swissALTI3D is 0.5 m but is
distributed as per-km² tiles through a STAC API rather than a coverage service,
so it is more work than IGN or TINITALY were. Most national portals are
downloads rather than WMS/WCS, which is the real obstacle.

## Why the lines were striped

Worth recording, because it looked like corrupted terrain rather than a bug.

Requesting an arbitrary Web-Mercator grid from a WMS makes the *server* resample
from its native grid, and the IGN one appears to do that with a nearest-neighbour
kernel. Rows land slightly off the source grid, some get duplicated, and the
result is a moire of horizontal stripes that survives contouring — strongest on
steep ground, and present in France too, not just across the border.

Every source is therefore now requested in its **own** projection at its **own**
grid resolution, snapped to a multiple of that grid, and resampled exactly once —
here, with bilinear interpolation — onto the output grid. The stripes are gone.
If you add a source, keep to that rule.

## Aspect ratio and projection

The capture area is defined in **true ground metres** about its centre, and
rendered through a transverse-Mercator projection re-centred on that point. So
"1 : 1" is an honest square kilometre-wise, not a square on screen. This is why
the blue outline on the map looks taller than it is wide at northern latitudes —
that is correct, and it is what Web-Mercator-based tools get wrong.

Rotation is supported; the page is always axis-aligned, and the terrain rotates
inside it. Internally everything downstream of the projection works in the
*page frame* — the capture rectangle un-rotated — so rotation exists in exactly
one place (`server/geo.py`) and nothing else has to think about it.

Drag the square's **corner or edge handles** on the map to resize it. The
opposite corner (or edge) stays anchored, so you can drop one corner and pull
the other out to frame the area — the centre moves to suit. Hold **⌥ Option**
to resize about the centre instead, keeping the centre marker fixed.

With an aspect preset selected the ratio is held whichever handle you pull, so
the handle tracks the wider of the two directions rather than the pointer
exactly; choose `custom…` to set width and height independently.

## Smoothing

Three independent controls, applied in this order:

- **Terrain blur** — a gaussian over the elevation raster *before* contouring.
  This is the one that matters. It removes stair-stepping at source rather than
  averaging it out afterwards. `1–2` for IGN, `2–4` for the global DEM.
- **Simplify** — Douglas–Peucker tolerance in ground metres. Cuts point count
  hard with little visible change; keeps SVG files small and plots faster.
- **Curve** — Chaikin corner-cutting iterations on the final polylines. Takes
  the last edge off. `1` is usually enough.

**Drop under** discards contour fragments shorter than N ground metres, which
removes the confetti of tiny closed rings around noisy summits.

**Index every Nth** contour goes to its own heavier layer for a pen change.
Untick it to get a single uniform contour layer.

## Layers

Every layer becomes a top-level `<g inkscape:groupmode="layer">`, which is what
both Inkscape and `vpype read` use to split an SVG into separate layers — one
vpype layer per group, ready for a pen change.

`contours`, `contours-index` (every Nth, heavier), `sea-fill`, `water-fill`
(hatching), `glaciers`, `sea`, `water`, `rivers`, `coastline`, `buildings`,
`railways`, `roads`, `paths`, `trip`, `peaks`, `places`, `labels`, `frame`.

Vector layers come from OpenStreetMap. They are optional and never block the
contours: if the data is unavailable the map still renders, with a warning.
Each layer gets its own time budget, dead mirrors are skipped for a while after
they fail, and a query that failed everywhere is remembered for 90 seconds so
pressing Generate again does not re-wait the whole timeout.
See **OSM data** below for where they come from.

## OSM data

Two sources, chosen in the **OSM data** panel.

**Overpass** is the public OpenStreetMap query service. Nothing to install, but
the public instances are frequently busy — and worst exactly when you are
exploring somewhere new, because that is where the cache cannot help. Responses
*are* cached, per layer, keyed on a bbox rounded to about 11 m, so only the
first look at an area can be slow.

**A local extract** is a one-off download of a region from Geofabrik, imported
into a small SQLite store (geometry as WKB, indexed by an R\*Tree) and then
queried instantly, offline, forever. Same tags, same geometry, same layers as
Overpass — it is the same data.

The download is deliberately a conscious act. When Overpass fails on an area
with no local coverage, the panel offers the extracts that contain it, smallest
first, with sizes:

```
Overpass is unavailable for this area.
  Scotland          325 MB   [download]
  United Kingdom    2.2 GB   [download]
```

Once installed it becomes the default for that area — complete and instant,
about 0.25 s against 6 s or more of waiting on a busy Overpass. Pick **Overpass**
explicitly if an area has changed since you downloaded it.

When you download, two options keep the store manageable:

- **Include buildings** (off by default). Buildings are usually more than half
  of everything in an extract — Luxembourg imports at 97 MB with them and
  43.5 MB without.
- **Only within N km of here** (on, 40 km). Clips the import to the area you
  actually work in and records that box as the store's coverage, so it never
  claims ground it does not hold.

Neither saves much *time*: the import is dominated by reading the file, so a
big region takes minutes either way (Rhône-Alpes is a 570 MB download and
around ten minutes). They save a great deal of space.

- **Keep the source file** (off by default). Normally the `.pbf` is deleted
  after import. Keeping it costs its download size on disk but makes any later
  widening free — no download at all.

If a store was imported without a layer you later ask for, the app says so
rather than quietly drawing nothing.

### Widening a clipped store

Clipping is the right default and the wrong one to be stuck with: a 325 MB
Scotland download clipped to 40 km around Edinburgh has nothing at Stirling,
40 km further on. That is not the same as having no data, and the app no longer
pretends it is. It knows both boxes — the clip it imported and the full extent
of the extract it came from — so it can tell you which situation you are in:

```
Scotland is installed but was cut down to a smaller area,
so it has nothing here — using Overpass   [extend it]
```

Extending re-imports the same extract with the clip widened to the union of the
old box and the new area, keeping every layer the store already had. Nothing you
already had is lost. If the source file was kept it re-imports straight away;
otherwise it downloads once more first.

The importer counts the file before it starts, so the progress bar has a real
target rather than a guess. Counting is a cheap pass — about 4% of the import —
and lands within about 1%, where estimating from file size is out by up to 82%
because the tagged fraction varies enormously between a city and a mountain
range.

Two things worth knowing:

- The store ends up **roughly twice the size of the `.pbf`** when imported in
  full, because WKB
  geometry is uncompressed where the pbf is delta-encoded (zlib on the blobs
  only reaches 0.84×, so it is not worth the cost). The `.pbf` is deleted after
  import unless you keep it, so that is the total, not the peak — peak is both
  at once.
- Extracts are a snapshot. Nothing updates them; remove and re-download if you
  need current data.

Stores live in `osmdata/`, deliberately outside `cache/` so that clearing the
cache never destroys a 300 MB download. Each has its own **remove** button.

## 3D meshes

The **3D mesh** output builds a printable solid from the same elevation grid the
contours come from — collection is identical, only the rendering differs.

The terrain becomes a watertight solid: top surface, four walls dropped to a
base plane, flat bottom. Every edge is checked to be shared by exactly two
faces, and the Result panel reports whether it came out closed.

**Mesh resolution is separate from elevation sampling**, and it has to be. A
1600 × 1600 capture meshed at full detail is 5.1 M triangles and a 256 MB STL.
Triangles come out at about `2 × (n−1)²` plus ~1% for walls and base, and a
binary STL is 50 bytes each — the figure under the slider is the real
prediction, within about half a percent.

**Vertical exaggeration** defaults to 3×. At 1× a model is geometrically honest
and looks disappointingly flat, because terrain is far wider than it is tall.
Watch the model height for high-relief ground: Mont Blanc over 8 km at 3× is
223 mm tall on a 180 mm wide base.

**Water is its own object**, so it can take its own colour, with a thickness
you set in whole layers. Each body is flattened to its own level — one level
shared across every lake sinks the high ones, which at Arthur's Seat punched a
71 m hole through Dunsapie Loch. The terrain underneath is recessed by the slab
thickness, so the surface still sits at the true water level.

The slab is built from the water polygon, so the shoreline keeps its shape.
Where an outline is too tangled to build cleanly — the sea traced from an
elevation grid has hundreds of island rings — it falls back to rasterising on
the mesh grid and says so; that shoreline looks stepped. Loch Lomond's lakes
come out at 3,720 triangles from their polygons against 254,936 rasterised.

**Not everything tagged as water is a lake.** A burn falling down a hillside
spans a large height range, and levelling it to one height carves a hole at the
top and leaves a ridge at the bottom. A body counts as a lake when the terrain
under it falls by less than **2% of its own length**; anything steeper keeps the
shape of the ground and is draped like a road. On real ground that separates
cleanly — Arthur's Seat lochs sit at 0.001–0.011 and Loch Lomond at 0.001–0.031,
against 0.021–0.142 for streams on the Cuillin — and the threshold is adjustable.

Bodies too small to cover three mesh vertices are left as terrain and reported,
because a sub-cell pond otherwise drags one arbitrary vertex to a wrong height.

**Max height** caps the finished model. When the exaggeration you asked for
would overshoot, it is reduced just enough to fit and the Result panel shows
both figures. The budget accounts for the base, the backplate, the water
recess and whichever of buildings or roads stands tallest.

**Model size** is set in the Output panel as width × height, with a lock that
keeps the capture's aspect. Open the lock to set both independently, which
stretches the ground.

**A backplate** is a thin plate under the whole model, larger than the terrain,
for tucking under the border of a deep frame. Give the size you want; the model
is centred on it and the terrain sits exactly on top rather than sinking in. The lake level is the median of the terrain underneath, which ignores
the shoreline vertices that catch the bank — at Loch Lomond that turns a
7.5–373.8 m spread into exactly 8.0 m. The shoreline is only as crisp as the
mesh resolution, so raise it if the coast matters.

Ticking **buildings** or **roads** for the mesh pulls in the layer it needs, so
they can no longer come out silently empty because the matching Layers chip was
off. Roads means roads: tick the **paths** layer as well if you want footpaths,
which in somewhere like Holyrood Park triples the triangle count.

**Buildings and roads** are separate objects standing on the terrain, not fused
into it — which is what lets a slicer treat them as distinct bodies you can
paint. Heights are yours to set, since OSM rarely carries usable ones. Road
widths come from the highway class, from motorway 14 m down to footpath 1.5 m.

Both are **draped**: every vertex takes its own ground height, so a road
follows the hillside instead of sitting at the lowest point under the whole
network. Buildings drape only underneath — the base follows the ground like a
foundation while the roof stays level, which is how a building actually sits on
a slope. Touching buildings are merged first, because two prisms meeting on a
shared terrace wall weld into an edge with four faces, which is non-manifold.

**Minimum feature size** is your nozzle. Buildings with a footprint smaller
than it are dropped and counted in the warnings, roads narrower than it are
widened to it, and you get told when the mesh grid is finer than the nozzle can
resolve — detail you pay for in file size and can never see.

| Format | What you get |
|---|---|
| **3MF** | separate named objects with colours; what slicers built around it (Bambu Studio especially) read most reliably — try this first |
| **OBJ + MTL** (zip) | the same objects and colours, more universal, but some slicers flatten it to one uncoloured body |
| **STL** | one solid, no colour, no names — fine for terrain alone |

Every object is checked for watertightness the way a slicer reads it — welded
by position, not by vertex index — and the Result panel reports the result per
object. Triangulation is ear clipping, which respects concavity and holes
exactly; a Delaunay-and-filter approach is wrong on 13 of 124 real building
footprints and 2.7% of road area, and those errors are exactly the open edges a
slicer complains about.

### Smoothing a mesh is not smoothing a contour

Two different problems, so two different controls. Contour blur is in pixels and
runs before marching squares, where the noise that matters is stair-stepping in
the line. The mesh reads the elevation grid directly and its problem is
different: bilinear sampling is continuous but not smooth, so once the mesh grid
is finer than the source, every source cell shows up as a flat facet with a
crease around it — visible as blockiness on a printed model, worst where the data
is coarse.

Mesh smoothing is therefore specified in **ground metres**, not pixels, so it
means the same thing at any mesh resolution. Measured at Croabh Haven, 30 m data
meshed at 400 across 3.59 km (9.0 m cells), scoring faceting as the mean absolute
second difference across a row:

```
smoothing   faceting   model height
     0 m      0.0849       26.8 mm
    10 m      0.0669  79%  26.5 mm
    20 m      0.0478  56%  26.4 mm
    40 m      0.0303  36%  26.1 mm
    80 m      0.0170  20%  25.2 mm
```

Twenty metres halves the faceting and costs 1.5% of the relief. It runs before
the water is flattened, because water levels are taken from this surface and the
terrain is recessed to match — blurring afterwards would round the water off and
lift the recess back through its own slab. Below about 0.8 of a cell the box-blur
approximation rounds its radius to zero, so anything smaller is reported as
having had no effect rather than silently doing nothing.

## Water

Lakes and the sea can be more than an outline.

**Fill** hatches them — one set of parallel lines, or two at right angles for
cross-hatch. Spacing is given **in millimetres on the page**, not in ground
metres, so the texture looks the same whatever scale you capture at; 1–2 mm
reads as water without flooding the drawing. Islands come out as holes rather
than being hatched over.

**Mask beneath** removes contour and river lines that fall inside water. This
matters more than it sounds: elevation models record a lake as a flat surface
and OSM maps rivers straight through them, so without it the pen draws lake-bed
contours and runs rivers across open water. Roads and paths are deliberately
left alone — those are bridges and causeways. On a 9 km capture of Loch Lomond
this removes 15.5 km of contour and 0.3 km of river from inside the loch.

**Include the sea** derives water from the elevation data rather than OSM, so
coastlines can be hatched and masked like a lake. It needs elevation that
covers the water: the global DEM has real bathymetry, and with IGN you want
*treat missing data as sea* on as well.

Water arrives as real polygons — closed ways, and multipolygon relations
assembled from their member ways — so a loch with fifty islands hatches
correctly rather than as fifty separate outlines.

### Trips

Click — or click and drag along — segments in the preview to move them into a
separate `trip` layer. Selection ids are OSM way ids, so they survive changing
the interval, resolution or page size.

**Trip selection** controls which layers respond to clicking. Paths and roads
are on by default; add rivers when you want to trace a paddle, or railways.
Turning a layer off there stops it reacting to clicks without hiding it.

## Plotting

The SVG has real `mm` dimensions and a matching viewBox, so it arrives at the
right physical size:

```bash
vpype read contours.svg linemerge --tolerance 0.2mm linesort layout -m 10mm a3 write plot.svg
```

The **Result** panel reports pen distance per layer, so you know what you are
committing to before you start.

## Coastlines

IGN's grid simply stops at the shoreline, so French coastal captures leave the
sea blank. Two options, which combine well:

- tick **coastline** to draw the OSM coastline as its own layer;
- tick **treat missing data as sea**, which fills nodata with −1 m so the 0 m
  contour closes around the coast.

Outside France the global DEM has real bathymetry and neither is needed — though
the coastline layer is still a cleaner line than a 0 m contour.

## Cache

Every elevation tile and Overpass response is cached under `cache/`, including
the decoded Scottish LiDAR tiles — which is why the second render of an area is
near-instant instead of refetching tens of MB.

It grows, and sub-metre data grows it fast: a session at 0.5 m over Edinburgh
can reach a couple of GB, because the cache key includes the sampling distance,
so 0.25 m and 0.5 m of the same ground are stored separately. The **Cache**
panel shows the size and clears it in one click — everything removed is simply
refetched on demand, so clearing is always safe.

Sampling finer than the source does not help: the app tells you the resolution
of the data actually used (`100% @ 0.5 m` in the Result panel) and warns if you
ask for finer than that. At Arthur's Seat, 0.25 m produces byte-for-byte the
same contours as 0.5 m for four times the pixels and three times the wait.

## Notes

- The Result panel breaks down where the time went (elevation, contours, OSM,
  water, svg) — useful when a render feels slow.
- Every setting that is not self-evident has a **?** next to it explaining what
  it does and what values are sensible.
- Sampling accepts values down to 0.25 m for the Scottish LiDAR. Guard rails
  (60 Mpx of elevation, 2000 contour levels) report as plain sentences naming
  the field, not raw validation objects.
- Exports are also written to `exports/`. Disposable; delete freely.
- Guard rails: 60 Mpx of elevation and 2000 contour levels per request. Both
  produce a clear error rather than hanging.
- Settings persist in the browser's localStorage.
- IGN encodes nodata as −99999 but its WMS *resamples before serving*, smearing
  interpolated garbage along every land/sea edge. `server/dem.py` masks below
  −15 m and erodes the valid mask by a pixel to remove it. If you ever see
  contours at implausible depths, that is the knob.
- Overpass mirrors sometimes answer 200 with valid JSON from an empty database.
  `server/osm.py` validates `osm3s.timestamp_osm_base` before trusting or
  caching a response.

## Where the code lives

```
server/  geo.py       projections, the Region model, the page frame
         cog.py       range-request reader for remote Cloud-Optimised GeoTIFFs
         dem.py       source registry, native-grid fetching, mosaicking
         data/        simplified territorial polygons per source
         contours.py  blur, marching squares, clip, simplify, Chaikin
         osm.py       Overpass queries, mirror failover, polygon assembly
         localosm.py  Geofabrik extracts: download, import, extend, query
         water.py     sea from the DEM, hatching, masking
         mesh.py      heightfield, watertight solid, extrusions, STL/OBJ
         svg.py       layered millimetre SVG writer
         main.py      FastAPI endpoints
web/     index.html, app.js, style.css, viewer.js (WebGL mesh preview)
```
