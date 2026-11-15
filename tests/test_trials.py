"""Trials: fleet-wide start / status / flags / stop, the event log, and the
operator request log.  The machines' ``adt4-rec`` is a fake behind ssh_cmd."""

import json
import pathlib
import shlex

import pytest

from adt4_console import trials as tr

from .conftest import make_client

GB = 10**9
ROBOTS = ["euclid", "gauss", "hamilton"]


class FakeRec:
    """ssh_cmd that answers like adt4-rec on each machine (keyed by ip)."""

    def __init__(self):
        self.calls = []  # (ip, [args after --output-root ROOT])
        self.rec = {}  # ip -> {"trial", "bytes"}
        self.unreachable = set()
        self.missing = set()
        self.fail_start = {}  # ip -> error
        self.stubborn = set()
        self.free = 200 * GB
        self.rate = 50_000_000  # bytes per status call
        self.t = 1_794_800_000.0
        self.silent = ["/x/odom"]
        self.missing_topics = []

    def ssh_cmd(self, user, ip, cmd, timeout=5):
        argv = shlex.split(cmd)
        assert (
            argv[0] == "python3"
            and argv[1].endswith("adt4-rec")
            and argv[2] == "--output-root"
        )
        args = argv[4:]
        self.calls.append((ip, args))
        if ip in self.unreachable:
            return 255, "", "ssh: connect to host: Connection timed out"
        if ip in self.missing:
            return (
                2,
                "",
                "python3: can't open file '/home/swarm/dcist_ws/.../adt4-rec': [Errno 2] No such file or directory",
            )
        self.t += 0.5
        verb = args[0]
        opt = {
            args[i]: args[i + 1]
            for i in range(1, len(args) - 1)
            if args[i].startswith("--")
        }
        rec = self.rec.get(ip)
        if verb == "status":
            out = {"ok": True, "t": self.t, "recording": rec is not None, "free_bytes": self.free,
                   "total_bytes": 900 * GB, "other_recorders": [], "stack_running": True}  # fmt: skip
            if rec:
                rec["bytes"] += self.rate
                out.update(
                    trial=rec["trial"],
                    bytes=rec["bytes"],
                    last_write_age_s=0.2,
                    pid=4242,
                )
            return 0, "noise from .zshrc\n" + json.dumps(out), ""
        if verb == "start":
            if ip in self.fail_start:
                return 1, json.dumps({"ok": False, "error": self.fail_start[ip]}), ""
            if rec:
                return (
                    3,
                    json.dumps(
                        {
                            "ok": False,
                            "error": "already recording",
                            "recording": True,
                            "trial": rec["trial"],
                        }
                    ),
                    "",
                )
            self.rec[ip] = {"trial": opt["--trial"], "bytes": 1000}
            return 0, json.dumps({"ok": True, "recording": True, "trial": opt["--trial"], "machine": opt["--machine"],
                                  "dir": f"/home/swarm/adt4_output/{opt['--trial']}/{opt['--machine']}", "pid": 4242,
                                  "topics": 19, "bytes": 1000, "growing": True, "stack_running": True,
                                  "other_recorders": [], "free_bytes": self.free, "total_bytes": 900 * GB, "t": self.t}), ""  # fmt: skip
        if verb == "check-topics":
            return 0, json.dumps({"ok": True, "topics": 19, "published": 19 - len(self.missing_topics),
                                  "missing": self.missing_topics}), ""  # fmt: skip
        if verb == "stop":
            if rec is None:
                return (
                    0,
                    json.dumps({"ok": True, "stopped": False, "was_recording": False}),
                    "",
                )
            if ip in self.stubborn and "--force" not in args:
                return (
                    4,
                    json.dumps(
                        {
                            "ok": False,
                            "recording": True,
                            "error": "the recorder (pid 4242) is still running after SIGINT",
                        }
                    ),
                    "",
                )
            del self.rec[ip]
            return 0, json.dumps({"ok": True, "stopped": True, "was_recording": True, "metadata": True,
                                  "size_bytes": 6 * GB, "duration_s": 120.0, "topics": 19,
                                  "silent_topics": self.silent, "stack_panes": 12, "free_bytes": self.free}), ""  # fmt: skip
        return 1, "", f"unexpected {args}"

    def run_parallel(self, fn, items, max_workers=8):
        return [(i, fn(i)) for i in items]

    def get_local_ips(self):
        return ["127.0.0.1"]

    def verbs(self, ip=None):
        return [a[0] for i, a in self.calls if ip in (None, i)]


@pytest.fixture
def rig(settings):
    client, svc = make_client(settings)
    fake = FakeRec()
    svc.helpers = fake
    t = svc.trials
    t.background = False
    t.sleep = lambda s: None
    now = [1_794_800_000.0]
    t.clock = lambda: now[0]
    ips = {n: svc.fleet.machine(n)["ip"] for n in ROBOTS}
    with client:
        yield type(
            "Rig",
            (),
            {"c": client, "svc": svc, "t": t, "fake": fake, "now": now, "ip": ips},
        )


def start(rig, description="Door search, two spots", machines=ROBOTS):
    return rig.c.post(
        "/api/trials/start", json={"description": description, "machines": machines}
    )


def events(rig, trial_id):
    path = (
        pathlib.Path(rig.svc.settings.output_root).expanduser()
        / trial_id
        / "console"
        / "events.jsonl"
    )
    return [json.loads(line) for line in path.read_text().splitlines()]


# --------------------------------------------------------------------- pure
def test_trial_id_and_slug():
    tid = tr.make_trial_id("Door search: two Spots, run #3!", 1_794_800_000.0)
    assert tid.startswith("trial_2026") and tid.endswith(
        "Z_door-search-two-spots-run-3"
    )
    assert tr.TRIAL_RE.match(tid)
    assert tr.slug("  ") == "trial"
    assert len(tr.slug("word " * 40)) <= 40


def test_recorder_command_quotes_but_keeps_tilde():
    cmd = tr.recorder_command(
        "~/dcist_ws/bin/adt4-rec",
        "~/adt4 output",
        "start",
        "--description",
        "a b; rm -rf /",
    )
    assert cmd.startswith(
        "python3 ~/dcist_ws/bin/adt4-rec --output-root ~/'adt4 output' start"
    )
    assert shlex.split(cmd)[-1] == "a b; rm -rf /"


def test_parse_reply_kinds():
    assert (
        tr.parse_reply(0, 'motd\n{"ok": true, "recording": false}')["recording"]
        is False
    )
    assert tr.parse_reply(255, "", "ssh: connect to host x: Connection timed out")[
        "unreachable"
    ]
    assert tr.parse_reply(-1, "", "SSH timeout")["unreachable"]
    assert tr.parse_reply(
        2, "", "python3: can't open file 'x': [Errno 2] No such file or directory"
    )["missing"]
    r = tr.parse_reply(1, "Traceback ...", "")
    assert not r["ok"] and "unreachable" not in r and "missing" not in r
    # a failing helper's own JSON wins over the exit status text
    assert (
        tr.parse_reply(3, '{"ok": false, "error": "already recording"}')["error"]
        == "already recording"
    )


def test_summarize_body_drops_bulk_and_secrets():
    body = json.dumps(
        {"message": "go to the door", "history": ["x"] * 999, "token": "s3cret"}
    ).encode()
    assert tr.summarize_body(body) == {"message": "go to the door"}
    assert tr.summarize_body(b"\x00\x01binary") == {"_bytes": 8}
    big = tr.summarize_body(json.dumps({"k": "v" * 9000}).encode())
    assert big["_chars"] > 9000 and len(big["_truncated"]) == 4000
    assert tr.summarize_body(b"") is None


def test_estimate():
    assert tr.estimate(60 * GB, 100e6) == pytest.approx(600)
    assert tr.estimate(60 * GB, 0) is None and tr.estimate(None, 5) is None


# ---------------------------------------------------------------- service
def test_start_records_on_every_machine_and_logs(rig):
    r = start(rig)
    assert r.status_code == 200, r.text
    a = r.json()["active"]
    assert a["state"] == "recording" and a["description"] == "Door search, two spots"
    assert sorted(a["machines"]) == ROBOTS
    assert all(m["state"] == "recording" for m in a["machines"].values())
    # every machine got the same trial id, its own name and its platform's topic list
    for name in ROBOTS:
        verbs = rig.fake.verbs(rig.ip[name])
        assert verbs == ["start", "check-topics"]
        args = next(a_ for ip, a_ in rig.fake.calls if ip == rig.ip[name])
        assert args[args.index("--trial") + 1] == a["trial_id"]
        assert args[args.index("--machine") + 1] == name
        assert args[args.index("--topic-set") + 1] == (
            "phoenix" if name == "gauss" else "spot"
        )
    ev = events(rig, a["trial_id"])
    assert [e["type"] for e in ev][:1] == ["trial_start"]
    assert {"trial_started", "machine_state"} <= {e["type"] for e in ev}
    manifest = json.loads((pathlib.Path(a["console_dir"]) / "trial.json").read_text())
    assert manifest["trial_id"] == a["trial_id"] and manifest["stopped"] is None
    # the hub carries the state for new clients
    assert rig.svc.hub.latest("trial/state")["active"]["trial_id"] == a["trial_id"]


def test_start_needs_a_description_and_refuses_a_second_trial(rig):
    assert start(rig, description="  ").status_code == 400
    assert rig.fake.calls == []
    assert start(rig).status_code == 200
    r = start(rig, description="another")
    assert r.status_code == 409 and "still recording" in r.json()["detail"]


def test_partial_start_is_visible_not_hidden(rig):
    rig.fake.unreachable.add(rig.ip["gauss"])
    rig.fake.fail_start[rig.ip["hamilton"]] = "topic list spot_topics.txt: no topics"
    a = start(rig).json()["active"]
    assert a["state"] == "recording"  # euclid records; the others say why not
    assert a["machines"]["euclid"]["state"] == "recording"
    assert (
        a["machines"]["gauss"]["state"] == "failed"
        and "no answer" in a["machines"]["gauss"]["message"]
    )
    assert "no topics" in a["machines"]["hamilton"]["message"]
    started = next(
        e for e in events(rig, a["trial_id"]) if e["type"] == "trial_started"
    )
    assert started["recording"] == 1 and started["not_recording"] == [
        "gauss",
        "hamilton",
    ]
    # retry one machine once it is back
    rig.fake.unreachable.clear()
    r = rig.c.post("/api/trials/machines/gauss/start")
    assert (
        r.status_code == 200
        and r.json()["active"]["machines"]["gauss"]["state"] == "recording"
    )
    assert rig.c.post("/api/trials/machines/euclid/start").status_code == 409


def test_nothing_started_ends_the_trial_as_failed(rig):
    for ip in rig.ip.values():
        rig.fake.missing.add(ip)
    body = start(rig).json()
    assert body["active"] is None and body["last"]["state"] == "failed"
    assert "not on this machine" in body["last"]["machines"]["euclid"]["message"]
    assert rig.c.post("/api/trials/flag", json={"text": "x"}).status_code == 409
    rig.fake.missing.clear()
    rig.now[0] += 1  # a new second: a new trial id
    assert start(rig).json()["active"]["state"] == "recording"  # a new start is allowed


def test_poll_measures_rate_and_disk_estimate(rig):
    start(rig)
    rig.now[0] += 10
    rig.t.poll_once()
    rig.now[0] += 10
    rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]
    assert m["state"] == "recording" and m["rate_bps"] > 0
    # the fake's machine clock advances 0.5 s per call, 50 MB per status
    assert m["minutes_left"] == pytest.approx(200 * GB / m["rate_bps"] / 60, rel=0.01)
    assert m["clock_offset_s"] is not None and m["last_seen"] == rig.now[0]
    assert m["warnings"] == []


def test_low_disk_warns_once_and_never_stops(rig):
    start(rig)
    rig.fake.free = 4 * GB
    for _ in range(3):
        rig.now[0] += 10
        rig.t.poll_once()
    a = rig.c.get("/api/trials").json()["active"]
    assert a["state"] == "recording"
    assert all(m["state"] == "recording" for m in a["machines"].values())
    assert a["machines"]["euclid"]["warnings"][0].startswith("disk: 4.0 GB free")
    assert "stop" not in rig.fake.verbs()
    warn = [
        e
        for e in events(rig, a["trial_id"])
        if e["type"] == "warning" and e["machine"] == "euclid"
    ]
    assert len(warn) == 1


def test_unreachable_is_unknown_then_recovers(rig):
    start(rig)
    rig.now[0] += 10
    rig.t.poll_once()
    seen = rig.now[0]
    rig.fake.unreachable.add(rig.ip["hamilton"])
    rig.now[0] += 10
    rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["hamilton"]
    assert m["state"] == "unreachable" and m["last_seen"] == seen
    rig.fake.unreachable.clear()
    rig.now[0] += 10
    rig.t.poll_once()
    a = rig.c.get("/api/trials").json()["active"]
    assert a["machines"]["hamilton"]["state"] == "recording"
    states = [
        (e["machine"], e["state"])
        for e in events(rig, a["trial_id"])
        if e["type"] == "machine_state"
    ]
    assert ("hamilton", "unreachable") in states and states[-1] == (
        "hamilton",
        "recording",
    )


def test_recorder_that_died_shows_as_lost(rig):
    start(rig)
    del rig.fake.rec[rig.ip["euclid"]]  # stopped on the robot, or crashed
    rig.now[0] += 10
    rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]
    assert m["state"] == "lost" and "NOT recording" in m["message"]


def test_flags_are_timestamped_and_only_during_a_trial(rig):
    assert rig.c.post("/api/trials/flag", json={"text": "early"}).status_code == 409
    a = start(rig).json()["active"]
    rig.now[0] += 75.5
    r = rig.c.post(
        "/api/trials/flag", json={"text": "  hamilton hesitated at the door  "}
    )
    assert r.status_code == 200
    ev = r.json()["event"]
    assert ev["text"] == "hamilton hesitated at the door" and ev["elapsed_s"] == 75.5
    assert ev["t"] == rig.now[0] and ev["iso"].endswith("Z")
    assert rig.c.post("/api/trials/flag", json={"text": " "}).status_code == 400
    flags = [e for e in events(rig, a["trial_id"]) if e["type"] == "flag"]
    assert len(flags) == 1
    assert rig.c.get("/api/trials").json()["active"]["flags"] == 1
    tail = rig.c.get("/api/trials/events?n=1").json()["events"]
    assert tail[-1]["type"] == "flag"
    # a flag costs no ssh
    assert "flag" not in " ".join(rig.fake.verbs())


def test_stop_closes_every_bag_and_reports_silent_topics(rig):
    a = start(rig).json()["active"]
    rig.now[0] += 120
    body = rig.c.post("/api/trials/stop").json()
    assert body["active"] is None
    last = body["last"]
    assert last["state"] == "stopped" and last["stopped"] == rig.now[0]
    for name in ROBOTS:
        m = last["machines"][name]
        assert m["state"] == "stopped" and m["stop"]["metadata"] is True
        assert m["warnings"] == ["1 of 19 topics recorded no message"]
    ev = events(rig, a["trial_id"])
    assert ev[-1]["type"] == "trial_end" and ev[-1]["duration_s"] == 120
    manifest = json.loads((pathlib.Path(a["console_dir"]) / "trial.json").read_text())
    assert (
        manifest["stopped_at"]
        and manifest["machines"]["gauss"]["stop"]["size_bytes"] == 6 * GB
    )
    # the measured rate feeds the next preflight's estimate
    pre = {m["name"]: m for m in rig.c.get("/api/trials/preflight").json()["machines"]}
    assert pre["euclid"]["rate_bps"] == pytest.approx(6 * GB / 120)
    assert pre["euclid"]["minutes"] == pytest.approx(
        200 * GB / (6 * GB / 120) / 60, abs=0.1
    )
    assert pre["euclid"]["ready"] is True
    assert rig.c.post("/api/trials/stop").status_code == 409


def test_stop_with_a_robot_out_of_range_keeps_trying(rig):
    start(rig)
    rig.fake.unreachable.add(rig.ip["gauss"])
    body = rig.c.post("/api/trials/stop").json()
    a = body["active"]
    assert a is not None and a["state"] == "stopping"
    assert a["machines"]["gauss"]["state"] == "stop_pending"
    assert a["machines"]["euclid"]["state"] == "stopped"
    assert rig.fake.rec.get(rig.ip["gauss"])  # it is still recording out there
    rig.t.poll_once()  # still out of range
    assert (
        rig.c.get("/api/trials").json()["active"]["machines"]["gauss"]["state"]
        == "stop_pending"
    )
    rig.fake.unreachable.clear()
    rig.t.poll_once()  # back: the stop goes through
    body = rig.c.get("/api/trials").json()
    assert (
        body["active"] is None
        and body["last"]["machines"]["gauss"]["state"] == "stopped"
    )


def test_stubborn_recorder_needs_force_or_close(rig):
    start(rig)
    rig.fake.stubborn.add(rig.ip["euclid"])
    a = rig.c.post("/api/trials/stop").json()["active"]
    assert a["machines"]["euclid"]["state"] == "stop_failed"
    assert (
        rig.c.post("/api/trials/start", json={"description": "next"}).status_code == 409
    )
    body = rig.c.post("/api/trials/stop", json={"force": True}).json()
    assert (
        body["active"] is None
        and body["last"]["machines"]["euclid"]["state"] == "stopped"
    )


def test_close_ends_a_trial_and_names_what_was_left(rig):
    start(rig)
    assert rig.c.post("/api/trials/close").status_code == 409  # stop first
    rig.fake.unreachable.add(rig.ip["gauss"])
    rig.c.post("/api/trials/stop")
    body = rig.c.post("/api/trials/close").json()
    assert body["active"] is None
    assert body["last"]["note"] == "closed with gauss not confirmed stopped"
    assert body["last"]["machines"]["gauss"]["state"] == "stop_pending"


def test_restore_picks_up_an_unfinished_trial(rig):
    a = start(rig).json()["active"]
    again = tr.TrialService(
        rig.svc.settings, rig.svc.fleet, lambda: rig.fake, None, background=False
    )
    again.restore()
    assert again.active["trial_id"] == a["trial_id"]
    again.poll_once()
    assert again.active["machines"]["euclid"]["state"] == "recording"
    assert any(e["type"] == "console_restarted" for e in events(rig, a["trial_id"]))


def test_preflight_reports_missing_helper_and_running_recorder(rig):
    rig.fake.missing.add(rig.ip["gauss"])
    rig.fake.rec[rig.ip["hamilton"]] = {"trial": "trial_old", "bytes": 5}
    pre = {m["name"]: m for m in rig.c.get("/api/trials/preflight").json()["machines"]}
    assert (
        pre["gauss"]["ready"] is False
        and "not on this machine" in pre["gauss"]["error"]
    )
    assert (
        pre["hamilton"]["ready"] is False
        and "already recording trial_old" in pre["hamilton"]["notes"][0]
    )
    assert (
        pre["euclid"]["ready"] is True and pre["euclid"]["minutes"] is None
    )  # no rate known yet


# ------------------------------------------------------------ request log
def test_operator_requests_are_logged_only_while_recording(rig):
    rig.c.post("/api/fleet/status/refresh")  # before: not logged anywhere
    a = start(rig).json()["active"]
    r = rig.c.post("/api/fleet/status/refresh")
    rig.c.get("/api/fleet")  # reads are not logged
    rig.c.post(
        "/api/robots/euclid/teleop/cmd_vel", json={"vx": 0.1}
    )  # too many: skipped
    rig.c.post("/api/trials/flag", json={"text": "f"})  # logged as a flag, not twice
    reqs = [e for e in events(rig, a["trial_id"]) if e["type"] == "request"]
    assert [(e["method"], e["path"], e["status"]) for e in reqs] == [
        ("POST", "/api/fleet/status/refresh", r.status_code)]  # fmt: skip


def test_request_log_failure_never_breaks_the_request(rig, monkeypatch):
    start(rig)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(rig.t, "log_request", boom)
    assert rig.c.post("/api/fleet/status/refresh").status_code < 500
