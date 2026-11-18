"""Mission-prep edit files (mission_prep): compiling intent against a map, and (with a
throwaway Neo4j, ``ADT4_TEST_PREP_NEO4J``) applying files, a follow-up file, the
applied list, the trial-log copy and Reset."""

import json
import os
import socket
import urllib.parse

import pytest

from adt4_console.mission_prep.core import (
    FORMAT,
    PrepError,
    Snapshot,
    compile_intent,
    convex_hull,
    make_file,
    point_in_polygon,
    prepare,
    prior_name,
    validate_file,
)


def grid_snapshot() -> Snapshot:
    """A 4x3 grid of places T0..T11 (2 m apart, 4-connected); R0 'road' holds the
    left half, R1 'parking lot' the right; cones O1 (T0), O2 (T11), a chair O3."""
    s = Snapshot(prior_path="/x/penn_run/hydra/backend/dsg.json", epoch="e1", version=1)
    for i in range(12):
        x, y = i % 4, i // 4
        s.places[f"T{i}"] = {"xyz": [2.0 * x, 2.0 * y, 0.0], "room": "R0" if x < 2 else "R1",
                             "rooms": ["R0" if x < 2 else "R1"], "nbrs": set()}  # fmt: skip
    for i in range(12):
        x, y = i % 4, i // 4
        for dx, dy in ((1, 0), (0, 1)):
            if x + dx < 4 and y + dy < 3:
                j = (y + dy) * 4 + x + dx
                s.places[f"T{i}"]["nbrs"].add(f"T{j}")
                s.places[f"T{j}"]["nbrs"].add(f"T{i}")
    s.rooms = {"R0": {"name": "road", "cls": "road", "xyz": [1, 2, 0]},
               "R1": {"name": "parking lot", "cls": "parking lot", "xyz": [5, 2, 0]}}  # fmt: skip
    s.objects = {"O1": {"cls": "cone", "xyz": [0, 0, 0.3], "places": ["T0"]},
                 "O2": {"cls": "cone", "xyz": [6, 4, 0.3], "places": ["T11"]},
                 "O3": {"cls": "chair", "xyz": [2, 2, 0.3], "places": ["T5"]}}  # fmt: skip
    s.used_symbols = set(s.places) | set(s.rooms) | set(s.objects)
    return s


def by_op(ops, name):
    return [o for o in ops if o["op"] == name]


def test_delete_and_add_objects_ignore_case_and_attach():
    ops, rep, after = compile_intent(
        [{"do": "delete_objects", "class": "Cone"}, {"do": "add_objects", "class": "cone", "at": ["t5", "T6"]}],
        grid_snapshot(),
    )  # fmt: skip
    assert [o["symbol"] for o in by_op(ops, "delete_node")] == ["O1", "O2"]
    created = by_op(ops, "create_node")
    assert [c["symbol"] for c in created] == ["O4", "O5"]  # past the highest O symbol
    assert created[0]["pos"][:2] == [2.0, 2.0] and created[0]["class"] == "cone"
    assert {(e["from"], e["to"]) for e in by_op(ops, "create_edge")} == {
        ("T5", "O4"),
        ("T6", "O5"),
    }
    assert set(after.objects) == {"O3", "O4", "O5"} and not rep["unresolved"]


def test_region_adjacent_takes_places_from_their_old_region():
    ops, rep, after = compile_intent(
        [{"do": "add_region", "name": "base camp", "places": ["T5"], "expand": "adjacent"}], grid_snapshot()
    )  # fmt: skip
    [g] = by_op(ops, "group")
    assert (
        g["symbol"] == "R2" and g["class"] == "base camp" and g["name"] == "base camp"
    )
    assert g["members"] == ["T5", "T1", "T4", "T6", "T9"]
    # every member leaves its old region first (one parent region per place)
    assert {(d["from"], d["to"]) for d in by_op(ops, "delete_edge")} == {
        ("R0", "T5"), ("R0", "T1"), ("R0", "T4"), ("R1", "T6"), ("R0", "T9")}  # fmt: skip
    assert ops.index(g) > max(ops.index(d) for d in by_op(ops, "delete_edge"))
    assert rep["steps"][0]["taken_from"] == {"R0 road": 4, "R1 parking lot": 1}
    assert all(after.places[m]["rooms"] == ["R2"] for m in g["members"])


def test_a_later_regions_ring_never_takes_an_earlier_regions_places():
    ops, rep, after = compile_intent(
        [{"do": "add_region", "name": "intersection 2", "places": ["T5"], "expand": "adjacent"},
         {"do": "add_region", "name": "base camp", "places": ["T7"], "expand": "adjacent"}],
        grid_snapshot(),
    )  # fmt: skip
    a, b = by_op(ops, "group")
    assert "T6" in a["members"] and "T6" not in b["members"]  # T6 is in both rings
    # ... but a place named explicitly always moves
    ops, _, after = compile_intent(
        [{"do": "add_region", "name": "a", "places": ["T5"], "expand": "adjacent"},
         {"do": "add_region", "name": "b", "places": ["T6"]}], grid_snapshot(),
    )  # fmt: skip
    assert after.places["T6"]["room"] == "R3"


def test_polygon_region_and_geometry():
    hull = convex_hull([(0, 0), (4, 0), (4, 2), (0, 2), (2, 1)])
    assert (
        len(hull) == 4 and point_in_polygon(2, 1, hull) and point_in_polygon(4, 1, hull)
    )
    assert not point_in_polygon(5, 1, hull)
    ops, rep, _ = compile_intent(
        [{"do": "add_region", "name": "Parking Lot 7", "class": "parking lot", "polygon_from": ["T0", "T2", "T10", "T8"]}],
        grid_snapshot(),
    )  # fmt: skip
    [g] = by_op(ops, "group")
    assert sorted(g["members"], key=lambda s: int(s[1:])) == [
        "T0",
        "T1",
        "T2",
        "T4",
        "T5",
        "T6",
        "T8",
        "T9",
        "T10",
    ]
    assert g["polygon"] and g["class"] == "parking lot"


def test_names_are_unique_ignoring_case_and_unknown_places_are_reported():
    _, rep, _ = compile_intent(
        [{"do": "add_region", "name": "Parking Lot", "places": ["T0"]}], grid_snapshot()
    )
    assert "taken by R1" in rep["unresolved"][0]
    _, rep, _ = compile_intent(
        [{"do": "add_objects", "class": "cone", "at": ["T99"]}], grid_snapshot()
    )
    assert rep["unresolved"] == ["T99: no such place (add_objects)"]
    with pytest.raises(PrepError):
        compile_intent([{"do": "fly"}], grid_snapshot())


def test_retire_then_restore_in_a_follow_up_file():
    snap = grid_snapshot()
    ops, rep, after = compile_intent(
        [{"do": "retire_places", "places": ["T5", "t6"]},
         {"do": "add_region", "name": "x", "places": ["T4"], "expand": "adjacent"}], snap,
    )  # fmt: skip
    assert [o["symbol"] for o in by_op(ops, "retire_node")] == ["T5", "T6"]
    assert (
        "T5" not in by_op(ops, "group")[0]["members"]
    )  # removed places never join a region
    assert set(after.retired) == {"T5", "T6"} and "T5" not in after.places["T4"]["nbrs"]
    ops2, rep2, after2 = compile_intent(
        [{"do": "restore_places", "places": ["T5", "T6", "T7"]}], after
    )
    assert [o["symbol"] for o in by_op(ops2, "restore_node")] == ["T5", "T6"]
    assert rep2["warnings"] == ["T7: already in the map (restore_places)"]
    assert "T5" in after2.places and "T5" in after2.places["T4"]["nbrs"]


def test_files_validate_hash_and_prepare():
    snap = grid_snapshot()
    f = make_file("m1", [{"do": "delete_objects", "class": "cone"}], snap, "cones")
    assert (
        validate_file(json.dumps(f))["format"] == FORMAT
        and f["map"]["prior"] == "penn_run"
    )
    res = prepare(f, snap)
    assert res["same_as_file"] and res["prior_ok"] and not res["problems"]
    snap.objects.pop("O2")  # the map moved on: re-resolved, still applicable
    res = prepare(f, snap)
    assert not res["same_as_file"] and [o["symbol"] for o in res["ops"]] == ["O1"]
    assert (
        prior_name("/home/rrg/adt4_output/prior_penn_run/hydra/backend/dsg.json")
        == "penn_run"
    )
    for bad in (
        {"format": "x"},
        {"format": FORMAT, "name": "a", "intent": []},
        "not json",
    ):
        with pytest.raises(PrepError):
            validate_file(bad)


# ----------------------------------------------------------------- Neo4j (opt-in)
PREP_URI = os.environ.get("ADT4_TEST_PREP_NEO4J")  # a THROWAWAY database: it is wiped


def _open(uri):
    u = urllib.parse.urlparse(uri)
    try:
        socket.create_connection((u.hostname, u.port or 7687), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.fixture
def prep_db():
    if not PREP_URI or not _open(PREP_URI):
        pytest.skip("set ADT4_TEST_PREP_NEO4J to a throwaway Neo4j (it is wiped)")
    pytest.importorskip("heracles.graph_edit")
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        PREP_URI, auth=("neo4j", os.environ.get("HERACLES_NEO4J_PASSWORD", "neo4j_pw"))
    )
    with drv.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        s.run(
            "CREATE (:_MapState {epoch: 'ep1', version: 1, prior_path: '/x/penn_run/hydra/backend/dsg.json'})"
        )
        s.run(
            "CREATE (:_Labelspace {layer: 'room', ids: [0, 6], names: ['unknown', 'road']})"
        )
        s.run("CREATE (:_Labelspace {layer: 'object', ids: [3], names: ['cone']})")
        s.run("CREATE (r:Room {nodeSymbol: 'R0', name: 'road', class: 'road', layer: 4, partition: 0, "
              "center: point({x: 0, y: 0, z: 0})})")  # fmt: skip
        for i in range(4):
            s.run("MATCH (r:Room {nodeSymbol: 'R0'}) CREATE (r)-[:CONTAINS]->(:MeshPlace {nodeSymbol: $s, "
                  "layer: 3, partition: 1, center: point({x: $x, y: 0.0, z: 0.0})})", s=f"T{i}", x=2.0 * i)  # fmt: skip
        for i in range(3):
            s.run("MATCH (a:MeshPlace {nodeSymbol: $a}), (b:MeshPlace {nodeSymbol: $b}) "
                  "CREATE (a)-[:MESH_PLACE_CONNECTED]->(b)", a=f"T{i}", b=f"T{i + 1}")  # fmt: skip
        s.run("MATCH (p:MeshPlace {nodeSymbol: 'T0'}) CREATE (p)-[:CONTAINS]->(:Object {nodeSymbol: 'O1', "
              "class: 'cone', semantic_label: 3, layer: 2, partition: 0, center: point({x: 0, y: 0, z: 0.3})})")  # fmt: skip
    yield drv
    drv.close()


class _Src:
    id = "neo4j:test"

    def __init__(self, drv):
        self.drv = drv

    def db(self):
        return self.drv


class _Edits:
    """EditService's apply / undo on heracles graph_edit directly."""

    def __init__(self, src):
        self.src = src

    def source(self, _):
        return self.src

    def apply(
        self, src_id, ops, *, author=None, summary=None, dry_run=False, stage=None, **_
    ):
        from heracles import graph_edit as ge

        return ge.apply_edit(self.src.drv, ops, provenance={"source": "test", **(author or {})},
                             summary=summary, dry_run=dry_run)  # fmt: skip

    def undo(self, src_id, mutation_id=None, *, author=None, force=False):
        from heracles import graph_edit as ge

        return ge.undo_edit(
            self.src.drv, mutation_id, provenance={"source": "test"}, force=force
        )


class _Maps:
    def __init__(self, src):
        self.src = src

    def target(self):
        return self.src


class _Trials:
    def __init__(self, d):
        self.active = {"trial_id": "t1", "console_dir": str(d)}
        self.events = []

    def event(self, kind, trial=None, **fields):
        self.events.append((kind, fields))


def _state(drv):
    with drv.session() as s:
        return {
            "places": sorted(r[0] for r in s.run("MATCH (p:MeshPlace) RETURN p.nodeSymbol")),
            "objects": sorted(r[0] for r in s.run("MATCH (o:Object) RETURN o.nodeSymbol")),
            "rooms": sorted(r[0] for r in s.run("MATCH (r:Room) RETURN r.nodeSymbol")),
            "contains": sorted(f"{r[0]}>{r[1]}" for r in s.run(
                "MATCH (a)-[:CONTAINS]->(b) WHERE NOT a:GraphMutation RETURN a.nodeSymbol, b.nodeSymbol")),
        }  # fmt: skip


def test_apply_follow_up_trial_copy_and_reset(prep_db, tmp_path):
    from adt4_console.mission_prep.service import PrepService

    src = _Src(prep_db)
    svc = PrepService(_Maps(src), _Edits(src))
    svc.trials = _Trials(tmp_path)
    initial = _state(prep_db)
    m1 = make_file("mission1", [
        {"do": "delete_objects", "class": "cone"},
        {"do": "add_objects", "class": "cone", "at": ["t3"]},
        {"do": "retire_places", "places": ["T1"]},
        {"do": "add_region", "name": "base camp", "places": ["T2"], "expand": "adjacent"},
    ], __import__("adt4_console.mission_prep.core", fromlist=["x"]).snapshot_from_neo4j(prep_db))  # fmt: skip
    chk = svc.check(m1)
    assert (
        chk["ok"] and chk["dry_run"]["ok"] and _state(prep_db) == initial
    )  # a check writes nothing
    out = svc.apply(m1)
    st = _state(prep_db)
    assert (
        st["places"] == ["T0", "T2", "T3"]
        and st["objects"] == ["O2"]
        and "R1" in st["rooms"]
    )
    assert {"R1>T2", "R1>T3", "T3>O2"} <= set(st["contains"]) and "R0>T2" not in st[
        "contains"
    ]
    assert out["applied"]["name"] == "mission1"
    # the trial got a copy of the file and an event
    kind, fields = svc.trials.events[-1]
    assert kind == "prep_apply" and (tmp_path / fields["file"]).is_file()
    assert json.loads((tmp_path / fields["file"]).read_text())["name"] == "mission1"
    # a follow-up file: restore the removed place
    m2 = {
        "format": FORMAT,
        "name": "remove obstacles",
        "intent": [{"do": "restore_places", "places": ["T1"]}],
    }
    svc.apply(m2)
    assert "T1" in _state(prep_db)["places"]
    assert [a["name"] for a in svc.status()["applied"]] == [
        "mission1",
        "remove obstacles",
    ]
    # a trial that starts now copies both in (once per file)
    svc.trials = _Trials(tmp_path / "t2")
    ctx = svc.trial_context()
    assert (
        len(list((tmp_path / "t2" / "prep_files").glob("*.json"))) == 2
        and len(ctx["applied"]) == 2
    )
    svc.trial_context()
    assert len(list((tmp_path / "t2" / "prep_files").glob("*.json"))) == 2
    # Reset: both files undone newest first, the map is back to the prior
    with pytest.raises(PrepError):
        svc.reset("files", "nope")
    r = svc.reset("files", "reset")
    assert r["ok"] and r["undone"] == ["remove obstacles", "mission1"]
    assert _state(prep_db) == initial and svc.status()["applied"] == []
    assert svc.trials.events[-1][0] == "prep_reset"


# ----------------------------------------------------------------- launch hook
class _Job:
    def __init__(self):
        self.lines = []

    def log(self, kind, text):
        self.lines.append((kind, text))


class _Plan:
    prior_path = "/out/penn_run"
    bs_session = "base_station_web_repair-default"


class _FakePrep:
    def __init__(self, seeded=True):
        self.seeded, self.applied, self.waited = seeded, [], None

    def wait_for_seed(self, epoch_before, timeout_s=0, log_fn=None, **_):
        self.waited = epoch_before
        return self.seeded

    def apply(self, f, via="ui"):
        self.applied.append((f["name"], via))
        return {
            "counts": {"group": 1},
            "apply_s": 0.1,
            "applied": {"map_version": 2},
            "same_as_file": True,
        }


def test_launch_applies_files_after_the_reseed_in_order():
    from types import SimpleNamespace

    from adt4_console.routes.launch import apply_launch_prep

    files = [{"name": "mission1"}, {"name": "remove obstacles"}]
    prep, job = _FakePrep(), _Job()
    assert apply_launch_prep(
        SimpleNamespace(prep=prep), job, _Plan(), files, "old-epoch"
    )
    assert prep.waited == "old-epoch" and prep.applied == [
        ("mission1", "launch"),
        ("remove obstacles", "launch"),
    ]
    assert sum(k == "ok" for k, _ in job.lines) == 2
    # the base never reloaded the map: nothing is applied, the operator is told
    prep, job = _FakePrep(seeded=False), _Job()
    assert not apply_launch_prep(
        SimpleNamespace(prep=prep), job, _Plan(), files, "old-epoch", timeout_s=1
    )
    assert prep.applied == [] and "NOT applied" in job.lines[-1][1]
