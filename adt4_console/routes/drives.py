"""USB bag drives on the fleet's machines (API.md §22): what is plugged in,
eject, the bags recorded on a machine, and copying them onto a drive."""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import drives as bd
from . import require_machine, services

router = APIRouter(prefix="/api/drives")

# (machine, drive) -> job id of the copy writing to it; an eject waits for it
_copies: dict[tuple[str, str], str] = {}
_copies_lock = threading.Lock()


def _busy(svc, machine: str, drive: str) -> str | None:
    """Id of the copy still writing to ``drive`` on ``machine``, if any."""
    with _copies_lock:
        job_id = _copies.get((machine, drive))
    if job_id is None:
        return None
    job = svc.jobs.get(job_id)
    if job is None or job.finished is not None:
        with _copies_lock:
            _copies.pop((machine, drive), None)
        return None
    return job_id


def machine_status(svc, m: dict) -> dict:
    rc, out, err = bd.run_cmd(svc, m, bd.STATUS_CMD, bd.STATUS_TIMEOUT_S)
    st = bd.parse_status(rc, out, err)
    for d in st["drives"]:
        d["copy_job"] = _busy(svc, m["name"], d["name"])
    return st


def _find_drive(svc, m: dict, name: str) -> dict:
    st = machine_status(svc, m)
    if st["error"]:
        raise HTTPException(502, f"{m['name']}: {st['error']}")
    drive = next((d for d in st["drives"] if d["name"] == name), None)
    if drive is None:
        raise HTTPException(404, f"{m['name']} has no drive named {name}")
    return drive


@router.get("")
def list_drives(request: Request):
    """Every online machine's drives.  A machine without the helper reports
    ``installed: false``; an unreachable one an ``error``."""
    svc = services(request)
    fleet = svc.fleet
    with fleet.lock:
        machines = [dict(m) for m in fleet.runtime_config.values()]
    online = [m for m in machines if m.get("online")]

    def fetch(m):
        try:
            return machine_status(svc, m)
        except Exception as e:  # noqa: BLE001
            return {
                "installed": None,
                "drives": [],
                "error": f"{type(e).__name__}: {e}",
            }

    results = dict(
        (m["name"], r if isinstance(r, dict) else {"installed": None, "drives": [], "error": str(r)})
        for m, r in (svc.helpers.run_parallel(fetch, online) if online else [])
    )  # fmt: skip
    out = []
    for m in sorted(machines, key=lambda x: x["name"]):
        st = results.get(m["name"])
        out.append(
            {
                "name": m["name"],
                "role": m.get("role"),
                "online": bool(m.get("online")),
                "installed": st["installed"] if st else None,
                "drives": st["drives"] if st else [],
                "error": st["error"] if st else "",
            }
        )
    return {"updated": time.time(), "machines": out}


class EjectBody(BaseModel):
    machine: str
    drive: str


@router.post("/eject")
def eject(body: EjectBody, request: Request):
    """Sync, unmount and power down ``drive`` on ``machine``."""
    svc = services(request)
    m = require_machine(svc, body.machine)
    if not bd.valid_drive(body.drive):
        raise HTTPException(400, f"bad drive name {body.drive!r}")
    busy = _busy(svc, m["name"], body.drive)
    if busy:
        raise HTTPException(
            409, f"a copy to {body.drive} is still running ({busy}): cancel it first"
        )
    rc, out, err = bd.run_cmd(svc, m, bd.eject_command(body.drive), bd.EJECT_TIMEOUT_S)
    message = ((out or "").strip() or (err or "").strip())[-400:]
    if rc != 0:
        if "password is required" in (err or ""):
            message = "eject needs the bag-drive sudo rule: rerun its installer on this machine"
        raise HTTPException(502, f"{m['name']}: eject failed: {message or f'rc {rc}'}")
    return {"ok": True, "message": message}


def _scan_bags(svc, m: dict, drive: dict | None) -> dict:
    dest = (
        bd.machine_dir(drive["write_dir"], m["name"])
        if drive and drive.get("write_dir")
        else None
    )
    rc, out, err = bd.run_cmd(
        svc,
        m,
        bd.bag_scan_command(svc.settings.output_root, dest),
        bd.SCAN_TIMEOUT_S,
    )
    try:
        return bd.parse_bag_scan(rc, out, err)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(502, f"{m['name']}: {e}") from e


@router.get("/bags")
def list_bags(machine: str, request: Request, drive: str | None = None):
    """The runs on ``machine`` that hold rosbags, newest first.  With ``drive``,
    each run also says what of it is already on that drive (``on_drive``)."""
    svc = services(request)
    m = require_machine(svc, machine)
    target = None
    if drive:
        if not bd.valid_drive(drive):
            raise HTTPException(400, f"bad drive name {drive!r}")
        target = _find_drive(svc, m, drive)
    return {"machine": m["name"], "drive": drive, **_scan_bags(svc, m, target)}


class CopyBody(BaseModel):
    machine: str
    drive: str
    runs: list[str]
    # delete each bag from the machine once the hashes of its copy match; the
    # job asks before deleting anything
    remove_source: bool = False


@router.post("/copy")
def copy(body: CopyBody, request: Request):
    """Copy the bags of ``runs`` from ``machine``'s disk onto its ``drive``.

    Cancelling the job interrupts the copy and keeps what is on the drive; the
    same request later continues where it stopped."""
    svc = services(request)
    m = require_machine(svc, body.machine)
    if not bd.valid_drive(body.drive):
        raise HTTPException(400, f"bad drive name {body.drive!r}")
    runs = list(dict.fromkeys(body.runs))
    if not runs:
        raise HTTPException(400, "no runs selected")
    bad = [r for r in runs if not bd.valid_run(r)]
    if bad:
        raise HTTPException(400, f"bad run name {bad[0]!r}")
    busy = _busy(svc, m["name"], body.drive)
    if busy:
        raise HTTPException(409, f"a copy to {body.drive} is already running ({busy})")

    drive = _find_drive(svc, m, body.drive)
    if not drive["mounted"]:
        raise HTTPException(409, f"{body.drive} is not mounted")
    if not drive["write_dir"]:
        raise HTTPException(409, f"{body.drive} is not writable by {m.get('user')}")

    scan = _scan_bags(svc, m, drive)
    by_name = {r["name"]: r for r in scan["runs"]}
    missing = [r for r in runs if r not in by_name]
    if missing:
        raise HTTPException(404, f"{m['name']} has no bags in run {missing[0]}")
    live = [r for r in runs if by_name[r].get("recording")]
    if live:
        raise HTTPException(
            409, f"{live[0]} is still being recorded: stop the recording first"
        )
    bags = [(r, b) for r in runs for b in by_name[r]["bags"]]
    # the run output folder each trial points to goes along with it; it is
    # copied after the bags, is never deleted from the machine, and a problem
    # with it is a warning, not a failed copy
    outputs = [
        (r, o) for r in runs if (o := by_name[r].get("output")) and not o.get("missing")
    ]
    total = sum(b["bytes"] for _, b in bags)
    # what is on the drive already (an interrupted or finished copy) needs no space
    needed = sum(max(0, b["bytes"] - b.get("dest_bytes", 0)) for _, b in bags)
    needed += sum(max(0, o["bytes"] - o.get("dest_bytes", 0)) for _, o in outputs)
    if needed > drive["free_bytes"]:
        raise HTTPException(
            409,
            f"not enough space on {body.drive}: {bd.human(needed)} to copy, "
            f"{bd.human(drive['free_bytes'])} free",
        )

    root, write_dir, name = scan["root"], drive["write_dir"], m["name"]
    copy_key, verify_key = f"{name} copy", f"{name} verify"

    def pct(done: float) -> float:
        return 100.0 * done / total if total else 100.0

    def run(job):
        from ..jobs import JobCancelled

        job.log(
            "heading",
            f"{name}: {len(bags)} bag(s), {bd.human(total)} -> {body.drive} ({write_dir})",
        )
        if needed < total:
            job.log(
                "info",
                f"{bd.human(total - needed)} is already on the drive: continuing from there",
            )
        if drive["slow_link"]:
            job.log(
                "warn",
                f"{body.drive} is on a USB 2.0 link ({drive['usb_speed_mbps']} Mbit/s): "
                "this will be slow. Use a USB-C port that links at 5 Gbit/s or more.",
            )
        result = {"copied": [], "skipped": [], "removed": [], "bytes": 0, "outputs": []}
        try:
            return work(job, result)
        except JobCancelled:
            job.log(
                "warn",
                "Copy interrupted. What was written stays on the drive: start the "
                "copy again to continue from there. The drive can be ejected now.",
            )
            raise

    def work(job, result):
        before = 0  # bytes of the bags finished so far
        for run_name, bag in bags:
            label = f"{run_name}/{bag['name']}"
            src = f"{root}/{run_name}/{bag['name']}"
            dst = bd.dest_dir(write_dir, name, run_name, bag["name"])
            size = bag["bytes"]
            item = {"run": run_name, "bag": bag["name"], "dest": dst, "bytes": size}
            if bag.get("on_drive") == "verified":
                job.log("ok", f"{label}: already on the drive, hashes checked earlier")
                result["skipped"].append(item)
                before += size
                job.set_progress(copy_key, pct(before))
                job.set_progress(verify_key, pct(before))
                continue

            job.log("info", f"{label} ({bd.human(size)})")
            tail: list[str] = []

            def on_copy(line, tail=tail, before=before, size=size):
                p = bd.parse_progress(line)
                if p is None:
                    tail.append(line)
                    del tail[:-5]
                    job.log("dim", line[:300])
                    return
                job.set_progress(copy_key, pct(before + min(p[0], size)))

            t0 = time.time()
            rc = bd.stream_cmd(job, m, bd.copy_command(src, dst), on_copy)
            if rc != 0:
                job.log("error", f"copy failed (rc {rc}): {' | '.join(tail)[-300:]}")
                return False
            secs = max(time.time() - t0, 1e-3)
            job.set_progress(copy_key, pct(before + size))
            job.log("dim", f"copied in {secs:.0f}s; hashing both copies")

            diffs: list[str] = []
            verified: list[tuple] = []

            def on_verify(
                line, diffs=diffs, verified=verified, before=before, size=size
            ):
                kind, v = bd.parse_verify(line)
                if kind == "progress":
                    job.set_progress(verify_key, pct(before + min(v[0], size)))
                elif kind == "diff":
                    diffs.append(v[0])
                    job.log("error", f"{label}: {v[0][:300]}")
                elif kind == "done":
                    verified.append(v)
                elif not line.startswith("FAILED"):
                    job.log("dim", line[:300])

            rc = bd.stream_cmd(job, m, bd.verify_command(src, dst), on_verify)
            if rc != 0 or diffs or not verified:
                # a file whose bytes are wrong is removed so the next copy writes
                # it again; a short one is kept (the next copy continues it)
                wrong = [d.split(":", 1)[0] for d in diffs if "sha256" in d]
                for fname in wrong:
                    if bd.valid_file(fname):
                        bd.run_cmd(svc, m, bd.remove_command(f"{dst}/{fname}"), 60)
                job.log(
                    "error",
                    f"{label}: the copy does not match the original"
                    + (
                        f"; removed {len(wrong)} bad file(s) from the drive, copy again"
                        if wrong
                        else f" (rc {rc})"
                    ),
                )
                return False
            _, _, vsecs = verified[0]
            before += size
            job.set_progress(verify_key, pct(before))
            job.log(
                "ok",
                f"{label}: on the drive, SHA-256 of every file matches "
                f"(hashed in {vsecs:.0f}s, {bd.human(size / max(vsecs, 1e-3))}/s)",
            )
            result["copied"].append(item)
            result["bytes"] += size
            job.check_cancelled()

        for run_name in runs:
            o = by_name[run_name].get("output")
            if o and o.get("missing"):
                job.log(
                    "warn",
                    f"{run_name}: run output {o['name']} was not copied ({o.get('note')})",
                )
                result["outputs"].append(
                    {"run": run_name, "name": o["name"], "state": "missing", "note": o.get("note")}
                )  # fmt: skip
        for run_name, o in outputs:
            result["outputs"].append(copy_output(job, run_name, o))
            job.check_cancelled()
        shaky = [o for o in result["outputs"] if o["state"] != "verified"]

        on_drive = result["copied"] + result["skipped"]
        if body.remove_source and on_drive:
            answer = job.confirm(
                f"Delete {len(on_drive)} bag(s) ({bd.human(sum(i['bytes'] for i in on_drive))}) "
                f"from {name}? The hashes of the copies on {body.drive} match. "
                + (
                    "The run output of "
                    + ", ".join(sorted({o["run"] for o in shaky}))
                    + " is NOT fully on the drive (see the log); it stays on "
                    f"{name}, but once the bags are deleted this page no longer "
                    "offers to copy it. "
                    if shaky
                    else ""
                )
                + "This cannot be undone.",
                kind="drive_delete",
                yes_label="Delete from robot",
                no_label="Keep them",
                detail={"bags": [f"{i['run']}/{i['bag']}" for i in on_drive]},
            )
            if answer:
                for item in on_drive:
                    src = f"{root}/{item['run']}/{item['bag']}"
                    rc, out, err = bd.run_cmd(svc, m, bd.remove_command(src), 120)
                    if rc != 0:
                        job.log(
                            "error", f"could not delete {src}: {(err or out)[-200:]}"
                        )
                        return False
                    result["removed"].append(f"{item['run']}/{item['bag']}")
                    job.log("ok", f"deleted {src}")
                for run_name in dict.fromkeys(i["run"] for i in on_drive):
                    bd.run_cmd(svc, m, bd.prune_command(f"{root}/{run_name}"), 30)
            else:
                job.log("info", "Bags left on the robot.")
        job.log("ok", f"Done. Eject {body.drive} before unplugging it.")
        return result

    def copy_output(job, run_name: str, o: dict) -> dict:
        """Copy one run output folder and hash it.  Never raises for a problem
        with the folder: the bags are what the job is judged on."""
        label = f"{run_name}: run output {o['name']}"
        dst = bd.output_dest_dir(write_dir, name, run_name, o["name"])
        item = {"run": run_name, "name": o["name"], "dest": dst, "bytes": o["bytes"],
                "live": bool(o.get("live")), "state": "failed", "changed": []}  # fmt: skip
        job.log("info", f"{label} ({bd.human(o['bytes'])}, {o['files']} files)")
        if o.get("live"):
            job.log(
                "warn",
                f"{label}: the stack is still running or wrote to it in the last "
                "minute, so this copy may be incomplete (the final scene graph is "
                "saved when the stack stops). Copy the run again after stopping it.",
            )
        tail: list[str] = []

        def on_copy(line):
            if bd.parse_progress(line) is None:
                tail.append(line)
                del tail[:-5]

        rc = bd.stream_cmd(job, m, bd.copy_tree_command(o["path"], dst), on_copy)
        # rc 24: files vanished while copying (a folder still being written)
        if rc not in (0, 24):
            job.log(
                "warn", f"{label}: copy failed (rc {rc}): {' | '.join(tail)[-300:]}"
            )
            return item
        verified: list[tuple] = []

        def on_verify(line):
            kind, v = bd.parse_verify(line)
            if kind == "diff":
                item["changed"].append(v[0].split(":", 1)[0])
            elif kind == "done":
                verified.append(v)

        rc = bd.stream_cmd(job, m, bd.verify_command(o["path"], dst, True), on_verify)
        if rc != 0 or not verified:
            job.log("warn", f"{label}: could not check the copy (rc {rc})")
            return item
        if item["changed"]:
            item["state"] = "changed"
            shown = ", ".join(item["changed"][:5])
            more = len(item["changed"]) - 5
            job.log(
                "warn",
                f"{label}: {len(item['changed'])} file(s) changed on {name} while "
                f"being copied ({shown}{f', and {more} more' if more > 0 else ''}). "
                "The rest matches; copy again once the stack is stopped.",
            )
        else:
            item["state"] = "verified"
            job.log("ok", f"{label}: on the drive, SHA-256 of every file matches")
        return item

    job = svc.jobs.start("drive_copy", f"Copy bags {name} -> {body.drive}", run)
    with _copies_lock:
        _copies[(name, body.drive)] = job.id
    return {"job_id": job.id, "bytes": total, "to_copy": needed}


class DeleteBody(BaseModel):
    machine: str
    runs: list[str]
    # a plugged-in drive: the question then says which of the runs are on it
    drive: str | None = None


@router.post("/delete")
def delete(body: DeleteBody, request: Request):
    """Delete the bags of ``runs`` from ``machine``'s disk, drive or no drive.

    Nothing is copied first, so the job asks with a typed ``delete`` and says
    what it knows about copies.  A run's output folder is not touched."""
    svc = services(request)
    m = require_machine(svc, body.machine)
    runs = list(dict.fromkeys(body.runs))
    if not runs:
        raise HTTPException(400, "no runs selected")
    bad = [r for r in runs if not bd.valid_run(r)]
    if bad:
        raise HTTPException(400, f"bad run name {bad[0]!r}")
    target = None
    if body.drive:
        if not bd.valid_drive(body.drive):
            raise HTTPException(400, f"bad drive name {body.drive!r}")
        if _busy(svc, m["name"], body.drive):
            raise HTTPException(409, f"a copy to {body.drive} is running: wait for it")
        target = _find_drive(svc, m, body.drive)
    scan = _scan_bags(svc, m, target)
    by_name = {r["name"]: r for r in scan["runs"]}
    missing = [r for r in runs if r not in by_name]
    if missing:
        raise HTTPException(404, f"{m['name']} has no bags in run {missing[0]}")
    live = [r for r in runs if by_name[r].get("recording")]
    if live:
        raise HTTPException(
            409, f"{live[0]} is still being recorded: stop the recording first"
        )
    root, name = scan["root"], m["name"]
    bags = [(r, b) for r in runs for b in by_name[r]["bags"]]
    total = sum(b["bytes"] for _, b in bags)
    safe = [r for r in runs if target and by_name[r].get("on_drive") == "verified"]
    unsafe = [r for r in runs if r not in safe]

    def run(job):
        if not unsafe:
            copies = f"All of them are on {body.drive} with their hashes checked."
        elif target:
            copies = (
                f"{len(unsafe)} of them are NOT on {body.drive} with checked hashes "
                f"({', '.join(unsafe[:4])}{', ...' if len(unsafe) > 4 else ''}): "
                "this may be the only copy."
            )
        else:
            copies = "No drive was checked for copies: this may be the only copy."
        answer = job.confirm(
            f"Delete {len(runs)} run(s), {len(bags)} bag folder(s) "
            f"({bd.human(total)}) from {name}? {copies} This cannot be undone.",
            kind="bag_delete",
            required_text="delete",
            yes_label=f"Delete from {name}",
            no_label="Keep them",
            detail={"bags": [f"{r}/{b['name']}" for r, b in bags], "not_on_drive": unsafe},
        )  # fmt: skip
        result = {"removed": [], "bytes": 0}
        if not answer:
            job.log("info", f"Nothing deleted from {name}.")
            return result
        # look again: a recording may have started while the question was open
        fresh = {r["name"]: r for r in _scan_bags(svc, m, None)["runs"]}
        for run_name, bag in bags:
            label = f"{run_name}/{bag['name']}"
            now = fresh.get(run_name)
            if now and now.get("recording"):
                job.log("error", f"{label}: is being recorded now, not deleted")
                return False
            rc, out, err = bd.run_cmd(svc, m, bd.remove_command(f"{root}/{label}"), 300)
            if rc != 0:
                job.log("error", f"could not delete {label}: {(err or out)[-200:]}")
                return False
            result["removed"].append(label)
            result["bytes"] += bag["bytes"]
            job.log("ok", f"deleted {root}/{label}")
        for run_name in runs:
            bd.run_cmd(svc, m, bd.prune_command(f"{root}/{run_name}"), 30)
        job.log("ok", f"Done: {bd.human(result['bytes'])} freed on {name}.")
        return result

    job = svc.jobs.start("bag_delete", f"Delete bags on {name}", run)
    return {"job_id": job.id, "bytes": total, "not_on_drive": unsafe}
