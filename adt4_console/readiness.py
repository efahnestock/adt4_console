"""Per-robot readiness: is the robot ready to map, ready to localize, and if not,
what is broken first (the Robot view's status card and the ring under each robot).

Everything comes from the robot's fleetlink C1 state, as the console receives
it: the robot's ros_system_monitor table (``health.nodes``: one row per node with
status nominal / warning / error / no_hb / startup and a note), ``pose`` (the
agent's ``<odom> -> base_link`` TF lookup, with the TF stamp ``t``),
``frame_align`` (``map -> <robot>/odom``), ``reloc`` (map -> odom jumps) and
``stats.dsg`` (scene graphs the agent received from Hydra).

Two checklists, each a list of items ``{key, name, state, reason, source,
age_s, blocking, info}``; ``state`` is ok / warn / fail / unknown, ``reason`` a
one-line plain-words explanation, ``age_s`` how old the evidence is (None when
the source carries no time: monitor rows have no stamp).  Items with
``info`` true are shown but never block (unreliable signals, see below).

Ready to map: the robot link, odometry (the pose stamp keeps advancing: the only
``odom -> base_link`` freshness the console can see; no monitor row exists for
it), Hydra (its row: "missing exepected modules: active_window, backend" means
Hydra gets no input, usually no odometry), scene graphs arriving, semantic
inference, FastSAM ("tf lookup failed": no camera pose), depth correction (the
fallback correction is acceptable: shown amber, not blocking).

Ready to localize: hydra_multi's accepted inter-robot loop closures (the
console's relocalized rule: :data:`RELOC_MIN_HYDRA_LCS`), and ``map -> odom``
present and not suspect.  hydra_multi spins only when a scene graph arrives, so
its row goes ERR "time since last spin" in between and that note drops the
counts: :class:`ReadinessTracker` keeps the last count and the last
optimization time it saw, labelled with their age.

Informational only: relocalization_monitor (GPS-based, "TF map->gps not
available" indoors / without a GPS alignment), zed_depth_quality (OK even when
"not measured (no lidar)"), gps / ntrip monitors, roman_lc / roman_map (NO_HB
between reports), tf_network.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from .routes.change_detection import (
    HYDRA_LC_RE,
    RELOC_MIN_HYDRA_LCS,
    _suspect,
    parse_hydra_optimization,
)

STATE_STALE_S = 10.0  # no C1 state for this long: the link is down
ODOM_STALE_S = 5.0  # pose stamp not advancing for this long: odometry frozen
DSG_STALE_S = 30.0  # no new scene graph for this long: Hydra stalled

STATUS_LABEL = {
    "nominal": "OK",
    "warning": "WARN",
    "error": "ERR",
    "no_hb": "NO_HB",
    "startup": "STARTUP",
}
_MISSING_MODULES_RE = re.compile(r"missing\s+ex\w*\s+modules?\s*:?\s*(.*)", re.I)
_TF_FAIL_RE = re.compile(r"tf\s+lookup\s+failed|missing\s+pose|no\s+pose", re.I)
_NOT_SEEN_RE = re.compile(r"not\s+yet\s+seen", re.I)
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
    dsg_received: int | None = None
    dsg_changed_at: float | None = None
    hydra_count: tuple[int, int] | None = None
    hydra_count_at: float | None = None
    opt: dict | None = None
    opt_at: float | None = None
    extra: dict = field(default_factory=dict)


class ReadinessTracker:
    """Remembers what a single C1 state cannot say (odometry advancing, scene
    graphs arriving, hydra_multi's last count / optimization) and computes the
    readiness.  Thread-safe: the receiver thread observes, routes compute."""

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
                dsg = (state.get("stats") or {}).get("dsg")
                rec = dsg.get("processed", dsg.get("received")) if isinstance(dsg, dict) else None
                if isinstance(rec, int | float):
                    if mem.dsg_received is None or rec != mem.dsg_received:
                        if mem.dsg_received is not None or rec > 0:
                            mem.dsg_changed_at = now
                        mem.dsg_received = int(rec)
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


# ------------------------------------------------------------------ ready to map
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


def _row_item(key, name, row, *, ok, missing, blocking=True, info=False, special=None) -> dict:
    """An item from one monitor row; ``special(row)`` may return
    ``(state, reason, blocking)`` for a known note."""
    src = f"status table: {(row or {}).get('nickname') or key}"
    if row is None:
        return _item(key, name, "unknown", missing, f"status table: {key}", blocking=blocking and not info, info=info)
    if special:
        got = special(row)
        if got:
            st, reason, blk = got
            return _item(key, name, st, reason, src, blocking=blk, info=info)
    status = row.get("status")
    if status == "nominal":
        return _item(key, name, "ok", ok + (f" ({_note(row)})" if _note(row) and _note(row).lower() != "ok" else ""), src, blocking=blocking, info=info)  # fmt: skip
    st = "warn" if status in ("warning", "startup") else "fail"
    return _item(key, name, st, f"{name} {_row_txt(row)}", src, blocking=blocking, info=info)


def _map_items(robot: str, state: dict, mem: _Memory, now: float) -> list[dict]:
    health = state.get("health")
    table = isinstance((health or {}).get("nodes"), list)
    link = _link_item(mem, now)
    odom = _odom_item(robot, state, mem, now, link["state"] == "ok")
    items = [link, odom]
    no_table = "The robot sends no status table (ros_system_monitor)"

    def hydra_special(row):
        note = _note(row)
        if _NOT_SEEN_RE.search(note):
            return "fail", "Hydra is not running (the monitor has not seen it yet)", True
        m = _MISSING_MODULES_RE.search(note)
        if m:
            mods = m.group(1).strip().rstrip(".") or "some modules"
            why = "there is no odometry" if odom["state"] == "fail" else "usually no odometry or no camera input"
            return "fail", f"Hydra is up but {mods} are not reporting: Hydra gets no input ({why})", True
        return None

    items.append(_row_item("hydra", "Hydra", find_row(health, robot, "hydra"), ok="Hydra running, all modules reporting",
                           missing=no_table if not table else "Hydra is not in the robot's status table",
                           special=hydra_special))  # fmt: skip
    items.append(_dsg_item(state, mem, now))
    items.append(_row_item("semantic_inference", "Semantic inference", find_row(health, robot, "semantic_inference"),
                           ok="Semantic segmentation running",
                           missing=no_table if not table else "semantic_inference is not in the robot's status table"))  # fmt: skip

    def fastsam_special(row):
        if _TF_FAIL_RE.search(_note(row)):
            why = "no odometry" if odom["state"] == "fail" else "camera or odom frame missing from TF"
            return "warn", f"FastSAM cannot look up the camera pose (TF lookup failed: {why})", True
        return None

    fs = find_row(health, robot, "fastsam")
    items.append(_row_item("fastsam", "FastSAM (open-set)", fs, ok="Open-set segmentation running",
                           missing="FastSAM is not in this robot's status table", blocking=fs is not None,
                           special=fastsam_special))  # fmt: skip

    def depth_special(row):
        note = _note(row)
        if row.get("status") == "warning" and re.search(r"fallback", note, re.I):
            return "warn", "Depth correction not calibrated: using the fallback correction (acceptable)", False
        return None

    dc = find_row(health, robot, "depth_correction")
    items.append(_row_item("depth_correction", "Depth correction", dc, ok="Depth correction calibrated",
                           missing="depth_correction is not in this robot's status table", blocking=dc is not None,
                           special=depth_special))  # fmt: skip

    def zed_special(row):
        if re.search(r"not\s+measured", _note(row), re.I):
            return "unknown", f"Depth quality not measured ({_note(row)}): OK here means nothing", False
        return None

    zq = find_row(health, robot, "zed_depth_quality")
    if zq is not None:
        items.append(_row_item("zed_depth_quality", "ZED depth quality", zq, ok="Depth quality measured OK",
                               missing="", info=True, special=zed_special))  # fmt: skip
    return items


def _dsg_item(state: dict, mem: _Memory, now: float) -> dict:
    name = "Scene graph from Hydra"
    src = "fleetlink stats.dsg"
    dsg = (state.get("stats") or {}).get("dsg")
    if not isinstance(dsg, dict) or mem.dsg_received is None:
        return _item("dsg", name, "unknown", "The agent reports no scene-graph counter", src)
    if mem.dsg_received == 0 or mem.dsg_changed_at is None:
        return _item("dsg", name, "warn", "No scene graph received from Hydra yet", src)
    age = now - mem.dsg_changed_at
    if dsg.get("last_error"):
        return _item("dsg", name, "warn", f"Scene graphs fail to decode: {dsg['last_error']}", src, age)
    if age > DSG_STALE_S:
        return _item("dsg", name, "warn", f"No new scene graph from Hydra for {_fmt_s(age)}", src, age)
    return _item("dsg", name, "ok", f"Scene graphs arriving ({mem.dsg_received} so far)", src, age)


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
            items.append(_item("frame_align", fa_name, "fail", f"Placement {sus.removeprefix('suspect: ')}", "fleetlink reloc", age))
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
        empty = _checklist([], no_data=True)
        return {"robot": robot, "map": empty, "localize": empty, "hydra_multi": None, "broken": [],
                "summary": f"No state from {robot}"}  # fmt: skip
    hm = hydra_multi_summary(robot, state, mem, now)
    mp = _checklist(_map_items(robot, state, mem, now))
    loc = _checklist(_localize_items(robot, state, mem, now, hm))
    broken = []
    for lst, cl in (("map", mp), ("localize", loc)):
        for st in ("fail", "warn", "unknown"):
            for i in cl["items"]:
                if i["blocking"] and i["state"] == st:
                    broken.append({"list": lst, "key": i["key"], "name": i["name"], "state": st, "reason": i["reason"]})
    return {"robot": robot, "map": mp, "localize": loc, "hydra_multi": hm, "broken": broken,
            "summary": broken[0]["reason"] if broken else "Ready to map and to localize"}  # fmt: skip
