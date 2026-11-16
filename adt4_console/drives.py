"""USB bag drives on fleet machines: status, eject, and copying bags onto them.

Each robot runs the ``bag-drive`` helper (a udev rule + systemd unit mount any
USB drive at ``/mnt/bag_drives/<label or UUID>``).  The console only talks to it
over ssh:

* ``bag-drive status --json``   drives, free space, USB link speed, where to write
* ``sudo -n bag-drive eject N`` sync, unmount and power the drive down

A copy runs ``rsync`` on the robot itself, from its run directory straight onto
the drive, so the data never crosses the radio; the console only starts it and
follows its progress.

What counts as a bag (``BAG_SCAN``) is deliberately in one place: a directory
with a ``metadata.yaml`` next to ``*.mcap`` / ``*.db3`` files, inside a run of
the output root.  A trial's ``console`` folder (``trial.json``, the event log
and the screen video) is listed and copied the same way, though it has no bag
file.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading

from .terminal import SSH_OPTS

BAG_DRIVE = "/usr/local/sbin/bag-drive"
STATUS_CMD = f"{BAG_DRIVE} status --json"
STATUS_TIMEOUT_S = 10
EJECT_TIMEOUT_S = 90  # a sync of a busy drive can take a while
SCAN_TIMEOUT_S = 30

# drive names are bag-drive's sanitized labels / UUIDs; runs are directory names
DRIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
BAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")

# usb_speed_mbps below this is a USB 2.0 link (wrong port or cable)
FAST_LINK_MBPS = 5000


def valid_drive(name: str | None) -> bool:
    return bool(name) and DRIVE_RE.match(name) is not None and ".." not in name


def valid_run(name: str | None) -> bool:
    return bool(name) and RUN_RE.match(name) is not None and ".." not in name


def valid_file(name: str | None) -> bool:
    """A plain file name inside a bag directory (no path separators)."""
    return (
        bool(name)
        and "/" not in name
        and name not in (".", "..")
        and "\x00" not in name
    )


def valid_bag(name: str | None) -> bool:
    return bool(name) and BAG_RE.match(name) is not None and ".." not in name


# ------------------------------------------------------------------ status
def parse_status(rc: int, out: str, err: str = "") -> dict:
    """``{"installed", "drives", "error"}`` from a ``bag-drive status --json`` run."""
    text = (out or "").strip()
    if rc == 127 or "No such file" in (err or "") or "not found" in (err or ""):
        return {
            "installed": False,
            "drives": [],
            "error": "bag-drive is not installed on this machine",
        }
    start = text.find("{")
    if rc != 0 or start < 0:
        detail = (err or out or "").strip()[-300:]
        return {
            "installed": True,
            "drives": [],
            "error": f"bag-drive status failed (rc {rc}): {detail}",
        }
    try:
        data = json.loads(text[start:])
    except ValueError as e:
        return {"installed": True, "drives": [], "error": f"bad status output: {e}"}
    drives = []
    for d in data.get("drives") or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        speed = int(d.get("usb_speed_mbps") or 0)
        drives.append(
            {
                "name": str(d["name"]),
                "label": d.get("label") or "",
                "uuid": d.get("uuid") or "",
                "fstype": d.get("fstype") or "",
                "model": d.get("model") or "",
                "mountpoint": d.get("mountpoint") or "",
                "write_dir": d.get("write_dir") or "",
                "mounted": bool(d.get("mounted")),
                "managed": bool(d.get("managed")),
                "size_bytes": int(d.get("size_bytes") or 0),
                "free_bytes": int(d.get("free_bytes") or 0),
                "usb_speed_mbps": speed,
                "slow_link": 0 < speed < FAST_LINK_MBPS,
                "error": d.get("error") or "",
            }
        )
    return {"installed": True, "drives": drives, "error": ""}


def eject_command(drive: str) -> str:
    if not valid_drive(drive):
        raise ValueError(f"bad drive name {drive!r}")
    return f"sudo -n {BAG_DRIVE} eject {shlex.quote(drive)}"


# -------------------------------------------------------------------- bags
# Runs under the output root that hold rosbags.  One JSON object on stdout:
# {"root": abs path, "runs": [{name, mtime, bytes, recording, on_drive, bags: [...]}]}
# "recording": a bag file changed in the last minute, so the bag is still being written.
# With a second argument (the drive folder bags of this machine are copied to),
# each bag also says what is there already: "dest_bytes" and "on_drive" =
# "verified" (all bytes there and a hash manifest written by VERIFY), "copied"
# (all bytes there, not hashed yet), "partial" (an interrupted copy) or "".
BAG_SCAN = r"""
import json, os, sys, time
root = os.path.expanduser(sys.argv[1])
dest = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
now = time.time()
runs = []
try:
    names = sorted(os.listdir(root))
except OSError as e:
    print(json.dumps({"root": root, "runs": [], "error": str(e)}))
    sys.exit(0)
def state(bags):
    kinds = {b["on_drive"] for b in bags}
    if kinds == {"verified"}:
        return "verified"
    if kinds <= {"verified", "copied"}:
        return "copied"
    return "partial" if kinds != {""} else ""
for name in names:
    run = os.path.join(root, name)
    if not os.path.isdir(run) or os.path.islink(run):
        continue
    bags = []
    try:
        subs = sorted(os.listdir(run))
    except OSError:
        continue
    for sub in subs:
        d = os.path.join(run, sub)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        try:
            files = [f for f in os.scandir(d) if f.is_file(follow_symlinks=False)]
        except OSError:
            continue
        # a bag, or a trial's console folder (trial.json + events.jsonl + the
        # screen video; no bag file in it)
        if not any(f.name.endswith((".mcap", ".db3")) for f in files) and not (
            sub == "console" and any(f.name == "trial.json" for f in files)
        ):
            continue
        stats = [f.stat() for f in files]
        total = sum(st.st_size for st in stats)
        bag = {"name": sub, "bytes": total, "files": len(files),
               "metadata": any(f.name == "metadata.yaml" for f in files),
               "recording": now - max(st.st_mtime for st in stats) < 60,
               "dest_bytes": 0, "on_drive": ""}
        if dest:
            there = 0
            for f, st in zip(files, stats):
                try:
                    there += min(os.path.getsize(os.path.join(dest, name, sub, f.name)), st.st_size)
                except OSError:
                    pass
            bag["dest_bytes"] = there
            if there >= total and total > 0:
                done = os.path.isfile(os.path.join(dest, name, sub + ".sha256"))
                bag["on_drive"] = "verified" if done else "copied"
            elif there > 0:
                bag["on_drive"] = "partial"
        bags.append(bag)
    if bags:
        runs.append({"name": name, "mtime": os.path.getmtime(run),
                     "bytes": sum(b["bytes"] for b in bags),
                     "dest_bytes": sum(b["dest_bytes"] for b in bags),
                     "recording": any(b["recording"] for b in bags),
                     "on_drive": state(bags), "bags": bags})
runs.sort(key=lambda r: -r["mtime"])
print(json.dumps({"root": root, "runs": runs}))
"""


def bag_scan_command(root: str, dest: str | None = None) -> str:
    """List the bags under ``root``; with ``dest`` (``machine_dir(...)``), also
    what of each is already on the drive."""
    cmd = f"python3 -c {shlex.quote(BAG_SCAN)} {shlex.quote(root)}"
    return f"{cmd} {shlex.quote(dest)}" if dest else cmd


def parse_bag_scan(rc: int, out: str, err: str = "") -> dict:
    text = (out or "").strip()
    start = text.rfind('{"root"')
    if rc != 0 or start < 0:
        raise RuntimeError(
            f"bag listing failed (rc {rc}): {(err or out or '').strip()[-300:]}"
        )
    data = json.loads(text[start:])
    if data.get("error"):
        # no output root yet on this machine: nothing recorded, not a failure
        return {"root": data.get("root", ""), "runs": [], "note": data["error"]}
    runs = [
        r
        for r in data.get("runs") or []
        if valid_run(r.get("name"))
        and all(valid_bag(b.get("name")) for b in r.get("bags") or [])
    ]
    return {"root": data.get("root", ""), "runs": runs}


# -------------------------------------------------------------------- copy
RSYNC_PROGRESS = re.compile(r"([\d,]+)\s+(\d+)%\s+(\S+/s)")


def parse_progress(line: str) -> tuple[int, float, str] | None:
    """(bytes done, percent, rate) from an ``rsync --info=progress2`` line."""
    m = RSYNC_PROGRESS.search(line)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), float(m.group(2)), m.group(3)


def machine_dir(write_dir: str, machine: str) -> str:
    """The folder on a drive that holds one machine's bags."""
    return "/".join([write_dir.rstrip("/"), machine])


def dest_dir(write_dir: str, machine: str, run: str, bag: str) -> str:
    """Where a bag lands on the drive: ``<write dir>/<machine>/<run>/<bag>``."""
    return "/".join([machine_dir(write_dir, machine), run, bag])


def copy_command(src: str, dst: str) -> str:
    """Copy bag directory ``src`` to ``dst`` on the same machine and flush it.

    ``--append`` is what makes an interrupted copy resumable: a file that is
    already partly on the drive continues where it stopped instead of starting
    over (a local rsync otherwise rewrites the whole file).  It trusts the part
    that is there; the hash check after the copy is what proves it.  ``-rt`` and
    not ``-a``: an exFAT drive has no owners or permissions to preserve.
    """
    s, d = shlex.quote(src.rstrip("/") + "/"), shlex.quote(dst.rstrip("/") + "/")
    return (
        f"mkdir -p {d} && "
        f"rsync -rt --append --no-inc-recursive --info=progress2,name0 {s} {d} && "
        f"sync -f {d}"
    )


# Hash every file of a bag and of its copy and compare.  Source and copy are
# read at the same time, one thread each, so the check takes as long as reading
# the slower of the two once (SHA-256 runs at ~2.5 GB/s a core on the robots'
# CPUs: the drive is the limit, not the hash).  The copy is dropped from the
# page cache first, so what is hashed is what is on the drive, not what the
# kernel still holds in memory from writing it.
# stdout: "P <bytes done> <bytes total> <MB/s>" progress lines, "DIFF <file>:
# <why>" for each mismatch, then "VERIFIED <files> <bytes> <seconds>" (exit 0)
# or "FAILED <mismatches>" (exit 3).  On success the hashes are written to the
# manifest (sha256sum format) next to the copy.
VERIFY = r"""
import hashlib, os, sys, threading, time
src, dst, manifest = sys.argv[1], sys.argv[2], sys.argv[3]
CHUNK = 16 << 20
names = sorted(f.name for f in os.scandir(src) if f.is_file(follow_symlinks=False))
total = sum(os.path.getsize(os.path.join(src, n)) for n in names)
done = [0]
lock = threading.Lock()
t0 = time.time()
def digest(path, count, out, key):
    h = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            if not count:
                try:
                    os.fsync(fd)
                except OSError:
                    pass
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
            while True:
                b = os.read(fd, CHUNK)
                if not b:
                    break
                h.update(b)
                if count:
                    with lock:
                        done[0] += len(b)
        finally:
            os.close(fd)
        out[key] = h.hexdigest()
    except OSError as e:
        out[key] = "error: %s" % e
stop = threading.Event()
def report():
    while not stop.wait(0.5):
        with lock:
            d = done[0]
        print("P %d %d %.0f" % (d, total, d / 1e6 / max(time.time() - t0, 1e-3)), flush=True)
threading.Thread(target=report, daemon=True).start()
bad, sums = 0, []
for n in names:
    s, d = os.path.join(src, n), os.path.join(dst, n)
    try:
        if os.path.getsize(d) != os.path.getsize(s):
            print("DIFF %s: size %d on the drive, %d on the robot" % (n, os.path.getsize(d), os.path.getsize(s)), flush=True)
            bad += 1
            with lock:
                done[0] += os.path.getsize(s)
            continue
    except OSError as e:
        print("DIFF %s: %s" % (n, e), flush=True)
        bad += 1
        continue
    out = {}
    a = threading.Thread(target=digest, args=(s, True, out, "s"))
    b = threading.Thread(target=digest, args=(d, False, out, "d"))
    a.start(); b.start(); a.join(); b.join()
    if out["s"] != out["d"] or out["s"].startswith("error"):
        print("DIFF %s: sha256 %s on the robot, %s on the drive" % (n, out["s"], out["d"]), flush=True)
        bad += 1
    else:
        sums.append("%s  %s" % (out["s"], n))
stop.set()
if bad:
    print("FAILED %d" % bad, flush=True)
    sys.exit(3)
with open(manifest, "w") as f:
    f.write("\n".join(sums) + "\n")
    f.flush()
    os.fsync(f.fileno())
print("VERIFIED %d %d %.1f" % (len(names), total, time.time() - t0), flush=True)
"""

VERIFY_PROGRESS = re.compile(r"^P (\d+) (\d+) (\d+)$")
VERIFY_DONE = re.compile(r"^VERIFIED (\d+) (\d+) ([\d.]+)$")


def manifest_path(dst: str) -> str:
    """The hash manifest of the copy at ``dst``: ``<dst>.sha256``, beside it."""
    return dst.rstrip("/") + ".sha256"


def verify_command(src: str, dst: str) -> str:
    """Hash ``src`` and its copy ``dst`` and compare (see ``VERIFY``)."""
    args = " ".join(
        shlex.quote(a) for a in (src.rstrip("/"), dst.rstrip("/"), manifest_path(dst))
    )
    return f"python3 -c {shlex.quote(VERIFY)} {args}"


def parse_verify(line: str) -> tuple[str, tuple]:
    """A VERIFY output line as (kind, values): ``("progress", (done, total, mbps))``,
    ``("done", (files, bytes, seconds))``, ``("diff", (text,))`` or ``("other", (line,))``."""
    m = VERIFY_PROGRESS.match(line)
    if m:
        return "progress", (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = VERIFY_DONE.match(line)
    if m:
        return "done", (int(m.group(1)), int(m.group(2)), float(m.group(3)))
    if line.startswith("DIFF "):
        return "diff", (line[5:],)
    return "other", (line,)


def remove_command(src: str) -> str:
    return f"rm -rf -- {shlex.quote(src.rstrip('/'))}"


# ---------------------------------------------------------- running things
def run_cmd(svc, m: dict, cmd: str, timeout: float) -> tuple[int, str, str]:
    """Run ``cmd`` on machine ``m``: (rc, stdout, stderr)."""
    if m.get("local"):
        try:
            p = subprocess.run(
                ["bash", "-c", cmd],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        return p.returncode, p.stdout, p.stderr
    return svc.helpers.ssh_cmd(m["user"], m["ip"], cmd, timeout=timeout)


def stream_cmd(job, m: dict, cmd: str, on_line) -> int:
    """Run ``cmd`` on ``m``, each output line (split on CR and LF, so rsync's
    progress updates arrive as they happen) to ``on_line``; cancellable."""
    import queue

    if m.get("local"):
        argv = ["bash", "-c", cmd]
    else:
        # a tty, so cancelling (killing ssh) hangs up the remote rsync too
        argv = ["ssh", "-tt", *SSH_OPTS, f"{m['user']}@{m['ip']}", cmd]
    p = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    lines: queue.Queue = queue.Queue()

    def read():
        buf = b""
        while True:
            chunk = p.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]+", buf)
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    lines.put(part.decode("utf-8", "replace").strip())
        if buf.strip():
            lines.put(buf.decode("utf-8", "replace").strip())
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    try:
        while True:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                job.check_cancelled()
                continue
            if line is None:
                break
            on_line(line)
            job.check_cancelled()
    except BaseException:
        p.kill()
        p.wait()
        raise
    return p.wait()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
