"""Per-robot localization readiness: is the robot relocalized against the prior
map, and if not, what is in the way (the Robot view's status card and the ring
under each robot).  Console-only: everything comes from the robot's fleetlink C1
state as the console already receives it -- the robot's ros_system_monitor table
(``health.nodes``: one row per node with status nominal / warning / error /
no_hb / startup and a note), ``pose`` (the agent's ``<robot>/odom -> base_link``
TF lookup, with the TF stamp ``t``), ``frame_align`` (``map -> <robot>/odom``)
and ``reloc`` (map -> odom jumps).

One checklist (``localize``) of items ``{key, name, state, reason, source,
age_s, blocking, info}``; ``state`` is ok / warn / fail / unknown, ``reason`` a
one-line plain-words explanation, ``age_s`` how old the evidence is (None when
the source carries no time: monitor rows have no stamp).  Blocking items: the
robot link, hydra_multi's accepted inter-robot loop closures (the console's
relocalized rule: :data:`RELOC_MIN_HYDRA_LCS`), ``map -> odom`` present and the
placement not suspect.  Shown, never blocking (``info``): roman_lc inter-robot
loop closures (NO_HB between reports), tf_network (map -> robot TF),
relocalization_monitor (GPS-based: "TF map->gps not available" indoors or
without a GPS alignment), roman_map, gps / ntrip.

hydra_multi spins only when a scene graph arrives, so its row goes ERR "time
since last spin" in between and that note drops the counts:
:class:`ReadinessTracker` keeps the last count and the last optimization time it
saw, labelled with their age.

``odometry``: one extra item, the only mapping-side check kept: the agent's
pose stamp must keep advancing (no monitor row exists for odom -> base_link).
No odometry means nothing to relocalize, so it makes the verdict ``fail``.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from .routes.change_detection import (
    HYDRA_LC_RE,
    RELOC_MIN_HYDRA_LCS,
    _suspect,
    parse_hydra_optimization,
)

STATE_STALE_S = 10.0  # no C1 state for this long: the link is down
ODOM_STALE_S = 5.0  # pose stamp not advancing for this long: odometry frozen

STATUS_LABEL = {
    "nominal": "OK",
    "warning": "WARN",
    "error": "ERR",
    "no_hb": "NO_HB",
    "startup": "STARTUP",
}
_TF_FAIL_RE = re.compile(r"tf\s+lookup\s+failed|missing\s+pose|no\s+pose", re.I)
_ROMAN_LCS_RE = re.compile(r"inter-?robot\s+lcs\s*:\s*(\d+)", re.I)


def find_row(health: dict | None, robot: str, name: str) -> dict | None:
    """``<robot>/<name>`` (or bare ``name``) row of a C1 ``health`` table.
    Exact on the last path part: ``hydra`` never matches ``hydra_multi``."""
    nodes = (health or {}).get("nodes")
    if not isinstance(nodes, list):
        return None
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nick = str(n.get("nickname") or "")
        if nick in (f"{robot}/{name}", name) or nick.rsplit("/", 1)[-1] == name:
            return n
    return None


def _label(row: dict | None) -> str:
    st = str((row or {}).get("status") or "?")
    return STATUS_LABEL.get(st, st.upper())


def _note(row: dict | None) -> str:
    return str((row or {}).get("notes") or (row or {}).get("note") or "").strip()


def _row_txt(row: dict) -> str:
    note = _note(row)
    return f"{_label(row)}{f': {note}' if note else ''}"


def _item(key, name, state, reason, source, age_s=None, blocking=True, info=False) -> dict:
    return {
        "key": key,
        "name": name,
        "state": state,
        "reason": reason,
        "source": source,
        "age_s": None if age_s is None else round(float(age_s), 1),
        "blocking": bool(blocking and not info),
        "info": bool(info),
    }


def _fmt_s(s: float) -> str:
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


@dataclass
class _Memory:
    epoch: object = None
    state_at: float | None = None
    pose_t: float | None = None
    pose_seen_at: float | None = None  # first time this pose stamp was seen
    pose_samples: int = 0
    hydra_count: tuple[int, int] | None = None
    hydra_count_at: float | None = None
    opt: dict | None = None
    opt_at: float | None = None


class ReadinessTracker:
    """Remembers what a single C1 state cannot say (odometry advancing,
    hydra_multi's last count / optimization) and computes the readiness.  Thread-safe: the receiver thread observes, routes compute."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._mem: dict[str, _Memory] = {}
        self._lock = threading.Lock()

    def forget(self, robot: str) -> None:
        with self._lock:
            self._mem.pop(robot, None)

    def observe(self, robot: str, state: dict | None, now: float | None = None) -> dict:
        """Feed one C1 state (receiver thread); returns :meth:`compute` of it."""
        now = self.clock() if now is None else now
        if isinstance(state, dict):
            with self._lock:
                mem = self._mem.setdefault(robot, _Memory())
                epoch = (state.get("stats") or {}).get("epoch")
                if epoch is not None and mem.epoch is not None and epoch != mem.epoch:
                    mem = self._mem[robot] = _Memory()  # the agent restarted
                mem.epoch = epoch if epoch is not None else mem.epoch
                mem.state_at = now
                t = _pose_t(state.get("pose"))
                if t is not None:
                    if t != mem.pose_t:
                        mem.pose_t, mem.pose_seen_at = t, now
                        mem.pose_samples += 1
                row = find_row(state.get("health"), robot, "hydra_multi")
                m = HYDRA_LC_RE.search(_note(row))
                if m:
                    mem.hydra_count, mem.hydra_count_at = (int(m.group(1)), int(m.group(2))), now
                opt = parse_hydra_optimization(_note(row))
                if opt:
                    mem.opt, mem.opt_at = opt, now
        return self.compute(robot, state, now)

    def compute(self, robot: str, state: dict | None, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        with self._lock:
            mem = self._mem.get(robot) or _Memory()
            mem = _Memory(**{k: getattr(mem, k) for k in mem.__dataclass_fields__})
        return compute_readiness(robot, state, mem, now)


def _pose_t(pose) -> float | None:
    if not isinstance(pose, dict):
        return None
    t = pose.get("t")
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def _checklist(items: list[dict], no_data: bool = False) -> dict:
    blocking = [i for i in items if i["blocking"]]
    if no_data:
        st = "unknown"
    elif any(i["state"] == "fail" for i in blocking):
        st = "fail"
    elif any(i["state"] != "ok" for i in blocking):
        st = "warn"
    else:
        st = "ok"
    problems = [i for i in blocking if i["state"] == "fail"] + [
        i for i in blocking if i["state"] in ("warn", "unknown")
    ]
    return {
        "state": st,
        "ready": st == "ok",
        "items": items,
        "first_problem": problems[0]["reason"] if problems else None,
    }


# ------------------------------------------------------------- link + odometry
def _link_item(mem: _Memory, now: float) -> dict:
    if mem.state_at is None:
        return _item("link", "Robot link", "unknown", "No state received from the robot yet (fleetlink)", "fleetlink C1")
    age = now - mem.state_at
    if age > STATE_STALE_S:
        return _item("link", "Robot link", "fail", f"No state from the robot for {_fmt_s(age)}: fleetlink link down or agent stopped",
                     "fleetlink C1", age)  # fmt: skip
    return _item("link", "Robot link", "ok", "Robot state arriving over fleetlink", "fleetlink C1", age)


def _odom_item(robot: str, state: dict, mem: _Memory, now: float, link_ok: bool) -> dict:
    name = "Odometry (odom -> base_link)"
    pose = state.get("pose")
    frame = (pose or {}).get("frame") or f"{robot}/odom"
    src = f"fleetlink pose ({frame} -> base_link TF)"
    hydra, fastsam = find_row(state.get("health"), robot, "hydra"), find_row(state.get("health"), robot, "fastsam")
    hints = [f"{n} reports {_note(r)!r}" for n, r in (("hydra", hydra), ("fastsam", fastsam)) if r and _TF_FAIL_RE.search(_note(r))]
    hint = f" ({'; '.join(hints)})" if hints else ""
    if not isinstance(pose, dict):
        return _item("odom", name, "fail",
                     f"No odometry: the robot's agent cannot look up {robot}/odom -> base_link (Spot driver / DLIO not running?){hint}",
                     src)  # fmt: skip
    if mem.pose_t is None:
        return _item("odom", name, "unknown", f"The agent sends no pose stamp (older fleetlink): cannot tell if odometry is live{hint}", src)  # fmt: skip
    age = now - (mem.pose_seen_at or now)
    if not link_ok:
        return _item("odom", name, "unknown", "No fresh robot state: odometry unknown", src, age)
    if age > ODOM_STALE_S:
        return _item("odom", name, "fail",
                     f"Odometry frozen: the {frame} -> base_link TF has not advanced for {_fmt_s(age)} (Spot driver / DLIO stopped?){hint}",
                     src, age)  # fmt: skip
    if mem.pose_samples < 2:
        return _item("odom", name, "unknown", "One pose sample so far: waiting for the TF stamp to advance", src, age)
    return _item("odom", name, "ok", "Odometry TF is live (pose stamp advancing)", src, age)


# ------------------------------------------------------------- ready to localize
def hydra_multi_summary(robot: str, state: dict, mem: _Memory, now: float) -> dict:
    """hydra_multi's count and last optimization, current or remembered."""
    row = find_row((state or {}).get("health"), robot, "hydra_multi")
    note = _note(row)
    m = HYDRA_LC_RE.search(note)
    current = (int(m.group(1)), int(m.group(2))) if m else None
    count = current or mem.hydra_count
    opt_now = parse_hydra_optimization(note)
    opt = opt_now or mem.opt
    count_age = 0.0 if current else (now - mem.hydra_count_at if mem.hydra_count_at is not None else None)
    opt_age = 0.0 if opt_now else (now - mem.opt_at if mem.opt_at is not None else None)
    return {
        "accepted": count[0] if count else None,
        "total": count[1] if count else None,
        "needed": RELOC_MIN_HYDRA_LCS,
        "count_age_s": None if count_age is None else round(count_age, 1),
        "count_stale": bool(count and not current),
        "took_s": opt["took_s"] if opt else None,
        # seconds before it was reported (older notes only) + since it was reported
        "ago_s": opt["ago_s"] if opt else None,
        "opt_age_s": None if opt_age is None else round(opt_age, 1),
        "opt_stale": bool(opt and not opt_now),
        "status": _label(row) if row else None,
        "note": note if row else None,
        "in_table": row is not None,
    }


def _localize_items(robot: str, state: dict, mem: _Memory, now: float, hm: dict) -> list[dict]:
    health = state.get("health")
    need = RELOC_MIN_HYDRA_LCS
    items = []
    name = "hydra_multi loop closures"
    src = "status table: hydra_multi"
    if hm["accepted"] is not None:
        acc, tot = hm["accepted"], hm["total"]
        if acc >= need:
            st, reason = "ok", f"Relocalized: {acc} / {tot} inter-robot loop closures accepted (needs {need})"
        elif acc > 0:
            st, reason = "warn", f"Relocalizing: {acc} of the {need} needed loop closures accepted ({tot} proposed)"
        else:
            st, reason = "warn", f"No prior-map match yet: 0 / {tot} loop closures accepted (needs {need})"
        if hm["count_stale"]:
            reason += f"; last reported {_fmt_s(hm['count_age_s'] or 0)} ago (hydra_multi now {hm['status'] or 'missing'}"
            reason += ": it reports only when a scene graph arrives)" if hm["note"] and "spin" in hm["note"] else ")"
        items.append(_item("hydra_multi", name, st, reason, src, hm["count_age_s"]))
    elif not hm["in_table"]:
        items.append(_item("hydra_multi", name, "unknown",
                           "hydra_multi is not in the robot's status table (not launched for this session?)", src))  # fmt: skip
    elif "spin" in (hm["note"] or ""):
        items.append(_item("hydra_multi", name, "warn",
                           "hydra_multi idle (time since last spin): no scene graph processed, no loop-closure count yet", src))  # fmt: skip
    else:
        items.append(_item("hydra_multi", name, "warn", f"hydra_multi gives no loop-closure count ({hm['status']}: {hm['note'] or 'no note'})", src))  # fmt: skip

    fa = state.get("frame_align")
    fa_name = "Global frame (map -> odom)"
    if not isinstance(fa, dict):
        items.append(_item("frame_align", fa_name, "unknown", "The agent reports no global-frame alignment (older agent)", "fleetlink frame_align"))  # fmt: skip
    elif not fa.get("ok"):
        items.append(_item("frame_align", fa_name, "warn",
                           f"No {fa.get('parent') or 'map'} -> {fa.get('child') or robot + '/odom'} transform yet",
                           "fleetlink frame_align"))  # fmt: skip
    else:
        sus = _suspect(state.get("reloc"))
        age = fa.get("age_s") if fa.get("stamp") else None
        if sus:
            items.append(_item("frame_align", fa_name, "fail", f"Suspect placement: {sus.removeprefix('suspect: ')}", "fleetlink reloc", age))
        else:
            items.append(_item("frame_align", fa_name, "ok", f"{fa.get('parent')} -> {fa.get('child')} available", "fleetlink frame_align", age))  # fmt: skip

    roman = find_row(health, robot, "roman_lc")
    if roman is not None:
        m = _ROMAN_LCS_RE.search(_note(roman))
        if m:
            n = int(m.group(1))
            items.append(_item("roman_lc", "ROMAN loop closures", "ok" if n else "warn",
                               f"ROMAN found {n} inter-robot loop closures", "status table: roman_lc", info=True))  # fmt: skip
        else:
            items.append(_item("roman_lc", "ROMAN loop closures", "unknown", f"roman_lc {_row_txt(roman)} (normal between reports)",
                               "status table: roman_lc", info=True))  # fmt: skip
    rmap = find_row(health, robot, "roman_map")
    if rmap is not None:
        items.append(_item("roman_map", "ROMAN map", "ok" if rmap.get("status") == "nominal" else "unknown",
                           f"roman_map {_row_txt(rmap)}", "status table: roman_map", info=True))  # fmt: skip
    tfn = find_row(health, robot, "tf_network")
    if tfn is not None:
        items.append(_item("tf_network", "TF over the network", "ok" if tfn.get("status") == "nominal" else "warn",
                           f"tf_network {_row_txt(tfn)}", "status table: tf_network", info=True))  # fmt: skip
    rm = find_row(health, robot, "relocalization_monitor")
    if rm is not None:
        items.append(_item("relocalization_monitor", "GPS relocalization monitor", "ok" if rm.get("status") == "nominal" else "unknown",
                           f"{_row_txt(rm)} (GPS-based: cannot go green indoors or without a GPS alignment)",
                           "status table: relocalization_monitor", info=True))  # fmt: skip
    for key, nm in (("gps_monitor", "GPS"), ("ntrip_monitor", "NTRIP corrections")):
        row = find_row(health, robot, key)
        if row is not None:
            items.append(_item(key, nm, "ok" if row.get("status") == "nominal" else "unknown", f"{key} {_row_txt(row)}",
                               f"status table: {key}", info=True))  # fmt: skip
    return items


def compute_readiness(robot: str, state: dict | None, mem: _Memory, now: float) -> dict:
    if not isinstance(state, dict):
        return {"robot": robot, "state": "unknown", "localize": _checklist([], no_data=True), "odometry": None,
                "hydra_multi": None, "broken": [], "summary": f"No state from {robot}"}  # fmt: skip
    hm = hydra_multi_summary(robot, state, mem, now)
    link = _link_item(mem, now)
    odom = _odom_item(robot, state, mem, now, link["state"] == "ok")
    loc = _checklist([link, *_localize_items(robot, state, mem, now, hm)])
    broken = []
    if odom["state"] == "fail":
        broken.append({"key": "odom", "name": odom["name"], "state": "fail", "reason": odom["reason"]})
    for st in ("fail", "warn", "unknown"):
        for i in loc["items"]:
            if i["blocking"] and i["state"] == st:
                broken.append({"key": i["key"], "name": i["name"], "state": st, "reason": i["reason"]})
    verdict = "fail" if odom["state"] == "fail" else loc["state"]
    return {"robot": robot, "state": verdict, "localize": loc, "odometry": odom, "hydra_multi": hm, "broken": broken,
            "summary": broken[0]["reason"] if broken else "Relocalized"}  # fmt: skip
