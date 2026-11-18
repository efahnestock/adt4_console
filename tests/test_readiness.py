"""Localization readiness (readiness.py), with status tables modelled
on the 2026-11-15 willow logs (gauss without DLIO, hamilton without the Spot
driver, then a relocalized run)."""

from adt4_console.live_service import LiveService
from adt4_console.readiness import ReadinessTracker, find_row


def _row(nick, status, notes=""):
    return {
        "nickname": nick,
        "node_name": nick.split("/")[-1],
        "status": status,
        "notes": notes,
    }


def _state(rows, *, t=100.0, pose=True, dsg=None, fa=None, reloc=None, epoch=1):
    st = {
        "health": {"nodes": rows},
        "stats": {"epoch": epoch, **({"dsg": dsg} if dsg is not None else {})},
    }
    if pose:
        st["pose"] = {
            "x": 0,
            "y": 0,
            "z": 0,
            "qw": 1,
            "qx": 0,
            "qy": 0,
            "qz": 0,
            "frame": "gauss/odom",
            "t": t,
        }
    if fa is not None:
        st["frame_align"] = fa
    if reloc is not None:
        st["reloc"] = reloc
    return st


# the morning: no odometry (DLIO failing on gauss), every downstream node unhappy
NO_ODOM_ROWS = [
    _row("gauss/fleetlink_agent", "nominal", "ok"),
    _row("gauss/hydra", "error", "missing exepected modules: active_window, backend"),
    _row("gauss/hydra_multi", "error", "time since last spin: 12.0 [s]"),
    _row("gauss/semantic_inference", "nominal"),
    _row("gauss/fastsam", "warning", "tf lookup failed"),
    _row("gauss/roman_map", "no_hb", "Waiting for observation"),
    _row("gauss/roman_lc", "no_hb", ""),
    _row("gauss/tf_network", "warning", "No TF received on /gauss/tf"),
    _row("gauss/relocalization_monitor", "warning", "TF map->gps not available"),
    _row(
        "gauss/depth_correction",
        "warning",
        "not calibrated within 300 s: using the fallback correction",
    ),
    _row("gauss/zed_depth_quality", "nominal", "not measured (no lidar)"),
    _row("gauss/gps_monitor", "warning", "Fix (No RTK)"),
    _row("gauss/ntrip_monitor", "startup", "Waiting for first NTRIP"),
]

FA_OK = {
    "parent": "map",
    "child": "gauss/odom",
    "ok": True,
    "T": [0, 0, 0, 1, 0, 0, 0],
    "stamp": 5.0,
    "age_s": 0.2,
}


def _good_rows(hydra_multi_note, hm_status="nominal"):
    rows = [
        r
        for r in NO_ODOM_ROWS
        if r["nickname"] not in ("gauss/hydra", "gauss/fastsam", "gauss/hydra_multi")
    ]
    return rows + [
        _row("gauss/hydra", "nominal", ""),
        _row("gauss/fastsam", "nominal", ""),
        _row("gauss/hydra_multi", hm_status, hydra_multi_note),
    ]


def _item(cl, key):
    return next(i for i in cl["items"] if i["key"] == key)


def test_find_row_is_exact_on_the_node_name():
    h = {"nodes": [_row("gauss/hydra_multi", "error"), _row("gauss/hydra", "nominal")]}
    assert find_row(h, "gauss", "hydra")["nickname"] == "gauss/hydra"
    assert find_row(h, "gauss", "hydra_multi")["nickname"] == "gauss/hydra_multi"
    assert find_row(None, "gauss", "hydra") is None


def test_no_pose_is_no_odometry_and_blocks():
    tr = ReadinessTracker(clock=lambda: 0.0)
    r = tr.observe("gauss", _state(NO_ODOM_ROWS, pose=False), now=0.0)
    assert set(r) >= {"state", "localize", "odometry", "hydra_multi", "broken"}
    assert "map" not in r  # no ready-to-map checklist: localization only
    odom = r["odometry"]
    assert odom["state"] == "fail" and "No odometry" in odom["reason"]
    assert "tf lookup failed" in odom["reason"]  # fastsam corroborates
    assert r["state"] == "fail" and r["broken"][0]["key"] == "odom"
    assert r["summary"].startswith("No odometry")
    keys = [i["key"] for i in r["localize"]["items"]]
    assert keys[:3] == ["link", "hydra_multi", "frame_align"]
    for gone in (
        "hydra",
        "fastsam",
        "depth_correction",
        "zed_depth_quality",
        "semantic_inference",
        "dsg",
    ):
        assert gone not in keys


def test_frozen_pose_stamp_is_frozen_odometry():
    tr = ReadinessTracker()
    tr.observe("gauss", _state(NO_ODOM_ROWS, t=100.0), now=0.0)
    tr.observe("gauss", _state(NO_ODOM_ROWS, t=100.1), now=0.25)
    r = tr.observe("gauss", _state(NO_ODOM_ROWS, t=100.1), now=9.0)
    odom = r["odometry"]
    assert (
        odom["state"] == "fail" and "frozen" in odom["reason"] and odom["age_s"] == 8.8
    )
    assert r["state"] == "fail"


def test_link_goes_down_without_state():
    tr = ReadinessTracker()
    rows = _good_rows("7 / 9 valid interrobot loop closures")
    tr.observe("gauss", _state(rows, t=1.0, fa=FA_OK), now=0.0)
    r = tr.observe("gauss", _state(rows, t=1.2, fa=FA_OK), now=1.0)
    assert r["state"] == "ok" and r["odometry"]["state"] == "ok"
    r = tr.compute("gauss", _state(rows, t=1.2, fa=FA_OK), now=40.0)
    assert _item(r["localize"], "link")["state"] == "fail" and r["state"] == "fail"
    assert r["odometry"]["state"] == "unknown"


def test_relocalized_and_remembered_count_and_optimization():
    tr = ReadinessTracker()
    note = "7 / 9 valid interrobot loop closures; last optimization took 0.412 s"
    r = tr.observe(
        "gauss",
        _state(_good_rows(note), t=1.0, fa=FA_OK, reloc={"prior_lcs": 3}),
        now=0.0,
    )
    loc = r["localize"]
    hm = _item(loc, "hydra_multi")
    assert hm["state"] == "ok" and hm["reason"].startswith("Relocalized: 7 / 9")
    assert r["hydra_multi"]["took_s"] == 0.412 and r["hydra_multi"]["ago_s"] is None
    assert _item(loc, "frame_align")["state"] == "ok"
    # informational rows never block
    for key in (
        "relocalization_monitor",
        "tf_network",
        "roman_lc",
        "roman_map",
        "gps_monitor",
        "ntrip_monitor",
    ):
        assert not _item(loc, key)["blocking"], key
    assert loc["state"] == "ok" and r["state"] == "ok"
    # hydra_multi idles: ERR "time since last spin" drops the counts -> keep the last ones, labelled
    idle = _good_rows("time since last spin: 20.0 [s]", "error")
    r = tr.observe("gauss", _state(idle, t=1.5, fa=FA_OK), now=20.0)
    hm = _item(r["localize"], "hydra_multi")
    assert (
        hm["state"] == "ok"
        and "last reported 20 s ago" in hm["reason"]
        and hm["age_s"] == 20.0
    )
    assert "only when a scene graph arrives" in hm["reason"]
    s = r["hydra_multi"]
    assert (
        s["accepted"] == 7
        and s["count_stale"]
        and s["took_s"] == 0.412
        and s["opt_stale"]
    )
    assert s["opt_age_s"] == 20.0 and s["status"] == "ERR"


def test_relocalizing_and_not_relocalized_are_amber():
    tr = ReadinessTracker()
    r = tr.observe(
        "gauss",
        _state(_good_rows("2 / 4 valid interrobot loop closures"), fa=FA_OK),
        now=0.0,
    )
    assert _item(r["localize"], "hydra_multi")["reason"].startswith(
        "Relocalizing: 2 of the 5"
    )
    assert r["localize"]["state"] == "warn"
    tr = ReadinessTracker()
    r = tr.observe(
        "gauss",
        _state(_good_rows("time since last spin: 5 [s]", "error"), fa=FA_OK),
        now=0.0,
    )
    assert "idle" in _item(r["localize"], "hydra_multi")["reason"]
    rows = [x for x in _good_rows("") if x["nickname"] != "gauss/hydra_multi"]
    r = tr.observe("gauss", _state(rows, fa=FA_OK), now=0.0)
    assert _item(r["localize"], "hydra_multi")["state"] == "unknown"


def test_suspect_placement_fails():
    tr = ReadinessTracker()
    jumps = [{"dxy": 10.0, "dyaw": 0.0}, {"dxy": 12.0, "dyaw": 50.0}]
    r = tr.observe("gauss", _state(_good_rows("9 / 9 valid interrobot loop closures"), fa=FA_OK,
                                   reloc={"prior_lcs": 2, "recent_jumps": jumps}), now=0.0)  # fmt: skip
    fa = _item(r["localize"], "frame_align")
    assert fa["state"] == "fail" and "jumped 2x" in fa["reason"]


def test_agent_restart_forgets():
    tr = ReadinessTracker()
    tr.observe(
        "gauss",
        _state(_good_rows("6 / 6 valid interrobot loop closures"), epoch=1),
        now=0.0,
    )
    r = tr.observe(
        "gauss",
        _state(_good_rows("time since last spin: 1 [s]", "error"), epoch=2),
        now=1.0,
    )
    assert r["hydra_multi"]["accepted"] is None


def test_no_state_is_unknown():
    r = ReadinessTracker().compute("gauss", None, now=0.0)
    assert (
        r["state"] == "unknown"
        and r["localize"]["state"] == "unknown"
        and r["odometry"] is None
    )


class _Hub:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, data):
        self.msgs.append((topic, data))


class _Rx:
    def __init__(self, st):
        self.st = st

    def state(self, robot):
        return self.st

    def paths(self, robot):
        return None

    def robots(self):
        return ["gauss"]


def test_live_service_publishes_readiness_with_the_state(settings):
    hub = _Hub()
    st = _state(NO_ODOM_ROWS, pose=False)
    live = LiveService(settings, hub=hub, receiver=_Rx(st))
    live.on_update("gauss", "state", st)
    topic, data = hub.msgs[-1]
    assert topic == "robot/gauss/state"
    assert data["readiness"]["broken"][0]["key"] == "odom"
    assert data["readiness"]["state"] == "fail"
    assert "readiness" not in st  # the receiver's own state is untouched
    assert live.state("gauss")["readiness"]["state"] == "fail"
    assert live.robot_readiness("gauss")["odometry"]["state"] == "fail"


# --- Meridian RPGO sessions: meridian_rpgo in place of hydra_multi ---------------------


def _meridian_rows(verdict, counts="6 / 9"):
    rows = [r for r in _good_rows("") if r["nickname"] != "gauss/hydra_multi"]
    note = (
        f"{verdict}; {counts} inliers, 80 odom keyframes, solves: 4, last solve: 0.20 s"
    )
    return rows + [_row("gauss/meridian_rpgo", "nominal", note)]


def test_meridian_session_relocalized_without_hydra_multi():
    tr = ReadinessTracker()
    r = tr.observe(
        "gauss",
        _state(_meridian_rows("localized"), t=1.0, fa=FA_OK, reloc={"prior_lcs": 2}),
        now=0.0,
    )
    r = tr.observe(
        "gauss",
        _state(_meridian_rows("localized"), t=2.0, fa=FA_OK, reloc={"prior_lcs": 2}),
        now=1.0,
    )
    keys = [i["key"] for i in r["localize"]["items"]]
    assert "hydra_multi" not in keys and keys[:3] == [
        "link",
        "meridian_rpgo",
        "frame_align",
    ]
    m = _item(r["localize"], "meridian_rpgo")
    assert (
        m["state"] == "ok"
        and m["blocking"]
        and m["reason"] == "Relocalized: 6 / 9 loop closures inliers"
    )
    assert r["state"] == "ok" and r["summary"] == "Relocalized"
    assert (
        r["meridian_rpgo"]["state"] == "localized"
        and r["meridian_rpgo"]["inliers"] == 6
    )


def test_meridian_not_localized_blocks():
    tr = ReadinessTracker()
    r = tr.observe(
        "gauss", _state(_meridian_rows("not localized", "1 / 5"), fa=FA_OK), now=0.0
    )
    m = _item(r["localize"], "meridian_rpgo")
    assert m["state"] == "warn" and "1 / 5" in m["reason"]
    assert any(b["key"] == "meridian_rpgo" for b in r["broken"])
    # a hydra_multi session has no meridian summary
    r = tr.observe(
        "gauss",
        _state(_good_rows("7 / 9 valid interrobot loop closures"), fa=FA_OK),
        now=1.0,
    )
    assert r["meridian_rpgo"] is None
