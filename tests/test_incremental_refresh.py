"""Neo4jSource.apply_change / poll_change: a known write (SAM3 commit, map edit) is
folded into the loaded graph instead of reloading the whole map, and must give exactly
what a full reload gives (records, edges, labelspaces).  The console sees writes by the
map version (heracles ``_MapState.version``) and what the last bump recorded as changed
(``map_reload.read_map_change``).

The DB tests need a throwaway Neo4j they may wipe: ``ADT4_TEST_SCRATCH_NEO4J``
(default neo4j://localhost:7701; skipped when nothing listens).  They only wipe a DB
that is empty or holds nothing but their own seed map (``_IncrementalTestMarker``),
so pointing the variable at a real map skips instead of erasing it.
"""

import os
from types import SimpleNamespace as NS

import numpy as np
import pytest

from .conftest import NEO4J_AUTH, port_open

SCRATCH = os.environ.get("ADT4_TEST_SCRATCH_NEO4J", "neo4j://localhost:7701")
MARKER = "_IncrementalTestMarker"


# ------------------------------------------------------------------ no database
class FakeHub:
    def __init__(self):
        self.frames = []

    def publish(self, topic, data):
        self.frames.append((topic, data))
        return 1


def test_version_frame_carries_the_delta_and_its_base(settings):
    from adt4_console.codec import GraphDelta, NodeRecord
    from adt4_console.graph_service import GraphService, Neo4jSource

    hub = FakeHub()
    gs = GraphService(settings, hub=hub)
    src = Neo4jSource("m", "bolt://x:1", "u", "p")
    src._version = 7
    gs.sources[src.id] = src
    gs.notify_version(src.id, "plain")
    d = GraphDelta(
        upserts=[NodeRecord("O(1)", 2, 0, (1.0, 2.0, 3.0))], deletes=["O(2)"]
    )
    gs.notify_version(src.id, "sam3", (d, 9))
    (t0, plain), (t1, rich) = hub.frames
    assert t0 == t1 == f"graph/{src.id}/version"
    assert plain == {"version": 7, "reason": "plain"}
    assert rich["version"] == 9 and rich["base"] == 8 and rich["reason"] == "sam3"
    assert rich["delta"]["del"] == ["O(2)"] and rich["delta"]["up"]["ids"] == ["O(1)"]


def test_a_plan_repair_edit_that_moves_only_the_map_version_is_polled():
    """heracles_agents remove_scene_place deletes a place, records an undo
    _SceneChange (node count unchanged) and bumps only _MapState.version: the
    console must still see a new DB and reload."""
    from adt4_console.graph_service import Neo4jSource

    src = Neo4jSource("m", "bolt://x:1", "u", "p")
    db = {"count": 5290, "map": 1}
    src.count_nodes = lambda: db["count"]
    src.read_map_version = lambda: db["map"]
    src.read_map_change = lambda: (db["map"], None, None)
    src._mutation_changes = lambda lo, hi: {}
    v0 = src.version()
    assert src.poll() is False
    db["map"] = 2  # the edit: same node count
    assert src.poll() is True and src.version() == v0 + 1
    assert src.poll() is False
    db["count"] += 1  # a writer that does not bump the version
    assert src.poll() is True and src.version() == v0 + 2


def _polled_source(changes):
    """A Neo4jSource on a fake DB: ``changes`` is (version, layers, ids) of the last bump."""
    from adt4_console.graph_service import Neo4jSource

    src = Neo4jSource("m", "bolt://x:1", "u", "p")
    db = {"count": 10, "map": 4}
    src.count_nodes = lambda: db["count"]
    src.read_map_version = lambda: db["map"]
    src.read_map_change = lambda: changes.pop(0)
    src._mutation_changes = lambda lo, hi: {}  # no GraphMutation records
    tried = []

    def try_apply(version, ids):
        tried.append((version, ids))
        src._db_key_seen = src._db_key()
        src._version += 1
        return ("delta", src._version)

    src._try_apply = try_apply
    src.version()
    return src, db, tried


def test_poll_folds_in_a_recorded_change_one_version_on():
    """A write made outside the console (openset_query's commit, another console's
    edit) that recorded its nodes is folded in by the poll, like an own edit."""
    src, db, tried = _polled_source([(5, ["2:0"], ["O50"])])
    v0 = src.version()
    db["map"], db["count"] = 5, 11
    assert src.poll_change() == (True, ("delta", v0 + 1))
    assert tried == [(5, ["O50"])] and src.poll_change() == (False, None)


@pytest.mark.parametrize(
    "jump, change",
    [
        (1, (5, None, None)),  # the writer did not say what changed (a load, a reset)
        (1, (5, ["2:0"], [])),  # no nodes
        (1, (6, ["2:0"], ["O1"])),  # another bump landed after the poll's key read
        (2, (6, ["2:0"], ["O1"])),  # a version the console never saw in between
    ],
)
def test_poll_reloads_what_it_cannot_fold_in(jump, change):
    src, db, tried = _polled_source([change])
    v0 = src.version()
    db["map"] += jump
    assert src.poll_change() == (True, None) and src.version() == v0 + 1
    assert tried == [] and src.poll_change() == (False, None)


def test_poll_once_pushes_the_polled_delta(settings):
    from adt4_console.graph_service import GraphService, Neo4jSource

    hub = FakeHub()
    gs = GraphService(settings, hub=hub)
    src = Neo4jSource("m", "bolt://x:1", "u", "p")
    src.poll_change = lambda: (True, None)
    gs.sources[src.id] = src
    sent = []
    gs.notify_version = lambda sid, reason, delta=None: sent.append(
        (sid, reason, delta)
    )
    gs.poll_once()
    src.poll_change = lambda: (True, ("d", 3))
    gs.poll_once()
    src.poll_change = lambda: (False, None)
    gs.poll_once()
    assert sent == [(src.id, "neo4j", None), (src.id, "neo4j", ("d", 3))]


def test_edit_service_hands_the_affected_nodes_over(settings):
    from adt4_console.edit_service import EditService

    calls = []
    graph = NS(apply_change=lambda *a: calls.append(a))
    svc = EditService(settings, graph=graph, launch_state=lambda: None)
    out = {
        "mutation_id": "m1",
        "revision": 3,
        "summary": ["x"],
        "map_version": 12,
        "affected": ["O5", "P1"],
    }
    svc._after_commit("neo4j:penn", out, "map edit")
    assert calls == [("neo4j:penn", "map edit", 12, ["O5", "P1"])]


# ------------------------------------------- what made each version (no database)
def _known_source(mutations=None, last=(None, None, None)):
    """A Neo4jSource on a fake DB at map version 4 with a loaded graph; folds are
    recorded, not applied.  ``mutations``: {map version: affected} as heracles'
    GraphMutation records hold them; ``last``: read_map_change's answer."""
    from adt4_console.graph_service import Neo4jSource

    src = Neo4jSource("m", "bolt://x:1", "u", "p")
    db = {"count": 10, "map": 4}
    src.count_nodes = lambda: db["count"]
    src.read_map_version = lambda: db["map"]
    src.read_map_change = (
        lambda: last if last[0] is not None else (db["map"], None, None)
    )
    asked = []

    def mutation_changes(lo, hi):
        asked.append((lo, hi))
        return {v: a for v, a in (mutations or {}).items() if lo < v <= hi}

    src._mutation_changes = mutation_changes
    tried = []

    def try_apply(version, ids):
        tried.append((version, ids))
        src._db_key_seen = src._db_key()
        src._version += 1
        return ("delta", src._version)

    src._try_apply = try_apply
    src.version()
    src._loaded = NS(version=src._version)  # a viewer loaded it
    return src, db, tried, asked


def test_a_gap_of_several_graph_edit_commits_is_folded_in_at_once():
    """Two edits by other processes (another console, plan repair's graph_edit)
    between two polls: each left a GraphMutation record with its nodes, so the poll
    folds both in as one delta instead of reloading the map."""
    src, db, tried, asked = _known_source({5: ["O1", "P2"], 6: ["O9"]})
    v0 = src.version()
    db["map"] = 6
    assert src.poll_change() == (True, ("delta", v0 + 1))
    assert tried == [(6, ["O1", "P2", "O9"])] and asked == [(4, 6)]


@pytest.mark.parametrize(
    "mutations",
    [
        {5: ["O1"]},  # version 6 has no record: someone bumped without saying
        {5: ["O1"], 6: None},  # a record whose list was cut short (500 nodes)
    ],
)
def test_a_gap_with_an_unexplained_version_waits_then_reloads(mutations, monkeypatch):
    import adt4_console.graph_service as gsm

    src, db, tried, _ = _known_source(mutations)
    v0 = src.version()
    db["map"] = 6
    clock = {"t": 100.0}
    monkeypatch.setattr(gsm.time, "monotonic", lambda: clock["t"])
    assert src.poll_change() == (False, None)  # its announcement may be on the way
    clock["t"] += gsm.UNKNOWN_CHANGE_GRACE_S + 0.1
    assert src.poll_change() == (True, None) and src.version() == v0 + 1
    assert tried == [] and src.poll_change() == (False, None)


def test_an_announced_scene_change_explains_its_version():
    """heracles' scene change writer (change detection, chat SGET via heracles_agents)
    bumps without recording ids and announces the nodes on ROS: the announcement
    folds it in -- whether it comes before the poll or after the poll started
    waiting for it."""
    src, db, tried, _ = _known_source({})
    v0 = src.version()
    db["map"] = 5
    assert src.poll_change() == (False, None)  # waiting for the announcement
    assert src.apply_change(5, ["O7"]) == ("delta", v0 + 1)
    assert tried == [(5, ["O7"])]
    # announced first, polled after
    src.note_change(6, ["P3"])
    db["map"] = 6
    assert src.poll_change() == (True, ("delta", v0 + 2))
    assert tried[-1] == (6, ["P3"])


def test_an_announcement_the_poll_already_handled_changes_nothing():
    """The old double reload: the poll reloaded a change, then its announcement on
    /scene_graph/changes_applied invalidated the map a second time."""
    src, db, tried, _ = _known_source({5: ["O1"]})
    v0 = src.version()
    db["map"] = 5
    assert src.poll_change() == (True, ("delta", v0 + 1))
    assert src.apply_change(5, ["O1"]) is None and src.version() == v0 + 1
    assert len(tried) == 1


def test_a_node_count_change_without_a_bump_reloads_at_once():
    src, db, tried, _ = _known_source({})
    v0 = src.version()
    db["count"] += 3
    assert src.poll_change() == (True, None) and src.version() == v0 + 1
    assert tried == []


def test_a_removed_place_brings_the_objects_it_held():
    """scene_change relinks the objects of a removed place to their nearest place;
    the writer reports only the place, so its objects are re-read too."""
    from adt4_console.codec import NodeRecord
    from adt4_console.graph_service import Loaded, _with_held_objects

    recs = {
        "P(1)": NodeRecord("P(1)", 3, 1, (0.0, 0.0, 0.0)),
        "O(1)": NodeRecord("O(1)", 2, 0, (0.0, 0.0, 0.0)),
        "O(2)": NodeRecord("O(2)", 2, 0, (1.0, 0.0, 0.0)),
        "a(3)": NodeRecord("a(3)", 2, 97, (0.0, 0.0, 0.0)),
        "R(0)": NodeRecord("R(0)", 4, 0, (0.0, 0.0, 0.0)),
    }
    edges = {("P(1)", "O(1)"), ("O(2)", "P(1)"), ("P(1)", "a(3)"), ("R(0)", "P(1)")}
    loaded = Loaded(version=1, records=recs, edges=edges, labelspaces={})
    assert sorted(_with_held_objects(loaded, ["P(1)"])) == ["O(1)", "O(2)", "P(1)"]
    assert _with_held_objects(loaded, ["O(1)"]) == ["O(1)"]


# ------------------------------------------- a reload pushed as a diff (no database)
def _records(n):
    from adt4_console.codec import NodeRecord

    return {f"O({i})": NodeRecord(f"O({i})", 2, 0, (float(i), 0.0, 0.0)) for i in range(n)}


def test_diff_loaded_small_and_big():
    from adt4_console.codec import NodeRecord
    from adt4_console.graph_service import Loaded, diff_loaded

    old = Loaded(1, _records(100), {("O(0)", "O(1)")}, {"2/0": {"1": "chair"}})
    recs = dict(old.records)
    recs["O(5)"] = NodeRecord("O(5)", 2, 0, (5.0, 1.0, 0.0))  # moved
    del recs["O(6)"]
    recs["O(200)"] = NodeRecord("O(200)", 2, 0, (0.0, 9.0, 0.0))
    new = Loaded(2, recs, {("O(0)", "O(200)")}, old.labelspaces)
    d = diff_loaded(old, new)
    assert sorted(r.sym for r in d.upserts) == ["O(200)", "O(5)"]
    assert d.deletes == ["O(6)"] and d.labelspaces is None
    assert d.edges_add == [("O(0)", "O(200)")] and d.edges_del == [("O(0)", "O(1)")]
    # another map: most nodes differ, the viewers load the snapshot
    other = Loaded(3, {k: NodeRecord(k, 2, 0, (0.0, 0.0, 1.0)) for k in recs}, set(), {})
    assert diff_loaded(new, other) is None


class _ReloadSource:
    """A cacheable source whose next load is ``nxt``."""

    def __init__(self, base, nxt):
        from adt4_console.graph_service import Source

        self.id, self.cacheable, self._mesh_key = "neo4j:t", True, None
        self._loaded, self._version, self.nxt = base, base.version, nxt
        self.version = lambda: self._version
        self.invalidated = 0
        assert Source  # the interface GraphService._changed relies on

    def invalidate(self):
        self.invalidated += 1
        self._version += 1

    def loaded(self):
        if self._loaded.version != self._version:
            self._loaded = self.nxt(self._version)
        return self._loaded


def _wait_frames(hub, n, timeout=5.0):
    import time

    t0 = time.time()
    while len(hub.frames) < n and time.time() - t0 < timeout:
        time.sleep(0.01)
    return hub.frames


def test_refresh_pushes_the_difference_against_what_viewers_hold(settings):
    """A change only a reload can tell (a reset, a snapshot restore, free Cypher)
    is reloaded on the server and pushed as a delta against the version viewers
    hold: they do not download the whole snapshot again."""
    from adt4_console.codec import NodeRecord
    from adt4_console.graph_service import GraphService, Loaded

    hub = FakeHub()
    gs = GraphService(settings, hub=hub)
    base = Loaded(3, _records(50), set(), {})

    def nxt(v):
        recs = dict(base.records)
        recs["O(1)"] = NodeRecord("O(1)", 2, 0, (1.0, 5.0, 0.0))
        return Loaded(v, recs, set(), {})

    src = _ReloadSource(base, nxt)
    gs.sources[src.id] = src
    gs.refresh(src.id, "reset")
    ((topic, msg),) = _wait_frames(hub, 1)
    assert topic == f"graph/{src.id}/version" and src.invalidated == 1
    assert msg["version"] == 4 and msg["base"] == 3 and msg["reason"] == "reset"
    assert msg["delta"]["up"]["ids"] == ["O(1)"] and msg["delta"]["del"] == []


def test_refresh_of_another_map_or_nothing_loaded_is_a_plain_push(settings):
    from adt4_console.codec import NodeRecord
    from adt4_console.graph_service import GraphService, Loaded

    hub = FakeHub()
    gs = GraphService(settings, hub=hub)
    base = Loaded(3, _records(50), set(), {})
    other = lambda v: Loaded(v, {k: NodeRecord(k, 2, 0, (0, 0, 9.0)) for k in base.records}, set(), {})  # noqa: E731
    src = _ReloadSource(base, other)
    gs.sources[src.id] = src
    gs.refresh(src.id, "reset")
    ((_, msg),) = _wait_frames(hub, 1)
    assert msg == {"version": 4, "reason": "reset"}
    src._loaded = None  # nothing loaded: nothing to diff against, push at once
    gs.refresh(src.id, "chat write")
    assert hub.frames[-1][1] == {"version": 5, "reason": "chat write"}


def test_refresh_with_no_viewer_subscribed_does_not_reload(settings):
    """A map that changes all the time (a robot writing it) is not reloaded on the
    server for nobody: with no viewer subscribed the push is plain, at once."""
    import time

    from adt4_console.graph_service import GraphService, Loaded
    from adt4_console.hub import Hub

    hub = Hub()
    gs = GraphService(settings, hub=hub)
    src = _ReloadSource(Loaded(3, _records(5), set(), {}), lambda v: Loaded(v, _records(5), set(), {}))
    gs.sources[src.id] = src
    loads = []
    src.loaded = lambda: loads.append(1)
    gs.refresh(src.id, "neo4j")
    assert loads == [] and hub.latest(f"graph/{src.id}/version") == {"version": 4, "reason": "neo4j"}
    c = hub.add_client()
    hub.subscribe(c, ["graph/"])
    assert hub.wanted(f"graph/{src.id}/version") and not hub.wanted("video/x")
    del src.loaded  # the class's: reloads, then pushes the (empty) difference
    topic = f"graph/{src.id}/version"
    gs.refresh(src.id, "neo4j")
    t0 = time.time()
    while (hub.latest(topic) or {}).get("version") != 5 and time.time() - t0 < 5:
        time.sleep(0.01)
    msg = hub.latest(topic)
    # against the graph last loaded (v3): a viewer holding v4 never loaded it
    assert msg["version"] == 5 and msg["base"] == 3 and msg["delta"]["del"] == []


def test_apply_change_already_seen_sends_nothing(settings):
    from adt4_console.graph_service import GraphService

    hub = FakeHub()
    gs = GraphService(settings, hub=hub)
    src, db, tried, _ = _known_source({5: ["O1"]})
    gs.sources[src.id] = src
    db["map"] = 5
    assert gs.apply_change(src.id, "map edit", 5, ["O1"]) is True
    assert len(hub.frames) == 1  # the delta
    assert gs.apply_change(src.id, "scene changes", 5, ["O1"]) is False
    assert len(hub.frames) == 1  # the echo: nothing new, no push, no reload


def test_scene_changes_from_ros_name_their_nodes(settings):
    """A batch on /scene_graph/changes_applied from someone else (change detection,
    plan repair) reaches the graph with its map version and nodes."""
    from adt4_console.scene_repair import SceneRepair

    got = []
    sr = SceneRepair(
        map_changed=lambda mv, changes: got.append((mv, [c["symbol"] for c in changes]))
    )
    sr.on_applied(
        "scene_change_detector", 12, [{"kind": 1, "symbol": "O4", "label": "chair"}]
    )
    sr.on_applied("sget/chat", 13, [], local=True)  # our own: already folded in
    assert got == [(12, ["O4"])]


# --------------------------------------------------------------------- database
def _seed_graph():
    import spark_dsg as dsg

    G = dsg.DynamicSceneGraph()
    G.add_layer(3, 1, dsg.DsgLayers.MESH_PLACES)  # heracles' ingest expects it
    G.set_labelspace(dsg.Labelspace({0: "unknown", 1: "chair", 2: "table"}), 2, 0)
    G.set_labelspace(dsg.Labelspace({0: "region", 1: "kitchen"}), 4, 0)
    for i, (label, pos) in enumerate(
        [(1, (1.0, 0.0, 0.0)), (1, (9.0, 0.0, 0.0)), (2, (2.0, 2.0, 0.0))]
    ):
        a = dsg.ObjectNodeAttributes()
        a.position = np.array(pos)
        a.semantic_label = label
        a.bounding_box = dsg.BoundingBox(
            np.array([0.5, 0.5, 0.5], np.float32), np.array(pos, np.float32)
        )
        G.add_node(dsg.DsgLayers.OBJECTS, dsg.NodeSymbol("O", i), a)
    for i in range(3):
        p = dsg.PlaceNodeAttributes()
        p.position = np.array([float(3 * i), 1.0, 0.0])
        G.add_node(dsg.DsgLayers.PLACES, dsg.NodeSymbol("P", i), p)
    r = dsg.RoomNodeAttributes()
    r.position = np.array([3.0, 1.0, 0.0])
    r.semantic_label = 1
    G.add_node(dsg.DsgLayers.ROOMS, dsg.NodeSymbol("R", 0), r)
    b = dsg.SemanticNodeAttributes()
    b.position = np.array([3.0, 1.0, 0.0])
    G.add_node(dsg.DsgLayers.BUILDINGS, dsg.NodeSymbol("B", 0), b)
    for a, c in [
        ("P0", "P1"),
        ("P1", "P2"),
        ("P0", "O0"),
        ("P2", "O1"),
        ("R0", "P0"),
        ("R0", "P1"),
        ("B0", "R0"),
    ]:
        G.insert_edge(
            dsg.NodeSymbol(a[0], int(a[1:])), dsg.NodeSymbol(c[0], int(c[1:]))
        )
    return G


@pytest.fixture(scope="module")
def scratch():
    host, port = SCRATCH.split("//")[-1].split(":")
    if not port_open(host, int(port)):
        pytest.skip(f"no scratch Neo4j at {SCRATCH}")
    gi = pytest.importorskip("heracles.graph_interface")
    pytest.importorskip("heracles.map_reload")
    from heracles.query_interface import Neo4jWrapper

    with Neo4jWrapper(SCRATCH, NEO4J_AUTH, atomic_queries=True) as db:
        rows, _, _ = db.execute(
            f"RETURN COUNT {{ MATCH (n) WHERE NOT n:{MARKER} }} AS c, COUNT {{ MATCH (m:{MARKER}) }} AS marked"
        )
        if rows[0]["c"] and not rows[0]["marked"]:
            pytest.skip(
                f"{SCRATCH} holds a map this suite did not write; not wiping it"
            )
    return gi


@pytest.fixture
def seeded(scratch):
    """A fresh seed map in the scratch DB; returns (heracles graph_interface, db)."""
    from heracles.query_interface import Neo4jWrapper

    gi = scratch
    with Neo4jWrapper(SCRATCH, NEO4J_AUTH, atomic_queries=True) as db:
        db.execute("MATCH (n) DETACH DELETE n")
        db.execute(f"CREATE (:{MARKER})")
        gi.spark_dsg_to_db(_seed_graph(), db)
        _bump(db)
        yield gi, db


def _bump(db, layers=None, ids=None) -> int:
    from heracles.map_reload import bump_map_version

    return bump_map_version(db, layers, ids)


def _source(name="t"):
    from adt4_console.graph_service import Neo4jSource

    return Neo4jSource(name, SCRATCH, *NEO4J_AUTH)


def _full():
    s = _source("full")
    try:
        return s.loaded()
    finally:
        s.close()


def _assert_matches_full_reload(loaded):
    full = _full()
    assert {k: r.key() for k, r in loaded.records.items()} == {
        k: r.key() for k, r in full.records.items()
    }
    assert loaded.edges == full.edges
    assert loaded.labelspaces == full.labelspaces


def _edit(db, ops):
    from heracles import graph_edit as ge

    return ge.apply_edit(db, ops, provenance={"source": "test"})


def test_map_edits_match_a_full_reload(seeded):
    _, db = seeded
    src = _source()
    v0 = src.loaded().version
    # a new class grows the object labelspace: applied in place too
    out = _edit(
        db,
        [
            {
                "op": "create_node",
                "layer": "Object",
                "pos": [4.0, 1.0, 0.0],
                "class": "zz widget",
            }
        ],
    )
    sym = out["affected"][0]
    d, v = src.apply_change(out["map_version"], out["affected"])
    assert v == v0 + 1 and src.version() == v and src.loaded().version == v
    assert [r.sym for r in d.upserts] == ["O(3)"] and d.labelspaces
    assert "zz widget" in d.labelspaces["2/0"].values()
    _assert_matches_full_reload(src.loaded())
    for ops in (
        [{"op": "create_edge", "from": "P1", "to": sym}],  # place CONTAINS the object
        [{"op": "update_node", "symbol": sym, "set": {"pos": [5.0, 2.0, 0.0]}}],
        [{"op": "delete_edge", "from": "P0", "to": "P1"}],  # between two existing nodes
        [
            {"op": "retire_node", "symbol": "P2", "reason": "test"}
        ],  # takes its edges along
        [{"op": "retire_node", "symbol": sym, "reason": "test"}],
    ):
        out = _edit(db, ops)
        res = src.apply_change(out["map_version"], out["affected"])
        assert res is not None, ops
        _assert_matches_full_reload(src.loaded())
    assert "P(2)" not in src.loaded().records and not any(
        "P(2)" in e for e in src.loaded().edges
    )
    # the poll has nothing left to see: no second version, no reload
    assert src.poll() is False


def test_sam3_style_write_matches_a_full_reload(seeded):
    """What the SAM3 query server's commit does: insert an Object, link it, then
    notify_map_change(db, dirty_layers, [symbol])."""
    _, db = seeded
    src = _source()
    before = src.loaded()
    _sam3_style_write(db)
    mv = _notify(db, ["2:0"], ["O50"])
    d, v = src.apply_change(mv, ["O50"])
    assert v == before.version + 1
    assert [r.sym for r in d.upserts] == ["O(50)"] and d.edges_add == [
        ("P(2)", "O(50)")
    ]
    assert (
        before.G.has_node(__import__("spark_dsg").NodeSymbol("O", 50)) is False
    )  # old graph untouched
    _assert_matches_full_reload(src.loaded())


def test_a_write_made_elsewhere_is_folded_in_by_the_poll(seeded):
    """openset_query running on its own (no console commit route): it writes the
    object and bumps the map version with what it changed; the console's poll
    folds it in as a delta instead of reloading the map."""
    _, db = seeded
    src = _source()
    before = src.loaded()
    _sam3_style_write(db)
    _notify(db, ["2:0"], ["O50"])
    changed, (d, v) = src.poll_change()
    assert changed and v == before.version + 1 and src.loaded().version == v
    assert [r.sym for r in d.upserts] == ["O(50)"]
    _assert_matches_full_reload(src.loaded())
    # a graph_edit commit by someone else (another console) too
    out = _edit(
        db, [{"op": "update_node", "symbol": "O50", "set": {"pos": [6.5, 1.5, 0.2]}}]
    )
    changed, (d, v2) = src.poll_change()
    assert changed and v2 == v + 1 and [r.sym for r in d.upserts] == ["O(50)"]
    assert src.read_map_version() == out["map_version"]
    _assert_matches_full_reload(src.loaded())
    assert src.poll_change() == (False, None)


def _notify(db, layers, ids) -> int:
    """heracles_ros' notify_map_change where it imports (it needs spark_dsg), else
    the bump it makes."""
    try:
        from heracles_ros.map_state import notify_map_change
    except ImportError:
        return _bump(db, layers, ids)
    return notify_map_change(db, layers, ids)


def _sam3_style_write(db):
    db.execute(
        "CREATE (o:Object {nodeSymbol: 'O50', class: 'chair', name: 'green chair', "
        "center: point({x: 6.0, y: 1.5, z: 0.2}), attr_type: 'ObjectNodeAttributes'})"
    )
    db.execute(
        "MATCH (p:Place {nodeSymbol: 'P2'}), (o:Object {nodeSymbol: 'O50'}) CREATE (p)-[:CONTAINS]->(o)"
    )


def test_an_unknown_write_in_between_falls_back_to_one_reload(seeded):
    _, db = seeded
    src = _source()
    v0 = src.loaded().version
    _bump(db)  # someone else's write the console has not seen
    out = _edit(
        db, [{"op": "update_node", "symbol": "O0", "set": {"pos": [1.5, 0.0, 0.0]}}]
    )
    assert src.apply_change(out["map_version"], out["affected"]) is None
    assert src.version() == v0 + 1  # one bump ...
    assert src.poll() is False  # ... and the poll does not add a second
    _assert_matches_full_reload(src.loaded())


def test_nothing_loaded_or_no_symbols_just_invalidates(seeded):
    _, db = seeded
    src = _source()
    v0 = src.version()  # never loaded
    out = _edit(
        db, [{"op": "update_node", "symbol": "O1", "set": {"pos": [9.5, 0.0, 0.0]}}]
    )
    assert src.apply_change(out["map_version"], out["affected"]) is None
    assert src.version() == v0 + 1 and src.poll() is False
    src.loaded()
    out = _edit(
        db, [{"op": "update_node", "symbol": "O1", "set": {"pos": [9.0, 0.0, 0.0]}}]
    )
    assert src.apply_change(out["map_version"], []) is None
    _assert_matches_full_reload(src.loaded())


def test_invalidate_is_seen_once(seeded):
    """The old double reload: invalidate() after a write, then the poll saw the
    same write as a new DB key and bumped (and reloaded) again."""
    _, db = seeded
    src = _source()
    v0 = src.version()
    _bump(db)
    src.invalidate()
    assert src.version() == v0 + 1
    assert src.poll() is False and src.version() == v0 + 1
