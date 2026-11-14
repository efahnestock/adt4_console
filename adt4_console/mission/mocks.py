"""Mock objects as objects of the planner's scene graph.

A mock object the operator plants for a mission (Mission tab -> Mock objects) is
an object of its own symbol, ``O(900001)``, ``O(900002)``, ... (``MOCK_BASE`` + n,
far above any real object), so a ground-robot PDDL goal can name it like any
other object: ``(inspected-by spot1 o900001)``, ``(object-in-place o900001 t42)``.

* Planning adds the mission's mock objects to a copy of the scene graph's object
  layer (:func:`graph_with`); the shared map is not changed.
* The simulator resolves their symbols to where they were planted, and the
  simulated quad camera still detects them by label.
* Executing a mission whose goal names one stages it in the map first (the
  operator puts the real object there): :func:`create_ops`.
"""

from __future__ import annotations

import re

MOCK_BASE = 900000
_SYM = re.compile(r"^\s*[oO]\(?\s*(\d+)\s*\)?\s*$")
PDDL_MOCK = re.compile(r"\bo(9\d{5,})\b", re.IGNORECASE)


def _index(sym) -> int | None:
    m = _SYM.match(str(sym or ""))
    return int(m.group(1)) if m else None


def is_mock(sym) -> bool:
    i = _index(sym)
    return i is not None and i > MOCK_BASE


def fill_symbols(planted):
    """Give every planted object without a symbol the next free ``O(900000+n)``,
    in order (dicts or PlantedObject; changed in place, and returned)."""

    def get(p):
        return p.get("sym") if isinstance(p, dict) else getattr(p, "sym", None)

    def put(p, s):
        if isinstance(p, dict):
            p["sym"] = s
        else:
            p.sym = s

    used = {_index(get(p)) for p in planted if get(p)}
    n = MOCK_BASE
    for p in planted:
        if get(p) and is_mock(get(p)):
            put(p, f"O({_index(get(p))})")
            continue
        n += 1
        while n in used:
            n += 1
        used.add(n)
        put(p, f"O({n})")
    return planted


def _fields(p) -> tuple[str, str, list[float]]:
    if isinstance(p, dict):
        return str(p["sym"]), str(p.get("label") or "object"), list(p.get("pos") or [0, 0])
    return str(p.sym), str(p.label), list(p.pos)


def by_symbol(planted) -> dict[int, tuple[str, list[float]]]:
    """{index: (label, [x, y])} of the planted objects (symbols filled first)."""
    out = {}
    for p in fill_symbols(list(planted or [])):
        sym, label, pos = _fields(p)
        out[_index(sym)] = (label, pos)
    return out


def position(planted, sym) -> list[float] | None:
    """Where the mock object ``sym`` (``O(900001)`` / ``o900001``) was planted."""
    i = _index(sym)
    if i is None or i <= MOCK_BASE:
        return None
    hit = by_symbol(planted).get(i)
    return list(hit[1][:2]) if hit else None


def named(goal: str, planted) -> dict[int, tuple[str, list[float]]]:
    """The planted objects ``goal`` names; ValueError for a mock symbol none has."""
    have = by_symbol(planted)
    want = {int(m) for m in PDDL_MOCK.findall(goal or "") if int(m) > MOCK_BASE}
    missing = sorted(want - set(have))
    if missing:
        raise ValueError(", ".join(f"o{i}" for i in missing)
                         + " is not a mock object of this mission (plant it in the Mission tab)")  # fmt: skip
    return {i: have[i] for i in sorted(want)}


def _z(G, xy) -> float:
    """Ground height at ``xy``: the nearest mesh place's z (0 without places)."""
    import numpy as np
    import spark_dsg

    best, z = None, 0.0
    try:
        for n in G.get_layer(spark_dsg.DsgLayers.MESH_PLACES).nodes:
            p = np.asarray(n.attributes.position)
            d = float((p[0] - xy[0]) ** 2 + (p[1] - xy[1]) ** 2)
            if best is None or d < best:
                best, z = d, float(p[2])
    except Exception:  # noqa: BLE001
        pass
    return z


def graph_with(G, objects: dict[int, tuple[str, list[float]]]):
    """A copy of ``G`` with ``objects`` ({index: (label, [x, y])}) in its object
    layer; ``G`` itself when there are none."""
    if not objects:
        return G
    import numpy as np
    import spark_dsg

    H = G.clone()
    for i, (label, xy) in objects.items():
        a = spark_dsg.ObjectNodeAttributes()
        a.position = np.array([float(xy[0]), float(xy[1]), _z(G, xy) + 0.3])
        a.name = label
        try:
            a.semantic_label = 0
        except Exception:  # noqa: BLE001
            pass
        H.add_node(spark_dsg.DsgLayers.OBJECTS, spark_dsg.NodeSymbol("O", i).value, a)
    return H


def create_ops(objects: dict[int, tuple[str, list[float]]], have=lambda sym: False) -> list[dict]:
    """graph_edit ops that stage the mock objects in the map under their own
    symbols (skipping those the map already has)."""
    ops = []
    for i, (label, xy) in objects.items():
        if have(f"O({i})"):
            continue
        ops.append({"op": "create_node", "layer": "Object", "symbol": f"O{i}", "class": label,
                    "name": label, "pos": [float(xy[0]), float(xy[1])]})  # fmt: skip
    return ops
