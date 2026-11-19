"""Mission-prep edit files: resolve an ``intent`` against a map and compile it to
heracles ``graph_edit`` ops (applied as ONE edit: one transaction, one map-version
bump, one undo entry).

A file (``FORMAT``)::

    {"format": "adt4-mission-prep/1", "name": "mission1", "summary": "...",
     "map": {"prior": "penn2026_trav_ganav_regions"},
     "intent": [ {"do": ...}, ... ],          # what was asked; re-resolved at load
     "ops": [ ... ], "report": {...}}          # compiled when the file was made (review)

Intent steps (place symbols are matched ignoring case: ``t5840`` = ``T5840``):

* ``{"do": "delete_objects", "class": "cone"}`` -- every object of a class; or
  ``"symbols": [...]``; or ``"class"`` + ``"near": {"place": "T1", "radius": 3}``.
* ``{"do": "add_objects", "class": "cone", "at": ["T5840", ...], "size"?: 0.5}`` --
  one object on each place (at its center), attached to it (place CONTAINS object).
* ``{"do": "retire_places", "places": [...]}`` -- take places out of the map (an
  obstacle): ``retire_node``, which the planner never sees; ``restore_places`` undoes it.
* ``{"do": "restore_places", "places": [...] | "all"}`` -- ``"all"`` puts back every
  removed place, whichever file removed it (a generic "remove obstacles" file).
* ``{"do": "add_region", "name": "base camp", "class"?: "base camp",
  "places"?: [...], "polygon_from"?: [...], "expand"?: "adjacent"}`` -- a new region
  (Room).  ``places`` (+ their 1-hop neighbours with ``expand: adjacent``) or the
  places inside the convex hull of ``polygon_from``.  The region TAKES its places:
  a place has one parent region (the planner reads one), so each member leaves its
  old region.  Names are unique ignoring case.
* ``{"do": "add_to_region", "name": "...", "places": [...], "expand"?: "adjacent"}``
* ``{"do": "rename_region", "name": "...", "to": "..."}``
* ``{"do": "delete_region", "name": "..."}`` -- the region goes, its places become
  unassigned.

Loading always re-resolves ``intent`` against the live map (a snapshot is a few
read queries) so a file stays correct after other edits; the compiled ``ops`` in the
file are what was reviewed and are compared with the live result.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field

FORMAT = "adt4-mission-prep/1"
PLACE = "MeshPlace"
OBJECT_Z = 0.35  # a new object's center above its place's center (a cone ~0.7 m)
OBJECT_SIZE = 0.5
STEPS = {
    "delete_objects", "add_objects", "retire_places", "restore_places", "add_region",
    "add_to_region", "rename_region", "delete_region",
}  # fmt: skip


class PrepError(Exception):
    """The file is invalid or cannot be resolved against the map."""


# ------------------------------------------------------------------ snapshot
@dataclass
class Snapshot:
    """What the compiler needs from a map (no mesh, no images)."""

    places: dict = field(default_factory=dict)  # sym -> {xyz, room, nbrs:set}
    retired: dict = field(default_factory=dict)  # sym -> {xyz, label}
    rooms: dict = field(default_factory=dict)  # sym -> {name, cls, xyz, boundary}
    objects: dict = field(default_factory=dict)  # sym -> {cls, xyz, places:list}
    room_classes: list = field(default_factory=list)
    used_symbols: set = field(
        default_factory=set
    )  # every map-node symbol (incl. retired)
    epoch: str | None = None
    version: int | None = None
    prior_path: str | None = None

    def place(self, s: str) -> str | None:
        """The stored symbol of an active place, matching ignoring case."""
        return self._ci(self.places).get(s.strip().lower())

    def retired_place(self, s: str) -> str | None:
        return self._ci(self.retired).get(s.strip().lower())

    @staticmethod
    def _ci(d):
        return {k.lower(): k for k in d}

    def room_by_name(self, name: str) -> str | None:
        n = name.strip().lower()
        for k, r in self.rooms.items():
            if (r.get("name") or "").strip().lower() == n:
                return k
        return None

    @property
    def prior(self) -> str | None:
        return prior_name(self.prior_path)

    def copy(self) -> Snapshot:
        return copy.deepcopy(self)


def prior_name(prior_path: str | None) -> str | None:
    """``.../<run>/hydra/backend/dsg.json`` -> ``<run>`` (a copied ``prior_<run>`` too)."""
    if not prior_path:
        return None
    parts = re.split(r"[\\/]+", str(prior_path).rstrip("/"))
    if len(parts) >= 4 and parts[-3:-1] == ["hydra", "backend"]:
        name = parts[-4]
    else:
        name = parts[-1]
    return name.removeprefix("prior_")


def _xyz(p) -> list[float]:
    if p is None:
        return [0.0, 0.0, 0.0]
    if hasattr(p, "x"):
        return [float(p.x), float(p.y), float(getattr(p, "z", 0.0) or 0.0)]
    p = list(p)
    return [float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0]


def snapshot_from_neo4j(db, database: str = "neo4j") -> Snapshot:
    """One read session: places (+ parent region, neighbours), retired places,
    regions, objects (+ the places holding them), the room labelspace, _MapState."""
    driver = getattr(db, "driver", None) or db
    s = Snapshot()
    try:  # no "label does not exist" notices for an empty Retired set etc.
        ses_cm = driver.session(database=database, default_access_mode="READ",
                                notifications_min_severity="OFF")  # fmt: skip
    except TypeError:  # older neo4j driver
        ses_cm = driver.session(database=database, default_access_mode="READ")
    with ses_cm as ses:
        for r in ses.run(
            f"MATCH (p:{PLACE}) OPTIONAL MATCH (r:Room)-[:CONTAINS]->(p) "
            "RETURN p.nodeSymbol AS s, p.center AS c, collect(r.nodeSymbol) AS rooms"
        ):
            rooms = [x for x in r["rooms"] if x]
            s.places[r["s"]] = {"xyz": _xyz(r["c"]), "room": rooms[0] if rooms else None,
                                "rooms": rooms, "nbrs": set()}  # fmt: skip
        for r in ses.run(
            f"MATCH (a:{PLACE})-[:MESH_PLACE_CONNECTED]-(b:{PLACE}) "
            "RETURN a.nodeSymbol AS a, b.nodeSymbol AS b"
        ):
            if r["a"] in s.places and r["b"] in s.places and r["a"] != r["b"]:
                s.places[r["a"]]["nbrs"].add(r["b"])
        for r in ses.run(
            "MATCH (n:Retired) RETURN n.nodeSymbol AS s, n.center AS c, n.retired_label AS l"
        ):
            s.retired[r["s"]] = {"xyz": _xyz(r["c"]), "label": r["l"]}
        for r in ses.run(
            "MATCH (r:Room) RETURN r.nodeSymbol AS s, r.name AS n, r.class AS c, "
            "r.center AS x, r.boundary AS b"
        ):
            s.rooms[r["s"]] = {"name": r["n"] or r["c"] or r["s"], "cls": r["c"], "xyz": _xyz(r["x"]),
                               "boundary": [_xyz(p)[:2] for p in (r["b"] or [])] or None}  # fmt: skip
        for r in ses.run(
            "MATCH (o:Object) OPTIONAL MATCH (p)-[:CONTAINS]->(o) WHERE p:MeshPlace "
            "RETURN o.nodeSymbol AS s, o.class AS c, o.center AS x, collect(p.nodeSymbol) AS ps"
        ):
            s.objects[r["s"]] = {
                "cls": r["c"],
                "xyz": _xyz(r["x"]),
                "places": [p for p in r["ps"] if p],
            }
        rec = ses.run(
            "MATCH (l:_Labelspace {layer: 'room'}) RETURN l.names AS n"
        ).single()  # fmt: skip
        s.room_classes = list(rec["n"]) if rec and rec["n"] else []
        rec = ses.run(
            "MATCH (m:_MapState) RETURN m.epoch AS e, m.version AS v, m.prior_path AS p"
        ).single()
        if rec:
            s.epoch, s.version, s.prior_path = rec["e"], rec["v"], rec["p"]
        for r in ses.run(
            "MATCH (n) WHERE (n:Object OR n:Room OR n:Building OR n:Place OR n:Retired) "
            "AND n.nodeSymbol IS NOT NULL RETURN n.nodeSymbol AS s"
        ):
            s.used_symbols.add(r["s"])
    s.used_symbols |= set(s.places)
    return s


# ------------------------------------------------------------------ geometry
def convex_hull(pts: list[tuple[float, float]]) -> list[list[float]]:
    pts = sorted(set((float(x), float(y)) for x, y in pts))
    if len(pts) <= 2:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return [list(p) for p in lo[:-1] + hi[:-1]]


def point_in_polygon(x: float, y: float, poly) -> bool:
    """Even-odd rule; a point on an edge counts as inside."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i][:2]
        x2, y2 = poly[(i + 1) % n][:2]
        # on the segment
        if (
            min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
            and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
            and abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) < 1e-9
        ):
            return True
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


# ------------------------------------------------------------------ compile
class _Compiler:
    def __init__(self, snap: Snapshot):
        self.s = snap.copy()  # the map as the steps so far leave it
        self.ops: list[dict] = []
        self.report: dict = {"steps": [], "unresolved": [], "warnings": []}
        self.counters: dict[str, int] = {}
        # places a region made by THIS file holds: an expansion (adjacent ring, polygon
        # interior) never takes them; a place named explicitly always moves
        self.claimed: set[str] = set()

    # -- helpers
    def new_symbol(self, prefix: str) -> str:
        if prefix not in self.counters:
            nums = [int(m.group(1)) for x in self.s.used_symbols
                    if (m := re.fullmatch(rf"{prefix}\(?(\d+)\)?", x))]  # fmt: skip
            self.counters[prefix] = max(nums, default=-1)
        self.counters[prefix] += 1
        sym = f"{prefix}{self.counters[prefix]}"
        self.s.used_symbols.add(sym)
        return sym

    def places(self, names, step, *, retired_ok=False) -> list[str]:
        out = []
        for n in names or []:
            p = self.s.place(str(n))
            if p is None:
                if self.s.retired_place(str(n)) and not retired_ok:
                    self.report["unresolved"].append(
                        f"{n}: removed (retired) place ({step})"
                    )
                else:
                    self.report["unresolved"].append(f"{n}: no such place ({step})")
                continue
            out.append(p)
        return out

    def expand(self, places, how) -> list[str]:
        """``places`` + (``adjacent``) their 1-hop neighbours, minus neighbours a
        region of this file already holds."""
        if not how or how == "none":
            return list(dict.fromkeys(places))
        if how != "adjacent":
            raise PrepError(f"expand must be 'adjacent' or 'none', not {how!r}")
        out = list(places)
        for p in places:
            out += [q for q in sorted(self.s.places[p]["nbrs"], key=_symkey)
                    if q not in self.claimed]  # fmt: skip
        return list(dict.fromkeys(x for x in out if x in self.s.places))

    def region(self, name, step) -> str | None:
        r = self.s.room_by_name(name)
        if r is None:
            self.report["unresolved"].append(
                f"region {name!r}: no such region ({step})"
            )
        return r

    def take(self, room: str, members: list[str]) -> dict:
        """Detach ``members`` from their current regions (ops + snapshot)."""
        taken: dict[str, int] = {}
        for m in members:
            for old in list(self.s.places[m].get("rooms") or []):
                if old == room:
                    continue
                self.ops.append(
                    {"op": "delete_edge", "from": old, "to": m, "type": "CONTAINS"}
                )
                label = f"{old} {self.s.rooms.get(old, {}).get('name', '')}".strip()
                taken[label] = taken.get(label, 0) + 1
            self.s.places[m]["rooms"] = [room]
            self.s.places[m]["room"] = room
        return taken

    # -- steps
    def step(self, st: dict) -> None:
        do = st.get("do")
        if do not in STEPS:
            raise PrepError(f"unknown step {do!r} (one of {', '.join(sorted(STEPS))})")
        getattr(self, f"do_{do}")(st)

    def do_delete_objects(self, st):
        cls = (st.get("class") or "").strip().lower() or None
        if st.get("symbols"):
            ci = {k.lower(): k for k in self.s.objects}
            syms = []
            for x in st["symbols"]:
                k = ci.get(str(x).lower())
                if k is None:
                    self.report["unresolved"].append(
                        f"{x}: no such object (delete_objects)"
                    )
                else:
                    syms.append(k)
        elif cls:
            syms = sorted((k for k, o in self.s.objects.items() if (o["cls"] or "").lower() == cls),
                          key=_symkey)  # fmt: skip
            near = st.get("near")
            if near:
                [p] = self.places([near["place"]], "delete_objects") or [None]
                if p is None:
                    return
                c, rad = self.s.places[p]["xyz"], float(near.get("radius", 3.0))
                syms = [
                    k
                    for k in syms
                    if math.dist(self.s.objects[k]["xyz"][:2], c[:2]) <= rad
                ]
        else:
            raise PrepError("delete_objects needs 'class' or 'symbols'")
        for k in syms:
            self.ops.append({"op": "delete_node", "symbol": k})
            self.s.objects.pop(k, None)
        self.report["steps"].append(
            {"do": "delete_objects", "class": cls, "deleted": syms}
        )

    def do_add_objects(self, st):
        cls = (st.get("class") or "").strip().lower()
        if not cls:
            raise PrepError("add_objects needs a class")
        size = float(st.get("size", OBJECT_SIZE))
        added = []
        for p in self.places(st.get("at"), "add_objects"):
            sym = self.new_symbol("O")
            x, y, z = self.s.places[p]["xyz"]
            pos = [round(x, 3), round(y, 3), round(z + OBJECT_Z, 3)]
            self.ops.append({"op": "create_node", "layer": "Object", "symbol": sym, "class": cls,
                             "pos": pos, "size": size, "source": "mission_prep",
                             "reason": "added by a mission-prep file"})  # fmt: skip
            self.ops.append(
                {"op": "create_edge", "from": p, "to": sym, "type": "CONTAINS"}
            )
            self.s.objects[sym] = {"cls": cls, "xyz": pos, "places": [p], "added": True}
            added.append({"symbol": sym, "place": p})
        self.report["steps"].append({"do": "add_objects", "class": cls, "added": added})

    def do_retire_places(self, st):
        done = []
        for p in self.places(st.get("places"), "retire_places"):
            self.ops.append({"op": "retire_node", "symbol": p,
                             "reason": st.get("reason") or "obstacle (mission-prep file)"})  # fmt: skip
            info = self.s.places.pop(p)
            for q in info["nbrs"]:
                self.s.places.get(q, {}).get("nbrs", set()).discard(p)
            self.s.retired[p] = {"xyz": info["xyz"], "label": PLACE, "room": info.get("room"),
                                 "nbrs": info["nbrs"]}  # fmt: skip
            done.append(p)
        self.report["steps"].append({"do": "retire_places", "retired": done})

    def do_restore_places(self, st):
        done = []
        names = st.get("places")
        if names in (None, "all", "*"):
            names = sorted(
                (
                    k
                    for k, v in self.s.retired.items()
                    if v.get("label") in (PLACE, None)
                ),
                key=_symkey,
            )
        elif isinstance(names, str):
            names = [names]
        for n in names:
            p = self.s.retired_place(str(n))
            if p is None:
                if self.s.place(str(n)):
                    self.report["warnings"].append(
                        f"{n}: already in the map (restore_places)"
                    )
                else:
                    self.report["unresolved"].append(
                        f"{n}: no removed place to restore (restore_places)"
                    )
                continue
            self.ops.append({"op": "restore_node", "symbol": p})
            info = self.s.retired.pop(p)
            nbrs = set(info.get("nbrs") or ())
            self.s.places[p] = {"xyz": info["xyz"], "room": info.get("room"),
                                "rooms": [info["room"]] if info.get("room") else [], "nbrs": nbrs}  # fmt: skip
            for q in nbrs:
                if q in self.s.places:
                    self.s.places[q]["nbrs"].add(p)
            done.append(p)
        self.report["steps"].append({"do": "restore_places", "restored": done})

    def do_add_region(self, st):
        name = str(st.get("name") or "").strip()
        if not name:
            raise PrepError("add_region needs a name")
        if self.s.room_by_name(name):
            r = self.s.room_by_name(name)
            self.report["unresolved"].append(
                f"region name {name!r} is taken by {r} {self.s.rooms[r]['name']!r} (names are unique ignoring case)"
            )
            return
        cls = str(st.get("class") or name).strip().lower()
        boundary = None
        if st.get("polygon_from"):
            corners = self.places(st["polygon_from"], "add_region")
            if len(corners) < 3:
                self.report["unresolved"].append(
                    f"region {name!r}: a polygon needs 3+ corner places"
                )
                return
            boundary = [[round(x, 3), round(y, 3)] for x, y in
                        convex_hull([self.s.places[c]["xyz"][:2] for c in corners])]  # fmt: skip
            members = sorted((p for p, v in self.s.places.items()
                              if p not in self.claimed
                              and point_in_polygon(v["xyz"][0], v["xyz"][1], boundary)), key=_symkey)  # fmt: skip
            members = list(dict.fromkeys(corners + members))
            if st.get("expand"):
                members = self.expand(members, st.get("expand"))
        else:
            seeds = self.places(st.get("places"), "add_region")
            if not seeds:
                self.report["unresolved"].append(
                    f"region {name!r}: none of its places resolved"
                )
                return
            members = self.expand(seeds, st.get("expand", "none"))
        sym = self.new_symbol("R")
        taken = self.take(sym, members)
        self.claimed |= set(members)
        op = {"op": "group", "parent_layer": "Room", "symbol": sym, "class": cls, "name": name,
              "members": members, "member_layers": [PLACE], "source": "mission_prep"}  # fmt: skip
        if boundary:
            op["polygon"] = boundary
        self.ops.append(op)
        xs = [self.s.places[m]["xyz"] for m in members]
        self.s.rooms[sym] = {"name": name, "cls": cls, "boundary": boundary, "added": True,
                             "xyz": [sum(c[i] for c in xs) / len(xs) for i in range(3)]}  # fmt: skip
        self.report["steps"].append({"do": "add_region", "symbol": sym, "name": name, "class": cls,
                                     "members": members, "taken_from": taken,
                                     "polygon": boundary})  # fmt: skip

    def do_add_to_region(self, st):
        r = self.region(st.get("name", ""), "add_to_region")
        if r is None:
            return
        members = [m for m in self.expand(self.places(st.get("places"), "add_to_region"), st.get("expand"))
                   if self.s.places[m].get("room") != r]  # fmt: skip
        taken = self.take(r, members)
        self.claimed |= set(members)
        for m in members:
            self.ops.append(
                {"op": "create_edge", "from": r, "to": m, "type": "CONTAINS"}
            )
        self.report["steps"].append(
            {"do": "add_to_region", "symbol": r, "added": members, "taken_from": taken}
        )

    def do_rename_region(self, st):
        r = self.region(st.get("name", ""), "rename_region")
        to = str(st.get("to") or "").strip()
        if r is None or not to:
            return
        other = self.s.room_by_name(to)
        if other and other != r:
            self.report["unresolved"].append(f"region name {to!r} is taken by {other}")
            return
        self.ops.append({"op": "update_node", "symbol": r, "set": {"name": to}})
        self.s.rooms[r]["name"] = to
        self.report["steps"].append({"do": "rename_region", "symbol": r, "to": to})

    def do_delete_region(self, st):
        r = self.region(st.get("name", ""), "delete_region")
        if r is None:
            return
        self.ops.append({"op": "delete_node", "symbol": r})
        freed = [p for p, v in self.s.places.items() if r in (v.get("rooms") or [])]
        for p in freed:
            v = self.s.places[p]
            v["rooms"] = [x for x in v["rooms"] if x != r]
            v["room"] = v["rooms"][0] if v["rooms"] else None
        self.s.rooms.pop(r)
        self.report["steps"].append(
            {"do": "delete_region", "symbol": r, "unassigned": len(freed)}
        )


def _symkey(s: str):
    m = re.fullmatch(r"([A-Za-z]+)\(?(\d+)\)?", s)
    return (m.group(1), int(m.group(2))) if m else (s, 0)


def compile_intent(
    intent: list[dict], snap: Snapshot
) -> tuple[list[dict], dict, Snapshot]:
    """``(ops, report, after)``: graph_edit ops in order, what each step resolved to
    (``report['unresolved']`` non-empty = the file cannot be applied as asked), and the
    map as the ops leave it (for rendering)."""
    if not isinstance(intent, list) or not intent:
        raise PrepError("intent is a non-empty list of steps")
    c = _Compiler(snap)
    for st in intent:
        if not isinstance(st, dict):
            raise PrepError(f"a step is an object, got {st!r}")
        c.step(st)
    c.report["counts"] = summarize_ops(c.ops)
    return c.ops, c.report, c.s


def summarize_ops(ops: list[dict]) -> dict:
    out: dict[str, int] = {}
    for o in ops:
        out[o["op"]] = out.get(o["op"], 0) + 1
    return out


# ------------------------------------------------------------------ files
def validate_file(f) -> dict:
    if isinstance(f, (str, bytes)):
        try:
            f = json.loads(f)
        except json.JSONDecodeError as e:
            raise PrepError(f"not JSON: {e}") from None
    if not isinstance(f, dict):
        raise PrepError("a mission-prep file is a JSON object")
    if f.get("format") != FORMAT:
        raise PrepError(f"format must be {FORMAT!r} (got {f.get('format')!r})")
    if not str(f.get("name") or "").strip():
        raise PrepError("the file needs a name")
    if not isinstance(f.get("intent"), list) or not f["intent"]:
        raise PrepError("the file needs a non-empty 'intent' list")
    for st in f["intent"]:
        if not isinstance(st, dict) or st.get("do") not in STEPS:
            raise PrepError(f"unknown step {st!r}")
    return f


def file_hash(f: dict) -> str:
    """Hash of what decides the edit (name + intent), not the compiled cache."""
    body = json.dumps(
        {"name": f.get("name"), "intent": f.get("intent")}, sort_keys=True
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def make_file(name: str, intent: list[dict], snap: Snapshot, summary: str = "") -> dict:
    ops, report, _ = compile_intent(intent, snap)
    return {
        "format": FORMAT,
        "name": name,
        "summary": summary or name,
        "map": {"prior": snap.prior, "epoch": snap.epoch, "version": snap.version},
        "intent": intent,
        "ops": ops,
        "report": report,
    }


def _norm_ops(ops: list[dict]) -> list[str]:
    """Ops without the symbols allocated for new nodes, for comparing two compiles."""
    out = []
    for o in ops:
        o = {k: v for k, v in o.items() if k not in ("reason",)}
        out.append(json.dumps(o, sort_keys=True))
    return out


def prepare(f: dict, snap: Snapshot) -> dict:
    """Resolve a file against the live map: ``{ops, report, after, prior_ok,
    same_as_file, problems}``.  ``problems`` (unresolved steps) block applying."""
    f = validate_file(f)
    ops, report, after = compile_intent(f["intent"], snap)
    want = (f.get("map") or {}).get("prior")
    prior_ok = not want or not snap.prior or prior_name(want) == snap.prior
    same = f.get("ops") is None or _norm_ops(f["ops"]) == _norm_ops(ops)
    return {"ops": ops, "report": report, "after": after, "prior_ok": prior_ok,
            "same_as_file": same, "problems": list(report["unresolved"]),
            "map_prior": snap.prior, "file_prior": want}  # fmt: skip
