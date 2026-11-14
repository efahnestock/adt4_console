"""Planner B: omniplanner's multi-robot PDDL planner in the console (API.md §15.6).

The mission chat's plan_ground_mission tool, the omniplanner tab's route and the
robot chat's goal preview all plan through ``mission.omni_planner`` on the
grid map of test_mission (1 m mesh-place grid, a wall at x = 10 open above
y = 8, O(0) at (18, 2) behind it, O(1) on an island).

Needs omniplanner with the goal-relevant multi-robot grounding and Fast
Downward (a sourced ADT4 workspace); skips otherwise.
"""

import pytest

from adt4_console.chat import ChatTurn

from .conftest import make_client
from .test_mission import LABELS, build_grid_run


@pytest.fixture
def grid_run(tmp_path):
    """test_mission's grid, with its mesh places in a named MESH_PLACES layer as
    Hydra saves them (omniplanner looks the layer up by name)."""
    import spark_dsg as dsg

    run = build_grid_run(tmp_path)
    path = run / "hydra" / "backend" / "dsg.json"
    G = dsg.DynamicSceneGraph.load(str(path))
    H = dsg.DynamicSceneGraph()
    H.add_layer(3, 1, dsg.DsgLayers.MESH_PLACES)
    H.set_labelspace(dsg.Labelspace(LABELS), 2, 0)
    for node in G.get_layer(3, 1).nodes:
        a = dsg.PlaceNodeAttributes()
        a.position = node.attributes.position
        H.add_node(dsg.DsgLayers.MESH_PLACES, node.id.value, a)
    for node in G.get_layer(dsg.DsgLayers.OBJECTS).nodes:
        a = dsg.KhronosObjectAttributes()
        a.position = node.attributes.position
        a.semantic_label = node.attributes.semantic_label
        a.bounding_box = node.attributes.bounding_box
        H.add_node(dsg.DsgLayers.OBJECTS, node.id.value, a)
    for edge in G.get_layer(3, 1).edges:
        H.insert_edge(edge.source, edge.target)
    H.save(str(path), False)
    return run


@pytest.fixture
def mission_settings(settings, grid_run, tmp_path):
    settings.maps = {"grid": str(grid_run)}
    settings.tiles_dir = str(tmp_path / "tiles")
    settings.missions_dir = str(tmp_path / "missions")
    return settings


omni = pytest.importorskip("adt4_console.mission.omni_planner")
if omni.available():
    pytest.skip(
        f"omniplanner cannot plan here: {omni.available()}", allow_module_level=True
    )

SPOT1 = {"id": "spot1", "kind": "spot", "start": [0.0, 0.0]}
SPOT2 = {"id": "spot2", "kind": "spot", "start": [20.0, 10.0]}


def _place(G, x, y):
    """The mesh place at (x, y) as a PDDL symbol (p<k>)."""
    import numpy as np
    import spark_dsg as dsg

    for node in G.get_layer(dsg.DsgLayers.MESH_PLACES).nodes:
        if np.allclose(node.attributes.position[:2], [x, y]):
            return node.id.str(True).lower()
    raise KeyError((x, y))


@pytest.fixture
def svc(mission_settings):  # noqa: F811
    client, services = make_client(mission_settings)
    with client:
        yield client, services.missions


def _G(service):
    from adt4_console.mission.model import Mission

    return service.world(Mission(src=service._default_src(), agents=[SPOT1])).graph.G


def test_closer_robot_gets_the_visit_and_ends_where_told(svc):
    _, service = svc
    G = _G(service)
    corner = _place(G, 0, 10)
    m, planned = omni.plan_pddl(
        f"(and (visited-object o0) (at-place spot1 {corner}))",
        [SPOT1, SPOT2],
        None,
        service,
    )
    by_agent = {a: [t for t in m.tasks if t.agent == a] for a in ("spot1", "spot2")}
    # spot2 (20, 10) is ~10 m from O(0); spot1 would go round the wall (~28 m)
    assert [t.target for t in by_agent["spot2"]] == ["O(0)"]
    assert by_agent["spot1"][-1].target == str(omni._modules()["to_node"](G, corner))
    assert m.planner == "omniplanner" and planned["driven_m"]["spot1"] > 9


def test_named_robot_does_the_task(svc):
    _, service = svc
    m, _ = omni.plan_pddl("(visited-object-by spot1 o0)", [SPOT1, SPOT2], None, service)
    assert [(t.agent, t.target) for t in m.tasks] == [("spot1", "O(0)")]


def test_inspect_becomes_one_inspect_task(svc):
    _, service = svc
    m, _ = omni.plan_pddl("(inspected-by spot2 o0)", [SPOT1, SPOT2], None, service)
    assert [(t.agent, t.type, t.target) for t in m.tasks] == [
        ("spot2", "inspect", "O(0)")
    ]


def test_no_go_zone_on_the_only_gap_cuts_the_object_off(svc):
    _, service = svc
    gap = {"id": "z", "polygon": [[9.4, 7.4], [10.6, 7.4], [10.6, 10.6], [9.4, 10.6]]}
    with pytest.raises(ValueError, match="o0 cannot be reached"):
        omni.plan_pddl("(visited-object o0)", [SPOT1], None, service, geofences=[gap])


def test_pick_and_place_become_tasks(svc):
    _, service = svc
    G = _G(service)
    dest = _place(G, 0, 0)
    m, _ = omni.plan_pddl(f"(object-in-place o0 {dest})", [SPOT1], None, service)
    types = [t.type for t in m.tasks]
    assert "pick" in types and "place" in types
    pick = next(t for t in m.tasks if t.type == "pick")
    place = next(t for t in m.tasks if t.type == "place")
    assert (
        pick.target == "O(0)"
        and place.params["object"] == "O(0)"
        and not place.params.get("dragged")
    )
    assert types.index("pick") < types.index("place")


def test_a_husky_never_picks(svc):
    _, service = svc
    G = _G(service)
    husky = {"id": "gauss", "kind": "husky", "start": [17.0, 2.0]}  # right next to O(0)
    m, _ = omni.plan_pddl(
        f"(object-in-place o0 {_place(G, 0, 0)})", [SPOT1, husky], None, service
    )
    assert all(
        t.agent == "spot1" for t in m.tasks if t.type in ("pick", "place", "drag")
    )


def test_bad_symbol_is_reported(svc):
    _, service = svc
    with pytest.raises(ValueError, match="o99"):
        omni.plan_pddl("(visited-object o99)", [SPOT1], None, service)


def test_route_plans_a_pddl_goal(svc):
    client, _ = svc
    r = client.post(
        "/api/missions/plan/omniplanner",
        json={"pddl_goal": "(visited-object o0)", "agents": [SPOT1, SPOT2]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mission"]["planner"] == "omniplanner"
    assert body["planned"]["driven_m"] and body["preview"]["issues"] is not None


def test_replan_keeps_the_goal_and_reassigns_from_new_starts(svc):
    client, _ = svc
    r = client.post(
        "/api/missions/plan/omniplanner",
        json={"pddl_goal": "(visited-object o0)", "agents": [SPOT1, SPOT2]},
    )
    m = r.json()["mission"]
    assert m["goal"] == {
        "pddl": "(visited-object o0)",
        "constraints": [],
        "engine": "fast_downward",
    }
    assert [t["agent"] for t in m["tasks"]] == ["spot2"]  # spot2 starts next to O(0)
    # spot2 moves behind the wall's far end, spot1 next to O(0): spot1 takes it now
    r = client.post(
        f"/api/missions/{m['id']}/replan",
        json={"starts": {"spot1": [17.0, 2.0], "spot2": [0.0, 0.0]}},
    )
    assert r.status_code == 200, r.text
    new = r.json()["mission"]
    assert (
        new["id"] == m["id"]
        and new["revision"] == m["revision"] + 1
        and new["goal"] == m["goal"]
    )
    assert [t["agent"] for t in new["tasks"]] == ["spot1"]
    assert {a["id"]: a["start"] for a in new["agents"]} == {
        "spot1": [17.0, 2.0],
        "spot2": [0.0, 0.0],
    }
    assert r.json()["planned"]["driven_m"]
    # no body: the mission's own (now moved) starts
    assert (
        client.post(f"/api/missions/{m['id']}/replan", json={}).json()["mission"][
            "revision"
        ]
        == new["revision"] + 1
    )


def test_replan_needs_a_kept_goal(svc):
    client, _ = svc
    m = client.post("/api/missions", json={"agents": [SPOT1], "tasks": []}).json()[
        "mission"
    ]
    r = client.post(f"/api/missions/{m['id']}/replan", json={})
    assert r.status_code == 400 and "kept its goal" in r.json()["detail"]


def test_route_text_goes_through_the_chat_in_omniplanner_mode(svc, monkeypatch):
    """Without a goal, the chat's model writes it (here: a stand-in that calls the tool)."""
    client, service = svc
    from adt4_console.mission import llm_planner

    seen = {}

    def fake_stream(message, robot, src, history, model, ctx):
        seen["ctx"] = ctx
        events, _ = llm_planner.run_plan_tool({"pddl_goal": "(visited-object o0)", "assumptions": []}, ctx,
                                              service, src, message)  # fmt: skip
        yield from events

    monkeypatch.setattr(_chat(client), "stream", fake_stream)
    r = client.post(
        "/api/missions/plan/omniplanner",
        json={"text": "someone visit the car", "agents": [SPOT1, SPOT2]},
    )
    assert r.status_code == 200, r.text
    assert seen["ctx"]["planner"] == "omniplanner"
    assert r.json()["mission"]["tasks"][0]["target"] == "O(0)"


def test_route_reports_why_the_chat_did_not_plan(svc, monkeypatch):
    client, _ = svc
    monkeypatch.setattr(
        _chat(client),
        "stream",
        lambda *a: iter([{"type": "text", "text": "Which car?"}]),
    )
    r = client.post(
        "/api/missions/plan/omniplanner",
        json={"text": "visit the car", "agents": [SPOT1]},
    )
    assert r.status_code == 400 and "Which car?" in r.json()["detail"]


def _chat(client):
    from adt4_console.routes import services

    class _R:
        app = client.app

    return services(_R()).chat


def test_preview_draws_routes(svc):
    client, _ = svc
    r = client.post("/api/missions/plan/preview_pddl",
                    json={"pddl_goal": "(visited-object-by spot1 o0)", "robots": [{"name": "spot1", "start": [0, 0]}]})  # fmt: skip
    assert r.status_code == 200, r.text
    p = r.json()
    assert len(p["routes"]["spot1"]) > 10 and p["driven_m"]["spot1"] > 20
    # round the wall through its gap (x = 10, y >= 8)
    assert any(abs(x - 10) < 0.6 and y > 7.5 for x, y, _ in p["routes"]["spot1"])


def test_preview_without_poses_is_explained(svc):
    client, _ = svc
    r = client.post(
        "/api/missions/plan/preview_pddl",
        json={"pddl_goal": "(visited-object o0)", "robots": [{"name": "ghost"}]},
    )
    assert r.status_code == 400 and "no robot has a pose" in r.json()["detail"]


def test_chat_offers_the_tool_and_forces_it(svc):
    client, _ = svc
    chat = _chat(client)

    def names(ctx):
        chat.turn = ChatTurn(mission=ctx)
        return {t["name"] for t in chat._tools(None, turn=chat.turn)}

    both = names({"roster": [SPOT1], "omniplanner": chat._omniplanner_ok()})
    assert {"propose_mission", "plan_ground_mission"} <= both
    forced = names({"roster": [SPOT1], "omniplanner": True, "planner": "omniplanner"})
    assert "plan_ground_mission" in forced and "propose_mission" not in forced


def test_chat_tool_card_reports_the_plan(svc):
    _, service = svc
    from adt4_console.mission import llm_planner

    events, for_model = llm_planner.run_plan_tool(
        {"pddl_goal": "(visited-object o0)", "assumptions": ["any robot"]},
        {"roster": [SPOT1, SPOT2]},
        service,
        None,
        "visit o0",
    )
    assert (
        events[0]["type"] == "mission"
        and events[0]["planned"]["pddl_goal"] == "(visited-object o0)"
    )
    assert for_model["omniplanner"]["driven_m"] and not for_model["errors"]
    _, bad = llm_planner.run_plan_tool({"pddl_goal": "(visited-object o99)", "assumptions": []}, {"roster": [SPOT1]},
                                       service, None, "")  # fmt: skip
    assert "omniplanner could not plan" in bad["error"]


def test_failure_carries_fast_downward_account(svc):
    """O(1) is on an island: the route says why, with Fast Downward's digest and log."""
    client, service = svc
    r = client.post(
        "/api/missions/plan/omniplanner",
        json={"pddl_goal": "(visited-object o1)", "agents": [SPOT1]},
    )
    assert r.status_code == 400
    body = r.json()
    assert "o1 cannot be reached" in body["detail"]
    fd = body["fast_downward"]
    assert (
        "unreachable" in fd["digest"]
        and fd["log"]
        and fd["problem_file"].endswith(".pddl")
    )
    # the chat: a tool card for the operator, the digest for the model
    from adt4_console.mission import llm_planner

    events, for_model = llm_planner.run_plan_tool({"pddl_goal": "(visited-object o1)", "assumptions": []},
                                                  {"roster": [SPOT1]}, service, None, "")  # fmt: skip
    assert events[0]["type"] == "tool" and events[0]["result"]["fast_downward"]["log"]
    assert "unreachable" in for_model["fast_downward"]["digest"]


def test_success_reports_the_search(svc):
    _, service = svc
    _, planned = omni.plan_pddl("(visited-object o0)", [SPOT1], None, service)
    assert "solution found" in planned["fast_downward"]["digest"]


AREA = {"name": "staging", "polygon": [[2.5, 2.5], [6.5, 2.5], [6.5, 6.5], [2.5, 6.5]]}


def _inside(service, task, poly):
    import re

    import spark_dsg as dsg

    from adt4_console.mission.paths import point_in_polygon

    k, n = re.fullmatch(r"([A-Za-z])\((\d+)\)", task.target).groups()
    node = _G(service).get_node(dsg.NodeSymbol(k, int(n)))
    return point_in_polygon(node.attributes.position[:2], poly)


def test_fast_downward_plans_a_drawn_area_as_its_places(svc, monkeypatch):
    """A drawn mission area (area1) is planned with Fast Downward: the goal is spelled
    as places inside it, kept for the robots' planner (pddl_places)."""
    _, service = svc
    m, planned = omni.plan_pddl("(visited-region-by spot1 area1)", [SPOT1, SPOT2], None, service,
                                areas=[AREA])  # fmt: skip
    assert planned["engine"] == "fast_downward"
    assert [t.agent for t in m.tasks] == ["spot1"]
    assert _inside(service, m.tasks[-1], AREA["polygon"])
    assert m.goal["pddl"] == "(visited-region-by spot1 area1)"
    places = m.goal["pddl_places"]
    assert places.startswith("(or (visited-place-by spot1 ") and "area1" not in places

    # a 4 x 4 m area is one look at 8 m spacing; at 2 m it takes several
    monkeypatch.setattr(omni, "AREA_COVER_SPACING_M", 2.0)
    m, _ = omni.plan_pddl("(explored-region area1)", [SPOT1], None, service, areas=[AREA])
    assert len(m.tasks) >= 2 and all(_inside(service, t, AREA["polygon"]) for t in m.tasks)
    assert m.goal["pddl_places"].startswith("(and (visited-place ")

    m, _ = omni.plan_pddl("(in-region spot2 area1)", [SPOT1, SPOT2], None, service, areas=[AREA])
    assert m.tasks[-1].agent == "spot2" and _inside(service, m.tasks[-1], AREA["polygon"])


def test_a_drawn_area_with_no_reachable_place_is_explained(svc):
    _, service = svc
    far = {"name": "lake", "polygon": [[100, 100], [101, 100], [101, 101]]}
    with pytest.raises(ValueError, match="no place the robots can reach"):
        omni.plan_pddl("(visited-region area1)", [SPOT1], None, service, areas=[far])
    with pytest.raises(ValueError, match="visited, explored or ended in"):
        omni.plan_pddl("(visited-object area1)", [SPOT1], None, service, areas=[AREA])


def test_expand_areas_spells_each_form():
    import numpy as np

    class _Fake:  # expand_areas only needs the mesh places
        pass

    named = {"area1": AREA}
    syms = [f"p{i}" for i in range(9)]
    xy = np.array([[x, y] for x in (3, 4.5, 6) for y in (3, 4.5, 6)], float)
    comp = np.zeros(9, int)
    orig = omni._mesh_places
    omni._mesh_places = lambda G: (syms, xy, comp)
    try:
        g, used = omni.expand_areas("(and (visited-region area1) (in-region spot1 area1))", named, _Fake(),
                                    {"spot1": [0, 0]})  # fmt: skip
    finally:
        omni._mesh_places = orig
    assert "area1" not in g and "(or (visited-place p" in g and "(or (at-place spot1 p" in g
    assert set(used["area1"]) <= set(syms)


CRATE = {"label": "crate", "pos": [5.0, 5.0]}


def test_mock_objects_are_objects_to_the_planner(svc):
    """A planted mock object gets the next object symbol after the map's (the grid
    ends at O(1): O(2)) in a copy of the planner's object layer: a ground goal
    inspects or moves it; the shared graph is not changed."""
    import spark_dsg as dsg

    _, service = svc
    G = _G(service)
    assert service.next_object(service._default_src()) == 2
    m, _ = omni.plan_pddl("(inspected-by spot1 o2)", [SPOT1, SPOT2], None, service, planted=[dict(CRATE)])
    assert [(t.agent, t.type, t.target) for t in m.tasks] == [("spot1", "inspect", "O(2)")]
    assert m.planted[0].sym == "O(2)"
    assert not G.has_node(dsg.NodeSymbol("O", 2))  # only the planning copy has it
    m, _ = omni.plan_pddl(f"(object-in-place o2 {_place(G, 0, 0)})", [SPOT1], None, service,
                          planted=[dict(CRATE)])  # fmt: skip
    assert {t.type for t in m.tasks} >= {"pick", "place"}
    # a mock's symbol that is a real object elsewhere: refused, re-plan renumbers it
    with pytest.raises(ValueError, match="real object"):
        omni.plan_pddl("(visited-object o0)", [SPOT1], None, service, planted=[{**CRATE, "sym": "O(0)"}])
    moved = service.fix_mock_symbols(planted := [{**CRATE, "sym": "O(0)"}], service._default_src())
    assert moved == {"o0": "o2"} and planted[0]["sym"] == "O(2)"


def test_mock_object_targets_simulate_and_stage_on_execute(svc):
    """The simulator finds a mock target where it was planted (no missing-target
    error), and Execute stages the mock objects the goal names in the map first."""
    _, service = svc
    m, _ = omni.plan_pddl("(visited-object-by spot1 o2)", [SPOT1], None, service, planted=[dict(CRATE)])
    pv = service.preview(m)
    assert not [i for i in pv["issues"] if i["level"] == "error"], pv["issues"]
    assert not [i for i in pv["issues"] if i.get("code") == "planted_unmatched"]
    applied = []

    class _Edits:
        def apply(self, sid, ops, **kw):
            applied.append((sid, ops))

    service.edits, src = _Edits(), m.src
    m.src = "neo4j:test"
    assert service._stage_mocks(m, service.world(m), "(visited-object-by euclid o2)") == "(visited-object-by euclid o2)"
    assert applied == [("neo4j:test", [{"op": "create_node", "layer": "Object", "symbol": "O2", "class": "crate",
                                        "name": "crate", "pos": [5.0, 5.0]}])]  # fmt: skip
    m.src = src
    with pytest.raises(ValueError, match="Neo4j map"):
        service._stage_mocks(m, service.world(m), "(visited-object-by euclid o2)")


def test_execute_waits_for_the_planner_to_have_the_staged_map(svc, monkeypatch):
    _, service = svc
    seen = iter([{"map_version": 4}, {"map_version": 5}])

    class _Ros:
        available = True

        def planner_view(self):
            return next(seen, {"map_version": 5})

    service.ros = _Ros()
    monkeypatch.setattr("time.sleep", lambda s: None)
    service._await_planner_map(5)  # returns once the planner reports version 5
    service.MAP_WAIT_S = 0.0
    with pytest.raises(ValueError, match="has not received"):
        service._await_planner_map(9)
