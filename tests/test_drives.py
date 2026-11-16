"""USB bag drives: status parsing, eject, bag listing and the copy job."""

import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from adt4_console import drives as bd
from adt4_console.routes import drives as routes

from .conftest import make_client

GB = 2**30

STATUS = {
    "host": "hamilton",
    "drives": [
        {
            "name": "dcist_ssd3",
            "label": "dcist_ssd3",
            "uuid": "7DFF-FFAA",
            "fstype": "exfat",
            "mountpoint": "/mnt/bag_drives/dcist_ssd3",
            "write_dir": "/mnt/bag_drives/dcist_ssd3",
            "mounted": True,
            "managed": True,
            "size_bytes": 1800 * GB,
            "free_bytes": 1600 * GB,
            "usb_speed_mbps": 10000,
            "error": "",
        }
    ],
}

SCAN = {
    "root": "/home/swarm/adt4_output",
    "runs": [
        {
            "name": "nov11_b45",
            "mtime": 1794440000.0,
            "bytes": 30 * GB,
            "bags": [
                {
                    "name": "recorded_data",
                    "bytes": 30 * GB,
                    "files": 2,
                    "metadata": True,
                }
            ],
        },
        {
            "name": "nov10_courtyard",
            "mtime": 1794350000.0,
            "bytes": 12 * GB,
            "bags": [
                {
                    "name": "recorded_data",
                    "bytes": 12 * GB,
                    "files": 2,
                    "metadata": True,
                }
            ],
        },
    ],
}


class FakeHelpers:
    """ssh_cmd answers by command; every call is recorded."""

    def __init__(self, status=STATUS, scan=SCAN):
        self.status = status
        self.scan = scan
        self.calls = []
        self.eject_rc = 0

    def ssh_cmd(self, user, ip, cmd, timeout=5):
        self.calls.append(cmd)
        if cmd == bd.STATUS_CMD:
            if self.status is None:
                return 127, "", "zsh:1: command not found: bag-drive"
            return 0, json.dumps(self.status), ""
        if " eject " in cmd:
            if self.eject_rc:
                return self.eject_rc, "", "cannot unmount: target is busy"
            return (
                0,
                "ejected dcist_ssd3: unmounted and powered down, safe to unplug",
                "",
            )
        if cmd.startswith("python3 -c"):
            return 0, json.dumps(self.scan), ""
        if cmd.startswith("rm -rf"):
            return 0, "", ""
        return 1, "", f"unexpected command {cmd}"

    def run_parallel(self, fn, items, max_workers=8):
        return [(i, fn(i)) for i in items]

    def get_local_ips(self):
        return ["127.0.0.1"]


@pytest.fixture
def app(settings, monkeypatch):
    client, svc = make_client(settings)
    svc.helpers = FakeHelpers()
    routes._copies.clear()
    streamed = []
    svc.verify_lines = None  # None: the hashes match

    def stream(job, m, cmd, on_line):
        streamed.append(cmd)
        if "--info=progress2" in cmd:
            on_line("  1,073,741,824   3%  500.00MB/s    0:00:58")
            on_line("sending incremental file list")
            return 0
        if svc.verify_lines is not None:
            for line in svc.verify_lines:
                on_line(line)
            return 3
        on_line("P 1073741824 2147483648 850")
        on_line("VERIFIED 2 2147483648 2.5")
        return 0

    monkeypatch.setattr(bd, "stream_cmd", stream)
    svc.streamed = streamed
    with client:
        yield client, svc


def _robot(svc):
    return next(
        n for n, m in svc.fleet.runtime_config.items() if m.get("role") == "robot"
    )


def _is_copy(cmd):
    return "--info=progress2" in cmd


# ------------------------------------------------------------------ parsing
def test_parse_status_marks_a_usb2_link_slow():
    slow = json.loads(json.dumps(STATUS))
    slow["drives"][0]["usb_speed_mbps"] = 480
    st = bd.parse_status(0, "motd banner\n" + json.dumps(slow))
    assert st["installed"] and not st["error"]
    assert st["drives"][0]["slow_link"] is True
    fast = bd.parse_status(0, json.dumps(STATUS))
    assert fast["drives"][0]["slow_link"] is False
    # an older helper reports no speed: unknown is not "slow"
    old = json.loads(json.dumps(STATUS))
    del old["drives"][0]["usb_speed_mbps"]
    assert bd.parse_status(0, json.dumps(old))["drives"][0]["slow_link"] is False


def test_parse_status_without_the_helper_or_with_garbage():
    missing = bd.parse_status(127, "", "bash: bag-drive: command not found")
    assert missing["installed"] is False and missing["drives"] == []
    broken = bd.parse_status(1, "", "Traceback ...")
    assert broken["installed"] is True and "rc 1" in broken["error"]
    assert "bad status" in bd.parse_status(0, "{not json")["error"]


def test_names_are_validated_before_they_reach_a_shell():
    assert bd.valid_drive("dcist_ssd3") and bd.valid_drive("7DFF-FFAA")
    assert bd.valid_run("nov11_b45") and bd.valid_run("2026-11-11+test")
    for bad in ("", "a b", "x;rm -rf ~", "$(id)", "../etc", "..", "a/b", "-rf"):
        assert not bd.valid_drive(bad), bad
        assert not bd.valid_run(bad), bad
    with pytest.raises(ValueError):
        bd.eject_command("x; reboot")


def test_progress_lines():
    assert bd.parse_progress(
        "  1,073,741,824  42%  512.30MB/s    0:01:02 (xfr#1, to-chk=0/2)"
    ) == (
        1073741824,
        42.0,
        "512.30MB/s",
    )
    assert bd.parse_progress("sending incremental file list") is None


def test_commands_are_valid_shell_and_quote_paths():
    src = "/home/swarm/adt4_output/run 1/recorded_data"
    dst = "/mnt/bag_drives/d/h/run 1/recorded_data"
    for cmd in (
        bd.copy_command(src, dst),
        bd.verify_command(src, dst),
        bd.remove_command(src),
        bd.bag_scan_command("~/adt4_output"),
        bd.bag_scan_command("~/adt4_output", "/mnt/bag_drives/d/h"),
        bd.eject_command("dcist_ssd3"),
    ):
        assert subprocess.run(["bash", "-n", "-c", cmd]).returncode == 0, cmd
    copy = bd.copy_command(src, dst)
    assert "'/home/swarm/adt4_output/run 1/recorded_data/'" in copy
    assert copy.endswith("sync -f '/mnt/bag_drives/d/h/run 1/recorded_data/'")
    # an interrupted copy continues instead of rewriting the file
    assert " --append " in copy
    assert bd.manifest_path(dst) == dst + ".sha256"
    assert bd.verify_command(src, dst).endswith(f"'{src}' '{dst}' '{dst}.sha256'")


def test_verify_output_lines():
    assert bd.parse_verify("P 1073741824 2147483648 850") == (
        "progress",
        (1073741824, 2147483648, 850),
    )
    assert bd.parse_verify("VERIFIED 2 2147483648 2.5") == (
        "done",
        (2, 2147483648, 2.5),
    )
    assert (
        bd.parse_verify("DIFF a.mcap: sha256 1 on the robot, 2 on the drive")[0]
        == "diff"
    )
    assert bd.parse_verify("FAILED 1")[0] == "other"


def _verify(src, dst):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            bd.VERIFY,
            str(src),
            str(dst),
            bd.manifest_path(str(dst)),
        ],
        capture_output=True,
        text=True,
    )


def test_verify_hashes_both_copies_and_writes_a_manifest(tmp_path):
    import hashlib

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    data = os.urandom(300_000)
    for d in (src, dst):
        (d / "recorded_data_0.mcap").write_bytes(data)
        (d / "metadata.yaml").write_text("meta")
    p = _verify(src, dst)
    assert p.returncode == 0, p.stdout + p.stderr
    assert bd.parse_verify(p.stdout.strip().splitlines()[-1])[1][:2] == (2, 300_004)
    manifest = pathlib.Path(bd.manifest_path(str(dst))).read_text().splitlines()
    assert f"{hashlib.sha256(data).hexdigest()}  recorded_data_0.mcap" in manifest

    # one flipped byte, same size: only a hash can tell
    bad = bytearray(data)
    bad[150_000] ^= 1
    (dst / "recorded_data_0.mcap").write_bytes(bytes(bad))
    pathlib.Path(bd.manifest_path(str(dst))).unlink()
    p = _verify(src, dst)
    assert p.returncode == 3
    assert "DIFF recorded_data_0.mcap: sha256" in p.stdout
    assert not pathlib.Path(bd.manifest_path(str(dst))).exists()

    # a short copy (interrupted) and a missing file
    (dst / "recorded_data_0.mcap").write_bytes(data[:1000])
    (dst / "metadata.yaml").unlink()
    p = _verify(src, dst)
    assert p.returncode == 3 and "size 1000 on the drive" in p.stdout
    assert "FAILED 2" in p.stdout


def test_bag_scan_lists_a_trials_console_folder(tmp_path):
    out = tmp_path / "out"
    con = out / "trial_x" / "console"
    con.mkdir(parents=True)
    (con / "trial.json").write_text("{}")
    (con / "events.jsonl").write_text("{}\n")
    (con / "screen_a.webm").write_bytes(b"v" * 100)
    # a folder with a trial.json but another name is not one (e.g. a robot's
    # bag folder before its first mcap), and neither is a console folder without it
    (out / "trial_x" / "notes").mkdir()
    (out / "trial_x" / "notes" / "trial.json").write_text("{}")
    (out / "run_b" / "console").mkdir(parents=True)
    (out / "run_b" / "console" / "x.log").write_text("x")
    p = subprocess.run(
        [sys.executable, "-c", bd.BAG_SCAN, str(out)], capture_output=True, text=True
    )
    scan = bd.parse_bag_scan(p.returncode, p.stdout, p.stderr)
    assert [(r["name"], [b["name"] for b in r["bags"]]) for r in scan["runs"]] == [
        ("trial_x", ["console"])
    ]
    assert scan["runs"][0]["bags"][0]["files"] == 3


def test_bag_scan_finds_bags_in_runs(tmp_path):
    out = tmp_path / "out"
    bag = out / "run_a" / "recorded_data"
    bag.mkdir(parents=True)
    (bag / "recorded_data_0.mcap").write_bytes(b"x" * 1000)
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")
    (out / "run_a" / "hydra").mkdir()
    (out / "run_a" / "hydra" / "dsg.json").write_text("{}")
    (out / "run_no_bag" / "hydra").mkdir(parents=True)
    (out / "stray.log").write_text("x")
    p = subprocess.run(
        [sys.executable, "-c", bd.BAG_SCAN, str(out)], capture_output=True, text=True
    )
    scan = bd.parse_bag_scan(p.returncode, "login banner\n" + p.stdout, p.stderr)
    assert [r["name"] for r in scan["runs"]] == ["run_a"]
    run = scan["runs"][0]
    # the bag was written just now: it counts as still recording
    assert run["bags"] == [
        {
            "name": "recorded_data",
            "bytes": 1000 + 31,
            "files": 2,
            "metadata": True,
            "recording": True,
            "dest_bytes": 0,
            "on_drive": "",
        }
    ]
    assert run["recording"] is True
    old = time.time() - 3600
    for f in bag.iterdir():
        os.utime(f, (old, old))
    p = subprocess.run(
        [sys.executable, "-c", bd.BAG_SCAN, str(out)], capture_output=True, text=True
    )
    assert bd.parse_bag_scan(p.returncode, p.stdout)["runs"][0]["recording"] is False
    assert run["bytes"] == 1031 and scan["root"] == str(out)
    assert run["on_drive"] == "" and run["dest_bytes"] == 0

    # with the drive folder: partial, then copied, then verified
    def scan_with(dest):
        q = subprocess.run(
            [sys.executable, "-c", bd.BAG_SCAN, str(out), str(dest)],
            capture_output=True,
            text=True,
        )
        return bd.parse_bag_scan(q.returncode, q.stdout)["runs"][0]

    drive = tmp_path / "drive" / "hamilton"
    copy = drive / "run_a" / "recorded_data"
    copy.mkdir(parents=True)
    (copy / "recorded_data_0.mcap").write_bytes(b"x" * 400)
    part = scan_with(drive)
    assert part["on_drive"] == "partial" and part["dest_bytes"] == 400
    (copy / "recorded_data_0.mcap").write_bytes(b"x" * 1000)
    (copy / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")
    assert scan_with(drive)["on_drive"] == "copied"
    (drive / "run_a" / "recorded_data.sha256").write_text("abc  recorded_data_0.mcap\n")
    done = scan_with(drive)
    assert done["on_drive"] == "verified" and done["dest_bytes"] == 1031

    # no output root yet: an empty listing with a note, not an error
    p = subprocess.run(
        [sys.executable, "-c", bd.BAG_SCAN, str(tmp_path / "nope")],
        capture_output=True,
        text=True,
    )
    empty = bd.parse_bag_scan(p.returncode, p.stdout, p.stderr)
    assert empty["runs"] == [] and "note" in empty
    with pytest.raises(RuntimeError):
        bd.parse_bag_scan(255, "", "ssh: connect to host: timed out")


# ------------------------------------------------------------------- routes
def test_list_drives_per_machine(app):
    client, svc = app
    data = client.get("/api/drives").json()
    names = {m["name"] for m in data["machines"]}
    assert set(svc.fleet.runtime_config) == names
    for m in data["machines"]:
        assert m["installed"] is True and m["online"] is True
        assert m["drives"][0]["name"] == "dcist_ssd3"
        assert m["drives"][0]["copy_job"] is None


def test_list_drives_without_the_helper(app):
    client, svc = app
    svc.helpers.status = None
    m = client.get("/api/drives").json()["machines"][0]
    assert m["installed"] is False and m["drives"] == []
    assert "not installed" in m["error"]


def test_eject(app):
    client, svc = app
    robot = _robot(svc)
    r = client.post("/api/drives/eject", json={"machine": robot, "drive": "dcist_ssd3"})
    assert r.status_code == 200 and "safe to unplug" in r.json()["message"]
    assert svc.helpers.calls[-1] == "sudo -n /usr/local/sbin/bag-drive eject dcist_ssd3"
    assert (
        client.post(
            "/api/drives/eject", json={"machine": robot, "drive": "x;id"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/drives/eject", json={"machine": "nope", "drive": "d"}
        ).status_code
        == 404
    )
    svc.helpers.eject_rc = 1
    busy = client.post(
        "/api/drives/eject", json={"machine": robot, "drive": "dcist_ssd3"}
    )
    assert busy.status_code == 502 and "target is busy" in busy.json()["detail"]


def test_list_bags(app):
    client, svc = app
    robot = _robot(svc)
    data = client.get("/api/drives/bags", params={"machine": robot}).json()
    assert [r["name"] for r in data["runs"]] == ["nov11_b45", "nov10_courtyard"]
    assert data["root"] == "/home/swarm/adt4_output"
    # without a drive nothing is asked about the drive
    assert "/mnt/bag_drives" not in svc.helpers.calls[-1]
    client.get("/api/drives/bags", params={"machine": robot, "drive": "dcist_ssd3"})
    assert svc.helpers.calls[-1].endswith(f"/mnt/bag_drives/dcist_ssd3/{robot}")
    assert (
        client.get(
            "/api/drives/bags", params={"machine": robot, "drive": "x;id"}
        ).status_code
        == 400
    )


BODY = {"drive": "dcist_ssd3", "runs": ["nov11_b45", "nov10_courtyard"]}


def test_copy_runs_rsync_on_the_robot_then_hashes_both_copies(app):
    client, svc = app
    robot = _robot(svc)
    r = client.post("/api/drives/copy", json={"machine": robot, **BODY})
    assert r.status_code == 200 and r.json()["bytes"] == 42 * GB
    job = svc.jobs.wait(r.json()["job_id"])
    assert job.state == "succeeded", job.to_dict()["log"]
    # per bag: the copy, then the hash check
    assert [_is_copy(c) for c in svc.streamed] == [True, False, True, False]
    dst = f"/mnt/bag_drives/dcist_ssd3/{robot}/nov11_b45/recorded_data"
    assert f"/home/swarm/adt4_output/nov11_b45/recorded_data/ {dst}/" in svc.streamed[0]
    assert svc.streamed[1].endswith(f"{dst} {dst}.sha256")
    assert [c["run"] for c in job.result["copied"]] == ["nov11_b45", "nov10_courtyard"]
    assert job.result["removed"] == []
    assert job.progress == {f"{robot} copy": 100.0, f"{robot} verify": 100.0}
    # nothing is deleted unless asked
    assert not any(c.startswith("rm -rf") for c in svc.helpers.calls)


def test_copy_refuses_what_cannot_work(app):
    client, svc = app
    robot = _robot(svc)
    post = lambda **kw: client.post(  # noqa: E731
        "/api/drives/copy",
        json={"machine": robot, "drive": "dcist_ssd3", "runs": ["nov11_b45"], **kw},
    )
    assert post(runs=[]).status_code == 400
    assert post(runs=["../x"]).status_code == 400
    assert post(runs=["not_a_run"]).status_code == 404
    assert post(drive="other").status_code == 404
    svc.helpers.scan = json.loads(json.dumps(SCAN))
    svc.helpers.scan["runs"][0]["recording"] = True
    live = post()
    assert live.status_code == 409 and "still being recorded" in live.json()["detail"]
    svc.helpers.scan = SCAN
    svc.helpers.status = json.loads(json.dumps(STATUS))
    svc.helpers.status["drives"][0]["free_bytes"] = 10 * GB
    full = post()
    assert full.status_code == 409 and "not enough space" in full.json()["detail"]
    svc.helpers.status["drives"][0].update(free_bytes=1600 * GB, write_dir="")
    assert post().status_code == 409
    assert svc.streamed == []


def test_copy_fails_on_a_hash_mismatch_and_drops_the_bad_file(app):
    client, svc = app
    robot = _robot(svc)
    svc.verify_lines = [
        "P 100 200 800",
        "DIFF recorded_data_0.mcap: sha256 aa on the robot, bb on the drive",
        "FAILED 1",
    ]
    r = client.post(
        "/api/drives/copy",
        json={
            "machine": robot,
            "drive": "dcist_ssd3",
            "runs": ["nov11_b45"],
            "remove_source": True,
        },
    )
    job = svc.jobs.wait(r.json()["job_id"])
    assert job.state == "failed"
    removed = [c for c in svc.helpers.calls if c.startswith("rm -rf")]
    # the bad copy on the drive goes (the next copy rewrites it); the robot's bag stays
    assert removed == [
        f"rm -rf -- /mnt/bag_drives/dcist_ssd3/{robot}/nov11_b45/recorded_data/recorded_data_0.mcap"
    ]


def test_a_short_copy_is_kept_for_the_next_resume(app):
    client, svc = app
    svc.verify_lines = [
        "DIFF recorded_data_0.mcap: size 10 on the drive, 20 on the robot",
        "FAILED 1",
    ]
    r = client.post(
        "/api/drives/copy",
        json={"machine": _robot(svc), "drive": "dcist_ssd3", "runs": ["nov11_b45"]},
    )
    assert svc.jobs.wait(r.json()["job_id"]).state == "failed"
    assert not any(c.startswith("rm -rf") for c in svc.helpers.calls)


def test_resume_skips_what_is_verified_and_counts_what_is_there(app):
    client, svc = app
    robot = _robot(svc)
    scan = json.loads(json.dumps(SCAN))
    scan["runs"][0]["bags"][0].update(on_drive="verified", dest_bytes=30 * GB)
    scan["runs"][1]["bags"][0].update(on_drive="partial", dest_bytes=8 * GB)
    svc.helpers.scan = scan
    # 42 GB selected, 38 GB already there: 4 GB is enough room
    svc.helpers.status = json.loads(json.dumps(STATUS))
    svc.helpers.status["drives"][0]["free_bytes"] = 5 * GB
    r = client.post("/api/drives/copy", json={"machine": robot, **BODY})
    assert r.status_code == 200, r.json()
    assert r.json() == {
        "job_id": r.json()["job_id"],
        "bytes": 42 * GB,
        "to_copy": 4 * GB,
    }
    job = svc.jobs.wait(r.json()["job_id"])
    assert job.state == "succeeded", job.to_dict()["log"]
    # only the unfinished bag is copied and hashed
    assert len(svc.streamed) == 2 and "nov10_courtyard" in svc.streamed[0]
    assert [i["run"] for i in job.result["skipped"]] == ["nov11_b45"]
    assert [i["run"] for i in job.result["copied"]] == ["nov10_courtyard"]


def test_interrupting_a_copy_keeps_the_drive_usable(app, monkeypatch):
    client, svc = app
    robot = _robot(svc)
    started = threading.Event()

    def blocking(job, m, cmd, on_line):
        started.set()
        while True:  # what stream_cmd does while rsync runs
            job.check_cancelled()
            time.sleep(0.01)

    monkeypatch.setattr(bd, "stream_cmd", blocking)
    job_id = client.post("/api/drives/copy", json={"machine": robot, **BODY}).json()[
        "job_id"
    ]
    assert started.wait(5)
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["state"] == "cancelled"
    job = svc.jobs.wait(job_id)
    assert job.state == "cancelled"
    assert any(
        "start the copy again to continue" in line["text"] for line in job.log_lines
    )
    # the drive is free again: eject works, and so does a new copy
    eject = client.post(
        "/api/drives/eject", json={"machine": robot, "drive": "dcist_ssd3"}
    )
    assert eject.status_code == 200


def test_move_deletes_only_after_the_hashes_match_and_a_typed_yes(app):
    client, svc = app
    robot = _robot(svc)
    body = {
        "machine": robot,
        "drive": "dcist_ssd3",
        "runs": ["nov11_b45"],
        "remove_source": True,
    }

    def run(answer):
        svc.helpers.calls.clear()
        svc.streamed.clear()
        job_id = client.post("/api/drives/copy", json=body).json()["job_id"]
        job = svc.jobs.get(job_id)
        deadline = time.time() + 5
        while job.state != "waiting" and time.time() < deadline:
            time.sleep(0.01)
        assert job.state == "waiting", job.to_dict()["log"]
        # the hash check ran before the question
        assert [_is_copy(c) for c in svc.streamed] == [True, False]
        assert "cannot be undone" in job.confirm_request["message"]
        # one button: nothing to type
        assert job.confirm_request["required_text"] is None
        assert job.confirm_request["yes_label"] == "Delete from robot"
        assert not any(c.startswith("rm -rf") for c in svc.helpers.calls)
        client.post(f"/api/jobs/{job_id}/confirm", json={"answer": answer})
        return svc.jobs.wait(job_id)

    kept = run(False)
    assert kept.state == "succeeded" and kept.result["removed"] == []
    assert not any(c.startswith("rm -rf") for c in svc.helpers.calls)

    moved = run(True)
    assert moved.state == "succeeded" and moved.result["removed"] == [
        "nov11_b45/recorded_data"
    ]
    assert (
        "rm -rf -- /home/swarm/adt4_output/nov11_b45/recorded_data" in svc.helpers.calls
    )


def test_eject_waits_for_a_running_copy(app, monkeypatch):
    client, svc = app
    robot = _robot(svc)
    release = threading.Event()

    def slow(job, m, cmd, on_line):
        release.wait(5)
        if not _is_copy(cmd):
            on_line("VERIFIED 2 100 0.1")
        return 0

    monkeypatch.setattr(bd, "stream_cmd", slow)
    body = {"machine": robot, "drive": "dcist_ssd3", "runs": ["nov11_b45"]}
    job_id = client.post("/api/drives/copy", json=body).json()["job_id"]
    assert client.post("/api/drives/copy", json=body).status_code == 409
    e = client.post("/api/drives/eject", json={"machine": robot, "drive": "dcist_ssd3"})
    assert e.status_code == 409 and job_id in e.json()["detail"]
    machines = client.get("/api/drives").json()["machines"]
    drive = next(m for m in machines if m["name"] == robot)["drives"][0]
    assert drive["copy_job"] == job_id
    release.set()
    assert svc.jobs.wait(job_id).state == "succeeded"
    again = client.post(
        "/api/drives/eject", json={"machine": robot, "drive": "dcist_ssd3"}
    )
    assert again.status_code == 200
