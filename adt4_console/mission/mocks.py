"""Mock objects as objects of the planner's scene graph.

A mock object the operator plants for a mission (Mission tab -> Mock objects)
gets the next free object symbol after the map's real objects (on a map whose
objects end at O(699): O(700), O(701), ...), so a ground-robot PDDL goal names
it like any other object: ``(inspected-by spot1 o700)``,
``(object-in-place o700 t42)``. The mission's ``planted`` list is what makes a
symbol a mock object.

* Planning adds the mission's mock objects to a copy of the scene graph's object
  layer (:func:`graph_with`); the shared map is not changed.
* The simulator resolves their symbols to where they were planted, and the
  simulated quad camera still detects them by label.
* Executing a mission whose goal names one stages it in the map first, under its
  symbol (the operator puts the real object there): :func:`create_ops`. A symbol
  a real object took in the meantime is renumbered first (:func:`renumber`).
"""

from __future__ import annotations

import re

_SYM = re.compile(r"^\s*[oO]\(?\s*(\d+)\s*\)?\s*$")
STAGED_M = 1.0  # a map object this close to the planted spot is the mock, staged


def index(sym) -> int | None:
    """``O(700)`` / ``o700`` / ``O700`` -> 700 (None for anything else)."""
    m = _SYM.match(str(sym or ""))
    return int(m.group(1)) if m else None


def pddl(i: int) -> str:
    return f"o{i}"


def _get(p, k):
    return p.get(k) if isinstance(p, dict) else getattr(p, k, None)


def _put(p, k, v):
    if isinstance(p, dict):
        p[k] = v
    else:
        setattr(p, k, v)


def max_real(objects) -> int:
    """The highest real object index among ``objects`` (symbols)."""
    return max(
        (i for i in (index(s) for s in objects or ()) if i is not None), default=-1
    )


def fill_symbols(planted, next_free: int = 0):
    """Give every planted object without a symbol the next free object symbol
    (from ``next_free``, after the mission's other mock objects); in place."""
    used = {index(_get(p, "sym")) for p in planted if index(_get(p, "sym")) is not None}
    n = max([next_free - 1, *used], default=next_free - 1)
    for p in planted:
        if index(_get(p, "sym")) is None:
            n += 1
            _put(p, "sym", f"O({n})")
    return planted


def renumber(planted, taken: set[int], next_free: int) -> dict[str, str]:
    """Move mock objects whose symbol a real object took (``taken``) to the next
    free symbols; returns {old pddl: new pddl} for the goals that name them."""
    used = {index(_get(p, "sym")) for p in planted} | set(taken)
    n = max([next_free - 1, *(u for u in used if u is not None)], default=next_free - 1)
    moved = {}
    for p in planted:
        i = index(_get(p, "sym"))
        if i is not None and i in taken:
            n += 1
            _put(p, "sym", f"O({n})")
            moved[pddl(i)] = pddl(n)
    return moved


def rewrite(goal: str, moved: dict[str, str]) -> str:
    """``goal`` with the moved mock symbols replaced."""
    if not moved:
        return goal
    return re.sub(
        r"\bo\d+\b",
        lambda m: moved.get(m.group(0).lower(), m.group(0)),
        goal,
        flags=re.I,
    )


def by_index(planted) -> dict[int, tuple[str, list[float]]]:
    """{index: (label, [x, y])} of the planted objects that have a symbol."""
    out = {}
    for p in planted or []:
        i = index(_get(p, "sym"))
        if i is not None:
            out[i] = (
                str(_get(p, "label") or "object"),
                list(_get(p, "pos") or [0, 0])[:2],
            )
    return out


def is_mock(sym, planted) -> bool:
    i = index(sym)
    return i is not None and i in by_index(planted)


def position(planted, sym) -> list[float] | None:
    """Where the mock object ``sym`` (``O(700)`` / ``o700``) was planted."""
    hit = by_index(planted).get(index(sym))
    return list(hit[1]) if hit else None


def named(goal: str, planted) -> dict[int, tuple[str, list[float]]]:
    """The mission's mock objects ``goal`` names."""
    have = by_index(planted)
    toks = {int(t) for t in re.findall(r"\bo(\d+)\b", goal or "", flags=re.I)}
    return {i: have[i] for i in sorted(toks & set(have))}


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
    layer; ``G`` itself when there are none. A mock already staged in the map
    (the same symbol within ``STAGED_M`` of where it was planted) is used as it
    is; a symbol a different object holds is refused (re-plan to renumber it)."""
    if not objects:
        return G
    import numpy as np
    import spark_dsg

    H = None
    for i, (label, xy) in objects.items():
        ns = spark_dsg.NodeSymbol("O", i)
        if G.has_node(ns):
            p = np.asarray(G.get_node(ns).attributes.position)[:2]
            if float(np.hypot(*(p - np.asarray(xy, float)))) <= STAGED_M:
                continue  # staged earlier
            raise ValueError(f"O({i}) is a real object of the map now, not the mock {label}: "
                             "plan the mission again to renumber its mock objects")  # fmt: skip
        if H is None:
            H = G.clone()
        a = spark_dsg.ObjectNodeAttributes()
        a.position = np.array([float(xy[0]), float(xy[1]), _z(G, xy) + 0.3])
        a.name = label
        H.add_node(spark_dsg.DsgLayers.OBJECTS, ns.value, a)
    return G if H is None else H


def create_ops(
    objects: dict[int, tuple[str, list[float]]], have=lambda sym: False
) -> list[dict]:
    """graph_edit ops that stage the mock objects in the map under their own
    symbols (skipping those ``have`` says the map already has)."""
    ops = []
    for i, (label, xy) in objects.items():
        if have(f"O({i})"):
            continue
        ops.append({"op": "create_node", "layer": "Object", "symbol": f"O{i}", "class": label,
                    "name": label, "pos": [float(xy[0]), float(xy[1])]})  # fmt: skip
    return ops
