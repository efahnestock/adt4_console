"""Overhead PNG of a map after a mission-prep file: places, regions, objects (no mesh).

Regions are drawn as the union of discs around their places (what the region covers
on the ground), the file's new regions bold and named; added objects are stars,
deleted ones red crosses, removed (retired) places black squares with a cross.
``satellite=True`` draws in UTM over Esri World Imagery (contextily; the tiles are
cached by contextily) using the map's ``geo_anchor.json``.
"""

from __future__ import annotations

import math

PLACE_R = 1.8  # disc radius around a place when drawing a region (m)
NEW_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
              "#f032e6", "#bfef45", "#469990", "#9a6324"]  # fmt: skip
OBJECT_STYLE = {"cone": ("^", "#ff8c00")}
# where the new regions' labels go (points from the region), one per region in turn
LABEL_OFFSETS = [(-150, 70), (150, 70), (150, -70), (-150, -70), (0, 120), (0, -120),
                 (-210, 0), (210, 0)]  # fmt: skip


def _members(snap, sym):
    return [p for p, v in snap.places.items() if v.get("room") == sym]


def _blob(points, r=PLACE_R):
    from shapely.geometry import Point
    from shapely.ops import unary_union

    return unary_union([Point(x, y).buffer(r, 12) for x, y in points])


def _draw_geom(ax, geom, tf, **kw):
    from matplotlib.patches import Polygon as MplPolygon

    polys = getattr(geom, "geoms", [geom])
    for g in polys:
        if g.is_empty:
            continue
        xy = [tf(x, y) for x, y in g.exterior.coords]
        ax.add_patch(MplPolygon(xy, closed=True, **kw))


def render(before, after, report: dict, out_png: str, *, anchor=None, satellite: bool = False,
           zoom: bool = False, title: str = "", referenced: list[str] | None = None,
           dpi: int = 160) -> str:  # fmt: skip
    """Write ``out_png``; ``before`` / ``after`` are core.Snapshot (after = compiled
    result); ``zoom`` crops to the edits (+ margin) instead of the whole map."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    if satellite and anchor is None:
        raise ValueError("a satellite render needs the map's geo anchor")
    if satellite:
        c, s = math.cos(anchor.yaw), math.sin(anchor.yaw)
        te, tn = anchor.t

        def tf(x, y):
            return (c * x - s * y + te, s * x + c * y + tn)
    else:

        def tf(x, y):
            return (x, y)

    fig, ax = plt.subplots(figsize=(14, 12))
    ax.set_aspect("equal")

    # place graph
    segs, pts = [], []
    for p, v in after.places.items():
        x, y = v["xyz"][:2]
        pts.append(tf(x, y))
        for q in v["nbrs"]:
            if q > p and q in after.places:
                segs.append([tf(x, y), tf(*after.places[q]["xyz"][:2])])
    edge_c = "#ffffff" if satellite else "#9a9a9a"
    ax.add_collection(
        LineCollection(
            segs,
            colors=edge_c,
            linewidths=0.3,
            alpha=0.35 if satellite else 0.5,
            zorder=2,
        )
    )
    if pts:
        xs, ys = zip(*pts)
        ax.scatter(
            xs,
            ys,
            s=2.5,
            c="#ffffff" if satellite else "#555555",
            alpha=0.6,
            zorder=3,
            linewidths=0,
        )

    # regions: existing muted, new bold
    new = [k for k, r in after.rooms.items() if r.get("added")]
    cmap = plt.get_cmap("tab20")
    for i, (sym, r) in enumerate(sorted(after.rooms.items())):
        mem = _members(after, sym)
        if not mem:
            continue
        geom = _blob([after.places[m]["xyz"][:2] for m in mem])
        if sym in new:
            col = NEW_COLORS[new.index(sym) % len(NEW_COLORS)]
            _draw_geom(
                ax,
                geom,
                tf,
                facecolor=col,
                alpha=0.38,
                edgecolor=col,
                linewidth=2.2,
                zorder=4,
            )
            _draw_geom(
                ax,
                geom,
                tf,
                facecolor="none",
                edgecolor="black",
                linewidth=0.8,
                zorder=4.1,
            )
            if r.get("boundary"):
                bx = [tf(x, y) for x, y in r["boundary"]] + [tf(*r["boundary"][0])]
                ax.plot(*zip(*bx), "--", color=col, linewidth=1.5, zorder=4.2)
            cx, cy = geom.centroid.x, geom.centroid.y
            k = new.index(sym)
            off = LABEL_OFFSETS[k % len(LABEL_OFFSETS)]
            ax.annotate(f"{r['name']} ({sym}, {len(mem)} places)", tf(cx, cy), xytext=off,
                        textcoords="offset points", fontsize=10, weight="bold", ha="center", va="center",
                        zorder=13, color="black",
                        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=col, lw=2, alpha=0.95),
                        arrowprops=dict(arrowstyle="-", color=col, lw=2))  # fmt: skip
        else:
            col = cmap(i % 20)
            _draw_geom(ax, geom, tf, facecolor=col, alpha=0.16 if not satellite else 0.22,
                       edgecolor=col, linewidth=0.6, zorder=3.5)  # fmt: skip
            cx, cy = geom.centroid.x, geom.centroid.y
            ax.annotate(f"{r['name']}", tf(cx, cy), fontsize=6.5, ha="center", va="center", zorder=8,
                        color="white" if satellite else "#333333", alpha=0.9)  # fmt: skip

    # objects
    deleted = [k for k in before.objects if k not in after.objects]
    for sym, o in after.objects.items():
        x, y = tf(*o["xyz"][:2])
        if o.get("added"):
            ax.scatter(
                [x],
                [y],
                marker="*",
                s=420,
                c="#ff8c00",
                edgecolors="black",
                linewidths=1.2,
                zorder=10,
            )
            ax.annotate(f"{o['cls']} {sym}", (x, y), xytext=(7, 7), textcoords="offset points", fontsize=8.5,
                        weight="bold", zorder=11, color="black",
                        bbox=dict(boxstyle="round,pad=0.15", fc="#ffe0b0", ec="none", alpha=0.85))  # fmt: skip
        else:
            m, col = OBJECT_STYLE.get(o["cls"] or "", ("o", "#1f77b4"))
            ax.scatter([x], [y], marker=m, s=16 if m == "o" else 40, c=col, edgecolors="black",
                       linewidths=0.3, zorder=6, alpha=0.85)  # fmt: skip
    for sym in deleted:
        x, y = tf(*before.objects[sym]["xyz"][:2])
        ax.scatter([x], [y], marker="x", s=55, c="red", linewidths=2, zorder=9)

    # removed places
    removed = [k for k in after.retired if k not in before.retired]
    for sym in removed:
        x, y = tf(*after.retired[sym]["xyz"][:2])
        ax.scatter(
            [x],
            [y],
            marker="s",
            s=70,
            facecolors="none",
            edgecolors="black",
            linewidths=1.6,
            zorder=9,
        )
        ax.scatter([x], [y], marker="x", s=40, c="black", linewidths=1.4, zorder=9)
    restored = [k for k in before.retired if k in after.places]
    for sym in restored:
        x, y = tf(*after.places[sym]["xyz"][:2])
        ax.scatter(
            [x],
            [y],
            marker="s",
            s=70,
            facecolors="none",
            edgecolors="#00b050",
            linewidths=1.8,
            zorder=9,
        )

    # referenced place ids
    for name in referenced or []:
        p = after.place(name) or after.retired_place(name)
        if p is None:
            continue
        v = after.places.get(p) or after.retired.get(p)
        x, y = tf(*v["xyz"][:2])
        ax.annotate(p, (x, y), xytext=(4, -10), textcoords="offset points", fontsize=7.5, zorder=12,
                    color="white" if satellite else "black",
                    bbox=dict(boxstyle="round,pad=0.1", fc="black" if satellite else "white", ec="none", alpha=0.6))  # fmt: skip

    # extent
    if zoom:
        focus = []
        for st in report.get("steps", []):
            for k in ("members", "retired", "restored", "added"):
                for v in st.get(k) or []:
                    sym = v["symbol"] if isinstance(v, dict) else v
                    src = after.places.get(sym) or after.retired.get(sym) or after.objects.get(sym) \
                        or before.objects.get(sym)  # fmt: skip
                    if src:
                        focus.append(tf(*src["xyz"][:2]))
        box = focus or pts
    else:
        box = pts
    if box:
        xs, ys = zip(*box)
        m = 15 if zoom else 10
        ax.set_xlim(min(xs) - m, max(xs) + m)
        ax.set_ylim(min(ys) - m, max(ys) + m)

    if satellite:
        try:
            import contextily as cx

            cx.add_basemap(ax, crs=f"EPSG:{anchor.epsg}", source=cx.providers.Esri.WorldImagery,
                           attribution="Esri, Maxar, Earthstar Geographics", zorder=0)  # fmt: skip
        except Exception as e:  # noqa: BLE001 - offline: say so on the image
            ax.text(
                0.5,
                0.5,
                f"satellite tiles unavailable: {e}",
                transform=ax.transAxes,
                ha="center",
            )
        ax.set_xlabel(f"UTM E (EPSG:{anchor.epsg}) [m]")
        ax.set_ylabel("UTM N [m]")
        ax.ticklabel_format(useOffset=False, style="plain")
    else:
        ax.set_facecolor("#f7f7f2")
        ax.set_xlabel("map x [m]")
        ax.set_ylabel("map y [m]")
        ax.grid(True, alpha=0.25)

    # legend
    from matplotlib.lines import Line2D

    handles = [
        Line2D(
            [],
            [],
            marker="*",
            ls="",
            ms=14,
            mfc="#ff8c00",
            mec="black",
            label="added object",
        ),
        Line2D(
            [], [], marker="x", ls="", ms=8, mec="red", mew=2, label="deleted object"
        ),
        Line2D(
            [], [], marker="^", ls="", ms=7, mfc="#ff8c00", mec="black", label="cone"
        ),
        Line2D(
            [],
            [],
            marker="o",
            ls="",
            ms=5,
            mfc="#1f77b4",
            mec="black",
            label="other object",
        ),
        Line2D(
            [],
            [],
            marker="s",
            ls="",
            ms=9,
            mfc="none",
            mec="black",
            label="removed place (obstacle)",
        ),
    ]
    if restored:
        handles.append(
            Line2D(
                [],
                [],
                marker="s",
                ls="",
                ms=9,
                mfc="none",
                mec="#00b050",
                label="restored place",
            )
        )
    ax.legend(handles=handles, loc="lower left", fontsize=8, framealpha=0.9)
    ax.set_title(title or "mission prep", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=dpi)
    plt.close(fig)
    return out_png
