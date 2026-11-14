"""Planner B: omniplanner's multi-robot PDDL planner, in-process.

A PDDL goal over map symbols (the chat writes it: ``(visited-object-by spot1
o12)``, ``(inspected-by spot2 o7)``, ``(at-place spot1 t40)``, ...) is planned
for every ground agent at once with omniplanner's multi-robot domain in its
goal-relevant scope: only the goal's points of interest, joined by shortest
paths in metres over the mesh-places graph, so Fast Downward assigns robots and
orders their stops by real travel (plan repair v4's domain; well under a second
on a fused multi-session map). No-go zones and forbidden places/passages are
taken out of the graph first.

The plan's ``goto-poi`` actions become ``goto`` tasks (an ``inspect`` of the
target just reached turns that goto into an ``inspect``), so the preview, the
simulator and the live executor treat them like any other mission. A pick /
start-drag at the object just reached turns that goto into a ``pick`` /
``drag`` task, and a place / release-drag into a ``place`` (``params.object``,
``params.dragged``); the console's live executor cannot send those (NOT_LIVE),
the robots' omniplanner executor can. Ground robots only.

Two engines plan these goals (``engine``): ``fast_downward`` (above) and
``grstaps`` (GRSTAPS-X, :mod:`.grstaps_engine`: task allocation + a timed
schedule, coalition inspections, ``before`` ordering, relocation). Failures
carry ``planner_log`` = {planner, digest, log, problem_file} for either; the
Fast Downward one is also kept under its old name ``fast_downward``.
"""

from __future__ import annotations

import logging
import re
import shutil
import time

import numpy as np

from .model import Mission, Task, new_id
from .paths import point_in_polygon, polygon_centroid

log = logging.getLogger(__name__)

DOMAIN = "RegionObjectRearrangementDomain_MultiRobot_FD_Explore"
SUPPORTED = (
    "goto-poi",
    "inspect",
    "pick-object",
    "place-object",
    "start-drag",
    "release-drag",
)
# FD manipulation action -> console task type; the object is the action's 2nd argument
GRAB = {"pick-object": "pick", "start-drag": "drag"}
DROP = {"place-object": False, "release-drag": True}  # -> dragged?
ENGINES = {"fast_downward": "Fast Downward", "grstaps": "GRSTAPS-X"}
DEFAULT_ENGINE = "fast_downward"
CONSTRAINTS = {
    "forbidden-poi": 1,
    "forbidden-edge": 2,
    "before": 2,
}  # predicate -> arity


class OmniUnavailable(Exception):
    pass


class PlanningFailed(ValueError):
    """No plan, with the planner's account when it ran: ``planner_log`` =
    {planner, digest, log (last lines), problem_file} or None (grounding failed
    first). ``fast_downward`` is the same account for the Fast Downward engine
    (None for the others), the name the API used before GRSTAPS-X."""

    def __init__(self, message: str, fast_downward: dict | None = None, planner_log: dict | None = None,
                 engine: str = DEFAULT_ENGINE):  # fmt: skip
        super().__init__(message)
        self.engine = engine
        if planner_log is None and fast_downward is not None:
            planner_log = {"planner": ENGINES["fast_downward"], **fast_downward}
        self.planner_log = planner_log
        self.fast_downward = fast_downward if engine == "fast_downward" else None


def check_engine(engine: str | None) -> str:
    engine = engine or DEFAULT_ENGINE
    if engine not in ENGINES:
        raise ValueError(f"unknown planner {engine!r} (one of {', '.join(ENGINES)})")
    return engine


def _modules():
    try:
        from importlib.resources import as_file, files

        import dsg_pddl.domains
        import dsg_pddl.dsg_pddl_grounding_multirobot as grounding
        import dsg_pddl.dsg_pddl_planning as planning
        from dsg_pddl.pddl_grounding import (
            ConstraintFact,
            MultiRobotPddlDomain,
            PddlGoal,
        )
        from dsg_pddl.pddl_utils import pddl_symbol_to_node_symbol
    except ImportError as e:
        raise OmniUnavailable(f"omniplanner is not importable: {e}") from None
    if not hasattr(grounding, "generate_goal_relevant_multirobot_pddl"):
        raise OmniUnavailable(
            "this omniplanner has no goal-relevant multi-robot grounding"
        )
    if (
        shutil.which("fast-downward") is None
        and shutil.which("fast-downward.py") is None
    ):
        raise OmniUnavailable("Fast Downward is not on PATH")
    with as_file(files(dsg_pddl.domains).joinpath(DOMAIN + ".pddl")) as path:
        domain = MultiRobotPddlDomain(open(path).read())
    domain.scene_scope = "goal_relevant"
    return {
        "domain": domain,
        "ground_problem": grounding.ground_problem,
        "make_plan": planning.make_plan,
        "ConstraintFact": ConstraintFact,
        "PddlGoal": PddlGoal,
        "to_node": pddl_symbol_to_node_symbol,
    }


def available(engine: str = DEFAULT_ENGINE) -> str | None:
    """None when planning with ``engine`` works here, else why not."""
    if check_engine(engine) == "grstaps":
        from . import grstaps_engine

        return grstaps_engine.available()
    try:
        _modules()
    except OmniUnavailable as e:
        return str(e)
    return None


def engines() -> list[dict]:
    """[{id, label, available, reason}] for the planner choice in the UI. GRSTAPS-X
    is optional: listed only when it is installed here (greyed out, with the
    reason, when e.g. its Gurobi license is missing)."""
    out = []
    for eid, label in ENGINES.items():
        if eid == "grstaps":
            from . import grstaps_engine

            if not grstaps_engine.installed():
                continue
        reason = available(eid)
        out.append(
            {"id": eid, "label": label, "available": reason is None, "reason": reason}
        )
    return out


def _constraint_facts(constraints, ConstraintFact):
    facts = []
    for c in constraints or []:
        pred, syms = c["predicate"], [str(s).lower() for s in c["symbols"]]
        if pred not in CONSTRAINTS:
            raise ValueError(
                f"unknown constraint {pred!r} (one of {', '.join(CONSTRAINTS)})"
            )
        if len(syms) != CONSTRAINTS[pred]:
            raise ValueError(
                f"{pred} takes {CONSTRAINTS[pred]} symbol(s), got {len(syms)}"
            )
        facts.append(ConstraintFact(predicate=pred, symbols=syms))
    return facts


def pddl_name(agent_id: str) -> str:
    return agent_id.lower()


# Operator-drawn mission areas are named area1, area2, ... in the order the
# mission lists them (merged_areas shows the model the same symbols). Only
# GRSTAPS-X plans them: it takes a polygon as a region; Fast Downward's regions
# are the map's Room nodes.
AREA_SYMBOL = re.compile(r"\barea\d+\b", re.IGNORECASE)


def area_symbols(areas) -> dict[str, dict]:
    """{area1: {name, polygon}, ...} for drawn areas ([{name, polygon}] or, as a
    mission keeps them, [{symbol, name, polygon}])."""
    out, n = {}, 0
    for a in areas or []:
        if not isinstance(a, dict) or len(a.get("polygon") or []) < 3:
            continue
        n += 1
        sym = str(a.get("symbol") or f"area{n}").lower()
        out[sym] = {"name": str(a.get("name") or sym),
                    "polygon": [[float(x), float(y)] for x, y in a["polygon"]]}  # fmt: skip
    return out


def goal_areas(goal: str, areas) -> dict[str, dict]:
    """The drawn areas ``goal`` names: {symbol: {name, polygon}}; ValueError for an
    area symbol no drawn area has."""
    known = area_symbols(areas)
    named = sorted({m.lower() for m in AREA_SYMBOL.findall(goal or "")})
    missing = [s for s in named if s not in known]
    if missing:
        raise ValueError(f"{', '.join(missing)} is not a drawn mission area"
                         + (f" (drawn: {', '.join(known)})" if known else " (none are drawn)"))  # fmt: skip
    return {s: known[s] for s in named}


# Drawn mission areas with Fast Downward: the area's atoms become atoms over the
# reachable mesh places inside its outline, so the goal is ordinary PDDL the
# robots' planner (and plan repair's goal manager) can take as it is.
AREA_CORE_PLACES = 6  # a visit / end position: any of this many central places
AREA_COVER_SPACING_M = 8.0  # explored: visit places about this far apart ...
AREA_COVER_MAX = 30  # ... at most this many
_AREA_ATOM = re.compile(
    r"\(\s*(visited-region|explored-region|visited-region-by|explored-region-by|in-region)"
    r"((?:\s+[A-Za-z0-9_-]+)*?)\s+(area\d+)\s*\)",
    re.IGNORECASE,
)


def _mesh_places(G):
    """(symbols, xy [n, 2], component id per place) of the mesh places layer."""
    import spark_dsg

    layer = G.get_layer(spark_dsg.DsgLayers.MESH_PLACES)
    ids, xy = [], []
    for n in layer.nodes:
        ids.append(n.id.value)
        xy.append(np.asarray(n.attributes.position)[:2])
    index = {v: i for i, v in enumerate(ids)}
    parent = list(range(len(ids)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e in layer.edges:
        a, b = index.get(e.source), index.get(e.target)
        if a is not None and b is not None:
            parent[find(a)] = find(b)
    comp = np.array([find(i) for i in range(len(ids))])
    syms = [spark_dsg.NodeSymbol(v).str(True).lower() for v in ids]
    return syms, np.asarray(xy, dtype=float).reshape(-1, 2), comp


def _cover(xy: np.ndarray, spacing: float, cap: int) -> list[int]:
    """Indices of places about ``spacing`` apart covering ``xy`` (farthest-point)."""
    if not len(xy):
        return []
    centre = xy.mean(axis=0)
    chosen = [int(np.argmin(np.linalg.norm(xy - centre, axis=1)))]
    d = np.linalg.norm(xy - xy[chosen[0]], axis=1)
    while len(chosen) < cap and d.max() > spacing:
        k = int(np.argmax(d))
        chosen.append(k)
        d = np.minimum(d, np.linalg.norm(xy - xy[k], axis=1))
    return chosen


def expand_areas(goal: str, named: dict, G, starts: dict, geofences=None) -> tuple[str, dict]:
    """``goal`` with every drawn area (area1, ...) replaced by mesh places inside it,
    for Fast Downward: (visited-region area1) -> (or (visited-place t..) ...) over a
    few central places, (in-region r area1) -> (or (at-place r t..) ...), and
    (explored-region area1) -> (and (visited-place t..) ...) over places about
    ``AREA_COVER_SPACING_M`` apart (the -by forms keep their robot). Only places the
    robots can reach (their mesh-places component) and outside no-go zones count.
    Returns (goal, {area: [place symbols]}); ValueError for an area with none."""
    if not named:
        return goal, {}
    syms, xy, comp = _mesh_places(G)
    if not len(syms):
        raise ValueError("the map has no mesh places to plan drawn areas on")
    reach = {int(comp[int(np.argmin(np.linalg.norm(xy - np.asarray(p, float)[:2], axis=1)))])
             for p in starts.values()}  # fmt: skip
    fences = [g["polygon"] if isinstance(g, dict) else g.polygon for g in geofences or []]
    places: dict[str, list[int]] = {}
    for sym, a in named.items():
        poly = a["polygon"]
        inside = [i for i in range(len(syms))
                  if int(comp[i]) in reach and point_in_polygon(xy[i], poly)
                  and not any(point_in_polygon(xy[i], f) for f in fences)]  # fmt: skip
        if not inside:
            raise ValueError(f"{sym} ({a.get('name') or sym}) has no place the robots can reach inside it")
        places[sym] = inside
    used: dict[str, list[str]] = {}

    def core(sym):
        idx = places[sym]
        P = xy[idx]
        centre = np.asarray(polygon_centroid(named[sym]["polygon"]), float)
        order = np.argsort(np.linalg.norm(P - centre, axis=1))
        half = order[: max(1, (len(order) + 1) // 2)]
        pick = [idx[int(i)] for i in half[_spread(len(half), AREA_CORE_PLACES)]]
        # and the core place nearest each robot, so a visit need not drive to the middle
        for p in starts.values():
            k = half[int(np.argmin(np.linalg.norm(P[half] - np.asarray(p, float)[:2], axis=1)))]
            pick.append(idx[int(k)])
        return list(dict.fromkeys(pick))

    def cover(sym):
        idx = places[sym]
        return [idx[k] for k in _cover(xy[idx], AREA_COVER_SPACING_M, AREA_COVER_MAX)]

    def sub(m):
        pred, args, sym = m.group(1).lower(), m.group(2).split(), m.group(3).lower()
        if sym not in places:
            return m.group(0)
        robot = args[0] if args else None
        explored = pred.startswith("explored")
        chosen = cover(sym) if explored else core(sym)
        used[sym] = [syms[i] for i in chosen]
        if pred == "in-region":
            atoms = [f"(at-place {robot} {syms[i]})" for i in chosen]
        elif robot:
            atoms = [f"(visited-place-by {robot} {syms[i]})" for i in chosen]
        else:
            atoms = [f"(visited-place {syms[i]})" for i in chosen]
        if len(atoms) == 1:
            return atoms[0]
        return f"({'and' if explored else 'or'} {' '.join(atoms)})"

    out = _AREA_ATOM.sub(sub, goal)
    left = sorted({s.lower() for s in AREA_SYMBOL.findall(out)} & set(named))
    if left:
        raise ValueError(f"{', '.join(left)}: a drawn area can be visited, explored or ended in "
                         "(visited-region / explored-region / in-region and their -by forms) only")  # fmt: skip
    return out, used


def _spread(n: int, k: int) -> np.ndarray:
    """``k`` indices spread evenly over range(n) (all of them when n <= k)."""
    if n <= k:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, k).round().astype(int))


def _geofence_constraints(G, geofences, ConstraintFact):
    """forbidden-poi for every mesh place inside a no-go polygon."""
    import spark_dsg

    polys = [
        g["polygon"] if isinstance(g, dict) else g.polygon for g in geofences or []
    ]
    if not polys:
        return []
    out = []
    for node in G.get_layer(spark_dsg.DsgLayers.MESH_PLACES).nodes:
        xy = np.asarray(node.attributes.position)[:2]
        if any(point_in_polygon(xy, p) for p in polys):
            out.append(
                ConstraintFact(
                    predicate="forbidden-poi", symbols=[node.id.str(True).lower()]
                )
            )
    return out


def _height_lookup(G):
    """(x, y) -> z of the nearest mesh place, to draw routes on the ground."""
    import spark_dsg

    pos = np.array(
        [
            n.attributes.position
            for n in G.get_layer(spark_dsg.DsgLayers.MESH_PLACES).nodes
        ]
    )
    if len(pos) == 0:
        return lambda x, y: 0.0
    try:
        from scipy.spatial import cKDTree

        tree = cKDTree(pos[:, :2])
        return lambda x, y: float(pos[tree.query([x, y])[1], 2])
    except ImportError:
        return lambda x, y: float(
            pos[np.argmin(np.hypot(pos[:, 0] - x, pos[:, 1] - y)), 2]
        )


def _solve_grstaps(
    goal, starts, G, constraints, geofences, kinds=None, areas=None
) -> dict:
    from dsg_pddl.pddl_grounding import ConstraintFact

    from . import grstaps_engine

    facts = _constraint_facts(constraints, ConstraintFact) + _geofence_constraints(
        G, geofences, ConstraintFact
    )
    try:
        out = grstaps_engine.solve(goal, starts, G, facts, _height_lookup(G), robot_types=kinds,
                                   region_polygons={s: a["polygon"] for s, a in (areas or {}).items()})  # fmt: skip
    except grstaps_engine.GrstapsFailed as e:
        raise PlanningFailed(
            str(e), planner_log=e.planner_log, engine="grstaps"
        ) from None
    if out.get("left_out"):
        out["left_out"] = sorted(out["left_out"])
    return out


def solve(
    goal: str,
    starts: dict,
    G,
    constraints=None,
    geofences=None,
    engine: str = DEFAULT_ENGINE,
    kinds: dict | None = None,
    areas=None,
    planted=None,
) -> dict:
    """Plan ``goal`` for robots at ``starts`` ({pddl robot name: [x, y]}).

    Returns {actions: [(robot, action tuple, path [[x, y, z], ...])], routes:
    {robot: [[x, y, z], ...]}, driven_m: {robot: m}, ground_s, solve_s, pois};
    GRSTAPS-X adds ``steps`` (tasks with dependencies), ``schedule`` and
    ``regions``. ``areas``: the operator's drawn mission areas (see
    :func:`area_symbols`), which a GRSTAPS-X goal may name as regions.
    Raises ValueError for a goal that cannot be planned (the reason in words).
    """
    from . import mocks

    # mock objects the goal names join a copy of the graph's object layer
    G = mocks.graph_with(G, mocks.named(goal, planted))
    named = goal_areas(goal, areas)
    if check_engine(engine) == "grstaps":
        return _solve_grstaps(goal, starts, G, constraints, geofences, kinds, named)
    area_goal, area_places = expand_areas(goal, named, G, starts, geofences)
    om = _modules()
    if any(c["predicate"] == "before" for c in constraints or []):
        raise ValueError(
            "Fast Downward cannot order targets (before): plan it with GRSTAPS-X"
        )
    facts = _constraint_facts(constraints, om["ConstraintFact"])
    facts += _geofence_constraints(G, geofences, om["ConstraintFact"])
    robots = {r: np.asarray(p, dtype=float)[:2] for r, p in starts.items()}
    first = next(iter(robots))
    t0 = time.perf_counter()
    try:
        extra = {}
        fields = getattr(om["PddlGoal"], "__dataclass_fields__", {})
        no_arm = {r for r in robots if (kinds or {}).get(r) == "husky"}
        if kinds and "robot_types" in fields:  # older omniplanner (with GRSTAPS-X)
            extra["robot_types"] = {
                r: kinds.get(r, "spot") for r in robots
            }  # husky -> no arm
        elif no_arm and "world_state" in fields:
            # omniplanner main: robots without an arm are the world state's
            # cannot_manipulate (only the others get can-manipulate)
            from omniplanner.world_state import WorldState

            extra["world_state"] = WorldState(cannot_manipulate=no_arm)
        goal_obj = om["PddlGoal"](
            pddl_goal=area_goal, robot_id=first, constraints=facts, **extra
        )
        grounded = om["ground_problem"](om["domain"], G, robots, goal_obj).value
        t1 = time.perf_counter()
        plan = om["make_plan"](grounded, G)
    except Exception as e:  # noqa: BLE001 — grounding / Fast Downward failures, in words
        first_line = str(e).splitlines()[0][:600] if str(e) else "no plan"
        fd = None
        if getattr(e, "fd_log", None) is not None:
            fd = {"digest": getattr(e, "fd_digest", ""), "log": list(e.fd_log),
                  "problem_file": getattr(e, "problem_file", None)}  # fmt: skip
        log.warning("omniplanner could not plan %s: %s%s", goal, first_line,
                    "".join(f"\n  | {line}" for line in (fd or {}).get("log", [])[-15:]))  # fmt: skip
        raise PlanningFailed(
            f"{type(e).__name__}: {first_line}", fd, engine="fast_downward"
        ) from None
    t2 = time.perf_counter()
    stats = getattr(grounded, "fd_stats", None) or {}
    z_of = _height_lookup(G)
    actions, routes, driven = [], {}, {}
    for act, path in zip(plan.symbolic_actions, plan.parameterized_actions):
        robot = str(act[1]) if len(act) > 1 else first
        pts = [[float(p[0]), float(p[1])] for p in path] if act[0] == "goto-poi" else []
        pts3 = [[x, y, z_of(x, y)] for x, y in pts]
        actions.append((robot, tuple(str(a) for a in act), pts3))
        if pts3:
            route = routes.setdefault(robot, [])
            for (
                p
            ) in pts3:  # no repeated points (where hops meet): they break line drawing
                if not route or p[:2] != route[-1][:2]:
                    route.append(p)
            driven[robot] = driven.get(robot, 0.0) + sum(
                float(np.hypot(b[0] - a[0], b[1] - a[1])) for a, b in zip(pts, pts[1:])
            )
    pois = sum(1 for s in grounded.symbols.values() if s.layer in ("place", "object"))
    return {"actions": actions, "routes": routes, "driven_m": driven, "ground_s": t1 - t0,
            "solve_s": t2 - t1, "pois": pois, "fast_downward": stats,
            "area_goal": area_goal if area_places else None, "area_places": area_places,
            "planner_log": {"planner": ENGINES["fast_downward"], **stats} if stats else None}  # fmt: skip


def _grstaps_tasks(steps, back) -> list[Task]:
    """GRSTAPS-X steps (see grstaps_engine) -> console tasks; ``after`` keys -> depends_on."""
    ids = {s["key"]: new_id("t") for s in steps if back.get(s["robot"])}
    tasks = []
    for s in steps:
        aid = back.get(s["robot"])
        if aid is None:
            continue
        params = dict(s.get("params") or {})
        if s.get("dwell_s") is not None:
            params["dwell_s"] = s["dwell_s"]
        params["planned"] = [round(s["t_start"], 1), round(s["t_end"], 1)]
        tasks.append(Task(id=ids[s["key"]], agent=aid, type=s["type"], target=s["target"], note=s["note"],
                          depends_on=[ids[k] for k in s["after"] if k in ids], params=params))  # fmt: skip
    return tasks


def plan_pddl(goal: str, agents: list[dict], src: str | None, service, constraints=None,
              geofences=None, text: str = "", name: str = "",
              engine: str = DEFAULT_ENGINE, areas=None, planted=None) -> tuple[Mission, dict]:  # fmt: skip
    """A mission for the ground agents that achieves ``goal`` (see :func:`solve`).
    The drawn ``areas`` the goal names are kept with it (``goal.areas``), so a
    replan plans the same polygons."""
    from .planning import start_of

    engine = check_engine(engine)
    src = src or service._default_src()
    m0 = Mission(text=text or goal, name=name, src=src, agents=agents, planner="omniplanner",
                 geofences=geofences or [], planted=[p if isinstance(p, dict) else p.model_dump() for p in planted or []],
                 goal={"pddl": goal, "constraints": list(constraints or []), "engine": engine})  # fmt: skip
    named = goal_areas(goal, areas)
    if named:
        m0.goal["areas"] = [{"symbol": s, **a} for s, a in named.items()]
    ground = [a for a in m0.agents if a.is_ground]
    if not ground:
        raise ValueError("omniplanner plans ground robots (spot, husky) only")
    world = service.world(m0)
    if world.graph is None or world.graph.G is None:
        raise ValueError("no scene graph to plan on")
    G = world.graph.G
    starts, back = {}, {}
    for a in ground:
        s = start_of(m0, world, a.id)
        if s is None:
            raise ValueError(
                f"{a.id} has no start (give it one, or bind it to a robot with a pose)"
            )
        starts[pddl_name(a.id)] = s[:2]
        back[pddl_name(a.id)] = a.id
    kinds = {pddl_name(a.id): a.kind for a in ground}
    out = solve(
        goal,
        starts,
        G,
        constraints,
        m0.geofences,
        engine=engine,
        kinds=kinds,
        areas=m0.goal.get("areas"),
        planted=m0.planted,
    )
    from . import mocks

    G = mocks.graph_with(G, mocks.named(goal, m0.planted))  # to_node sees them too
    if engine == "grstaps":
        m0.tasks = _grstaps_tasks(out["steps"], back)
        if not m0.tasks:
            raise ValueError("the goal is already satisfied: the plan has no actions")
        sched = out["schedule"]
        m0.goal["schedule"] = {
            **sched,
            "rows": [
                {**r, "robot": back.get(r["robot"], r["robot"])} for r in sched["rows"]
            ],
        }
        info = {"driven_m": {back.get(r, r): round(v, 1) for r, v in out["driven_m"].items()},
                "planning_s": round(out["solve_s"], 2), "points_of_interest": out["pois"], "engine": engine,
                "makespan_s": sched["makespan"], "planner_log": out["planner_log"], "fast_downward": None,
                "left_out": [back.get(r, r) for r in out.get("left_out", [])],
                "regions": out.get("regions") or {}}  # fmt: skip
        return Mission.model_validate(m0.model_dump()), info
    unsupported = sorted(
        {act[0] for _, act, _ in out["actions"] if act[0] not in SUPPORTED}
    )
    if unsupported:
        raise ValueError(f"the plan needs {', '.join(unsupported)}, which the console's robots cannot "
                         "execute; plan visits, inspections and end positions only")  # fmt: skip
    to_node = _modules()["to_node"]
    tasks: list[Task] = []
    last: dict[str, Task] = {}
    for robot, act, _ in out["actions"]:
        aid = back.get(robot)
        if aid is None:
            continue
        target = str(to_node(G, act[-1]))  # o12 -> O(12), the form the console resolves
        prev = last.get(aid)
        at_prev = prev is not None and prev.type == "goto"
        if act[0] == "inspect" and at_prev and prev.target == target:
            prev.type = (
                "inspect"  # goto then inspect = the console's inspect (goto + dwell)
            )
            prev.note += f"; {' '.join(act)}"
            continue
        if act[0] in GRAB or act[0] in DROP:
            obj = str(to_node(G, act[2]))
            if act[0] in GRAB:
                typ, tgt, params = GRAB[act[0]], obj, {}
            else:
                typ, tgt, params = (
                    "place",
                    target,
                    {"object": obj, **({"dragged": True} if DROP[act[0]] else {})},
                )
            if (
                at_prev and prev.target == tgt
            ):  # the goto that got there becomes the manipulation
                prev.type, prev.params = typ, {**prev.params, **params}
                prev.note += f"; {' '.join(act)}"
                continue
            t = Task(
                id=new_id("t"),
                agent=aid,
                type=typ,
                target=tgt,
                note=" ".join(act),
                params=params,
            )
            tasks.append(t)
            last[aid] = t
            continue
        t = Task(id=new_id("t"), agent=aid, type="goto" if act[0] == "goto-poi" else "inspect",
                 target=target, note=" ".join(act))  # fmt: skip
        tasks.append(t)
        last[aid] = t
    if not tasks:
        raise ValueError("the goal is already satisfied: the plan has no actions")
    m0.tasks = tasks
    if out.get("area_goal"):
        # the goal as the robots' planner takes it: drawn areas spelled as places
        m0.goal["pddl_places"] = out["area_goal"]
    info = {"driven_m": {back.get(r, r): round(v, 1) for r, v in out["driven_m"].items()},
            "planning_s": round(out["ground_s"] + out["solve_s"], 2), "points_of_interest": out["pois"],
            "fast_downward": out["fast_downward"], "engine": engine,
            "planner_log": out["planner_log"]}  # fmt: skip
    return Mission.model_validate(m0.model_dump()), info


def _goals_by_robot(out: dict, G) -> dict[str, list[str]]:
    """{robot: [node symbol, ...]}: where each robot's plan goes, in order (the chat
    shows each goal's image). GRSTAPS-X steps already name nodes; Fast Downward's
    goto / inspect name PDDL symbols."""
    from .planning import end_pose

    goals: dict[str, list[dict]] = {}
    last: dict[str, tuple] = {}

    def add(robot, sym, path):
        xy, yaw = end_pose(path, (last.get(robot) or (None, None))[1])
        if xy is None:
            xy, yaw = last.get(robot, (None, yaw))
        last[robot] = (xy, yaw)
        lst = goals.setdefault(robot, [])
        if sym and all(g["sym"] != sym for g in lst):
            lst.append({"sym": sym, "xy": xy, "yaw": yaw})

    if "steps" in out:  # GRSTAPS-X: one action per step, in the same order
        for s, (_, _, path) in zip(out["steps"], out["actions"]):
            add(s["robot"], s["target"], path)
        return goals
    to_node = _modules()["to_node"]
    for robot, act, path in out["actions"]:
        if act[0] in ("goto-poi", "inspect") and len(act) > 2:
            try:
                sym = str(to_node(G, act[-1]))
            except Exception:  # noqa: BLE001 — a symbol the graph does not know
                sym = None
            add(robot, sym, path)
    return goals


def preview(
    goal: str,
    robots: list[dict],
    src: str | None,
    service,
    constraints=None,
    engine: str = DEFAULT_ENGINE,
    areas=None,
) -> dict:
    """What omniplanner would do with ``goal`` for these robots, without a mission:
    {routes, driven_m, actions, robots, ground_s, solve_s, pois}. ``robots``:
    [{name, start?}] (no start: the robot's live pose)."""
    from .planning import start_of

    src = src or service._default_src()
    m0 = Mission(src=src, agents=[{"id": r["name"], "kind": r.get("kind") or "spot", "binding": r["name"],
                                   "start": r.get("start")} for r in robots])  # fmt: skip
    world = service.world(m0)
    if world.graph is None or world.graph.G is None:
        raise ValueError("no scene graph to plan on")
    starts = {}
    for a in m0.agents:
        s = start_of(m0, world, a.id)
        if s is not None:
            starts[pddl_name(a.id)] = s[:2]
    if not starts:
        raise ValueError(
            "no robot has a pose: connect the robots, or add agents with starts in the mission planner"
        )
    kinds = {pddl_name(a.id): a.kind for a in m0.agents}
    out = solve(
        goal,
        starts,
        world.graph.G,
        constraints,
        engine=engine,
        kinds=kinds,
        areas=areas,
    )
    return {
        "goals": _goals_by_robot(out, world.graph.G),
        "engine": check_engine(engine),
        "schedule": out.get("schedule"),
        "planner_log": out.get("planner_log"),
        "routes": out["routes"],
        "driven_m": {r: round(v, 1) for r, v in out["driven_m"].items()},
        "actions": [{"robot": r, "action": " ".join(a)} for r, a, _ in out["actions"]],
        "robots": sorted(starts),
        "ground_s": round(out["ground_s"], 3),
        "solve_s": round(out["solve_s"], 3),
        "pois": out["pois"],
        "fast_downward": out.get("fast_downward"),
        "regions": out.get("regions") or {},
        "left_out": out.get("left_out") or [],
    }
