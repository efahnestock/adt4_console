"""Mission-prep files on the live map (API.md: /api/prep).

* ``check``: resolve a file against the live map and dry-run it (nothing written).
* ``apply``: resolve + commit as ONE map edit (``EditService.apply(stage=False)``:
  one transaction, one map-version bump, one undo entry, plan repair notified) and
  record it in ``_PrepApplied`` (per map epoch, so a reseed starts an empty list).
* ``reset("files")``: undo the applied files newest first (each one's own
  ``GraphMutation``); live edits made outside the files are kept.  An undo that
  conflicts with a later edit stops the reset and names it.
* ``reset("prior")``: reseed the map from the launched prior (drops every edit).
* Trial logs: while a trial records, every applied file is copied into the trial's
  console dir (``prep_files/``) with a ``prep_apply`` event, a reset logs ``prep_reset``,
  and a trial's start / stop context copies in the files the map already carries.
* ``wait_for_seed``: the launch job waits for the base station's reseed
  (``_MapState.epoch`` changes) before applying the launch's files.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time

from .core import PrepError, file_hash, prepare, snapshot_from_neo4j, validate_file

log = logging.getLogger(__name__)

RESET_FILES_CONFIRM = "reset"
RESET_PRIOR_CONFIRM = "reseed"
_REC = "_PrepApplied"


def _session(driver, write: bool = False):
    mode = "WRITE" if write else "READ"
    try:
        return driver.session(
            default_access_mode=mode, notifications_min_severity="OFF"
        )
    except TypeError:
        return driver.session(default_access_mode=mode)


class PrepService:
    def __init__(self, maps, edits, graph=None, hub=None, launch_memory=None):
        self.maps = maps  # MapService: .target() -> the live Neo4j source
        self.edits = edits  # EditService
        self.graph = graph
        self.hub = hub
        self.launch_memory = launch_memory
        self.trials = None  # TrialService: applied files go into the recording trial

    # ------------------------------------------------------------- target
    def source(self, src_id: str | None = None):
        if src_id:
            return self.edits.source(src_id)
        return self.maps.target()

    @staticmethod
    def driver(src):
        db = src.db()
        return getattr(db, "driver", None) or db

    def map_state(self, src=None) -> dict:
        src = src or self.source()
        with _session(self.driver(src)) as s:
            rec = s.run(
                "MATCH (m:_MapState) RETURN m.epoch AS epoch, m.version AS version, "
                "m.prior_path AS prior_path"
            ).single()
        return (
            dict(rec) if rec else {"epoch": None, "version": None, "prior_path": None}
        )

    # ------------------------------------------------------------- check / apply
    def _resolve(self, f, src):
        f = validate_file(f)
        t = time.monotonic()
        snap = snapshot_from_neo4j(src.db())
        res = prepare(f, snap)
        res["resolve_s"] = round(time.monotonic() - t, 2)
        return f, snap, res

    def _report(self, f, snap, res) -> dict:
        return {
            "name": f["name"],
            "summary": f.get("summary") or f["name"],
            "hash": file_hash(f),
            "target": getattr(self.source(), "id", None),
            "map": {"prior": snap.prior, "epoch": snap.epoch, "version": snap.version},
            "file_prior": res["file_prior"],
            "prior_ok": res["prior_ok"],
            "same_as_file": res["same_as_file"],
            "problems": res["problems"],
            "warnings": res["report"]["warnings"],
            "steps": res["report"]["steps"],
            "counts": res["report"]["counts"],
            "resolve_s": res["resolve_s"],
        }

    def check(self, f, src_id: str | None = None) -> dict:
        src = self.source(src_id)
        f, snap, res = self._resolve(f, src)
        out = self._report(f, snap, res)
        out["dry_run"] = None
        if not res["problems"]:
            try:
                dry = self.edits.apply(src.id, res["ops"], dry_run=True,
                                       summary=f"mission prep: {f['name']}")  # fmt: skip
                out["dry_run"] = {"ok": True, "changes": len(dry.get("changes") or [])}
            except Exception as e:  # noqa: BLE001 - reported, nothing was written
                out["dry_run"] = {"ok": False, "error": str(e)}
                out["problems"] = [*out["problems"], f"dry run: {e}"]
        out["ok"] = not out["problems"]
        return out

    def apply(self, f, src_id: str | None = None, *, via: str = "ui") -> dict:
        src = self.source(src_id)
        f, snap, res = self._resolve(f, src)
        if res["problems"]:
            raise PrepError("; ".join(res["problems"]))
        if not res["ops"]:  # e.g. "restore every removed place" on a fresh map
            return {**self._report(f, snap, res), "applied": None, "apply_s": 0.0, "ok": True,
                    "nothing_to_do": True}  # fmt: skip
        t = time.monotonic()
        out = self.edits.apply(
            src.id, res["ops"], stage=False,
            author={"via": f"mission prep ({via})", "prep_file": f["name"]},
            summary=f"mission prep: {f.get('summary') or f['name']}",
        )  # fmt: skip
        apply_s = round(time.monotonic() - t, 2)
        rec = {
            "name": f["name"],
            "summary": f.get("summary") or f["name"],
            "hash": file_hash(f),
            "mutation_id": out.get("mutation_id"),
            "map_version": out.get("map_version"),
            "epoch": snap.epoch,
            "t": time.time(),
            "via": via,
            "counts": res["report"]["counts"],
        }
        self._record(src, {**rec, "file_json": json.dumps(f, sort_keys=True)})
        self._log_trial("prep_apply", f, rec)
        self._publish(src)
        return {
            **self._report(f, snap, res),
            "applied": rec,
            "apply_s": apply_s,
            "ok": True,
        }

    # ------------------------------------------------------------- applied list
    def _record(self, src, rec: dict) -> None:
        with _session(self.driver(src), write=True) as s:
            s.run(f"MATCH (r:{_REC}) WHERE r.epoch <> $e OR r.epoch IS NULL DETACH DELETE r",
                  e=rec["epoch"] or "")  # fmt: skip
            s.run(
                f"MATCH (r:{_REC}) WITH coalesce(max(r.seq), 0) AS m "
                f"CREATE (n:{_REC}) SET n = $p, n.seq = m + 1",
                p={k: v for k, v in rec.items() if k != "counts"}
                | {"counts": ",".join(f"{k}={v}" for k, v in rec["counts"].items())},
            )

    def applied(self, src_id: str | None = None) -> list[dict]:
        """Files applied to the current map (this epoch), oldest first."""
        src = self.source(src_id)
        epoch = self.map_state(src).get("epoch")
        with _session(self.driver(src)) as s:
            rows = s.run(
                f"MATCH (r:{_REC}) WHERE r.epoch = $e RETURN properties(r) AS p ORDER BY r.seq",
                e=epoch or "",
            )
            return [dict(r["p"]) for r in rows]

    def applied_files(self, src_id: str | None = None) -> list[tuple[dict, dict]]:
        """``(record without the file, the file)`` per applied file, oldest first."""
        out = []
        for r in self.applied(src_id):
            raw = r.pop("file_json", None)
            try:
                f = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                f = None
            out.append((r, f))
        return out

    def status(self, src_id: str | None = None) -> dict:
        src = self.source(src_id)
        ms = self.map_state(src)
        from .core import prior_name

        return {"target": getattr(src, "id", None), "epoch": ms.get("epoch"), "version": ms.get("version"),
                "prior": prior_name(ms.get("prior_path")),
                "applied": [r for r, _ in self.applied_files(src_id)]}  # fmt: skip

    # ------------------------------------------------------------- reset
    def reset(self, mode: str, confirm: str, src_id: str | None = None) -> dict:
        if mode == "files":
            if confirm != RESET_FILES_CONFIRM:
                raise PrepError(
                    f'type "{RESET_FILES_CONFIRM}" to undo the applied edit files'
                )
            return self._reset_files(src_id)
        if mode == "prior":
            if confirm != RESET_PRIOR_CONFIRM:
                raise PrepError(
                    f'type "{RESET_PRIOR_CONFIRM}" to reload the prior map (drops ALL edits)'
                )
            return self._reset_prior(src_id)
        raise PrepError("mode is 'files' or 'prior'")

    def _reset_files(self, src_id) -> dict:
        src = self.source(src_id)
        recs = self.applied(src_id)
        undone = []
        for rec in reversed(recs):
            try:
                self.edits.undo(src.id, rec["mutation_id"],
                                author={"via": "mission prep reset", "prep_file": rec["name"]})  # fmt: skip
            except Exception as e:  # noqa: BLE001 - EditConflict or a vanished mutation
                self._publish(src)
                return {"ok": False, "undone": undone, "failed": rec["name"],
                        "error": f"cannot undo {rec['name']!r}: {e}. A later edit touched the same "
                                 "nodes: undo that edit first, or reset to the prior (drops all edits)."}  # fmt: skip
            with _session(self.driver(src), write=True) as s:
                s.run(
                    f"MATCH (r:{_REC}) WHERE r.mutation_id = $m DETACH DELETE r",
                    m=rec["mutation_id"],
                )
            undone.append(rec["name"])
        self._log_trial("prep_reset", None, {"mode": "files", "undone": undone})
        self._publish(src)
        return {"ok": True, "undone": undone}

    def _reset_prior(self, src_id) -> dict:
        src = self.source(src_id)
        prior = self.map_state(src).get("prior_path")
        if not prior:
            raise PrepError(
                "the map records no prior (_MapState.prior_path): load it from Maps instead"
            )
        try:
            from heracles_ros.map_state import seed_db
        except ImportError as e:
            raise PrepError(f"reseeding needs heracles_ros ({e})") from None
        t = time.monotonic()
        version = seed_db(src.uri, src.creds, prior, image_store="auto", new_session=False,
                          log=lambda m: log.info("prep reset: %s", m))  # fmt: skip
        if self.graph is not None:
            try:
                self.graph.refresh(src.id, "map reseeded (mission prep reset)")
            except Exception:  # noqa: BLE001
                log.exception("refresh after reseed")
        self._log_trial(
            "prep_reset",
            None,
            {"mode": "prior", "prior": prior, "map_version": version},
        )
        self._publish(src)
        return {"ok": True, "prior": prior, "map_version": version,
                "seconds": round(time.monotonic() - t, 1)}  # fmt: skip

    # ------------------------------------------------------------- launch
    def wait_for_seed(self, epoch_before: str | None, timeout_s: float = 180.0,
                      poll_s: float = 2.0, log_fn=None) -> bool:  # fmt: skip
        """True once the live map's epoch differs from ``epoch_before`` (the base
        station reseeded it); False on timeout."""
        t_end = time.monotonic() + timeout_s
        last_err = None
        while time.monotonic() < t_end:
            try:
                ep = self.map_state().get("epoch")
                if ep and ep != epoch_before:
                    return True
            except Exception as e:  # noqa: BLE001 - Neo4j restarting
                if str(e) != last_err and log_fn:
                    log_fn(f"waiting for the map: {e}")
                last_err = str(e)
            time.sleep(poll_s)
        return False

    # ------------------------------------------------------------- files on the base
    MAX_FILES = 300
    MAX_BYTES = 8 << 20

    def prep_dir(self) -> pathlib.Path:
        s = getattr(self.maps, "settings", None)
        d = getattr(s, "prep_dir", None)
        if not d:
            root = getattr(s, "output_root", None) or "~/adt4_output"
            d = str(pathlib.Path(root) / "mission_prep")
        return pathlib.Path(d).expanduser()

    def list_files(self) -> dict:
        """The mission-prep files on this machine (``prep_dir``, recursively), newest
        first: ``{dir, files: [{path (relative), name, summary, prior, counts, mtime}]}``;
        JSON that is not a mission-prep file is left out."""
        root = self.prep_dir()
        out = []
        if root.is_dir():
            paths = sorted(
                root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for p in paths[: self.MAX_FILES]:
                try:
                    if p.stat().st_size > self.MAX_BYTES:
                        continue
                    f = validate_file(p.read_text())
                except (OSError, PrepError):
                    continue
                out.append({"path": str(p.relative_to(root)), "name": f["name"],
                            "summary": f.get("summary") or "", "prior": (f.get("map") or {}).get("prior"),
                            "counts": (f.get("report") or {}).get("counts") or {},
                            "mtime": p.stat().st_mtime})  # fmt: skip
        return {"dir": str(root), "files": out}

    def read_file(self, rel: str) -> dict:
        """A listed file's content (``rel`` inside ``prep_dir``; nothing outside it)."""
        root = self.prep_dir().resolve()
        p = (root / rel).resolve()
        if root != p and root not in p.parents:
            raise PrepError("not a file in the mission-prep folder")
        if p.suffix != ".json" or not p.is_file():
            raise PrepError(f"no such mission-prep file: {rel}")
        return validate_file(p.read_text())

    # ------------------------------------------------------------- trial logs
    def _trial(self):
        tr = self.trials
        return (
            (tr, tr.active)
            if tr is not None and getattr(tr, "active", None)
            else (None, None)
        )

    def _copy_into_trial(self, trials, trial, f: dict, rec: dict) -> str | None:
        """``<trial>/console/prep_files/<seq>_<name>_<hash>.json`` (the file as applied +
        what it resolved to); the path relative to the trial's console dir."""
        try:
            d = pathlib.Path(trial["console_dir"]) / "prep_files"
            d.mkdir(parents=True, exist_ok=True)
            n = len(list(d.glob("*.json"))) + 1
            name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(f.get("name") or "prep"))[:40]
            path = d / f"{n:02d}_{name}_{rec.get('hash') or 'x'}.json"
            path.write_text(
                json.dumps({**f, "applied": rec}, indent=1, sort_keys=True, default=str)
            )
            return str(path.relative_to(pathlib.Path(trial["console_dir"])))
        except Exception as e:  # noqa: BLE001 - never fail the edit over the log
            log.error(
                "mission-prep file not copied into trial %s: %s",
                trial.get("trial_id"),
                e,
            )
            return None

    def _log_trial(self, kind: str, f: dict | None, rec: dict) -> None:
        trials, trial = self._trial()
        if trial is None:
            return
        fields = {k: v for k, v in rec.items() if k != "file_json"}
        if f is not None:
            fields["file"] = self._copy_into_trial(trials, trial, f, rec)
            trial.setdefault("prep_copied", []).append(rec.get("hash"))
        try:
            trials.event(kind, trial, **fields)
        except Exception:  # noqa: BLE001
            log.exception("trial event %s", kind)

    def trial_context(self) -> dict:
        """For a trial's start / stop context: the map's applied files, each copied
        into the recording trial (once per file hash)."""
        st = self.status()
        trials, trial = self._trial()
        if trial is not None:
            seen = trial.setdefault(
                "prep_copied", []
            )  # file hashes (trial.json is JSON)
            for rec, f in self.applied_files():
                if f is not None and rec.get("hash") not in seen:
                    rec["file"] = self._copy_into_trial(trials, trial, f, rec)
                    seen.append(rec.get("hash"))
            st["applied"] = [r for r, _ in self.applied_files()]
        return st

    # ------------------------------------------------------------- misc
    def _publish(self, src) -> None:
        if self.hub is not None:
            try:
                self.hub.publish(
                    f"prep/{src.id}", {"kind": "changed", "t": time.time()}
                )
            except Exception:  # noqa: BLE001
                pass
