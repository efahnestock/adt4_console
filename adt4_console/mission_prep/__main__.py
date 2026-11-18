"""``python -m adt4_console.mission_prep``: make, check and render mission-prep files.

  compile  --intent steps.json --name mission1 [--summary ...] --neo4j bolt://H:P --out m1.json
           [--after earlier.json ...]   resolve against the map (+ earlier files, not yet applied)
  render   --file m1.json --neo4j bolt://H:P --geo-anchor geo_anchor.json --out-dir DIR
           [--after earlier.json ...]   plain + satellite PNGs, whole map + zoom on the edits
  verify   --file m1.json --neo4j bolt://H:P      what loading it now would do (read only)
  apply    --file m1.json --neo4j bolt://H:P --yes   TEST databases only (the console applies
           files on a live map: Launch window / map editor)

Reads only, except ``apply``.  Credentials: --user/--password or NEO4J_USER /
NEO4J_PASSWORD (default neo4j / neo4j_pw).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from .core import compile_intent, make_file, prepare, snapshot_from_neo4j, validate_file


def _driver(a):
    from neo4j import GraphDatabase

    user = a.user or os.environ.get("NEO4J_USER", "neo4j")
    pw = a.password or os.environ.get("NEO4J_PASSWORD", "neo4j_pw")
    return GraphDatabase.driver(a.neo4j, auth=(user, pw))


def _snapshot_after(a, drv):
    """The live map, then each ``--after`` file's intent on top (not applied)."""
    snap = snapshot_from_neo4j(drv)
    for path in a.after or []:
        f = validate_file(json.loads(pathlib.Path(path).read_text()))
        _, rep, snap = compile_intent(f["intent"], snap)
        if rep["unresolved"]:
            sys.exit(f"--after {path}: unresolved: {rep['unresolved']}")
        for r in snap.rooms.values():
            r.pop("added", None)
        for o in snap.objects.values():
            o.pop("added", None)
    return snap


def referenced_places(intent) -> list[str]:
    out = []
    for st in intent:
        for k in ("places", "at", "polygon_from"):
            out += [str(x) for x in st.get(k) or []]
        if isinstance(st.get("near"), dict):
            out.append(str(st["near"].get("place")))
    return list(dict.fromkeys(out))


def report_md(f: dict) -> str:
    rep = f["report"]
    lines = [f"# {f['name']}", "", f.get("summary") or "", "",
             f"Map: `{(f.get('map') or {}).get('prior')}` (version {(f.get('map') or {}).get('version')} when compiled)",
             "", "| step | result |", "|---|---|"]  # fmt: skip
    for st in rep["steps"]:
        do = st["do"]
        if do == "delete_objects":
            res = f"delete {len(st['deleted'])} `{st.get('class')}`: {', '.join(st['deleted'])}"
        elif do == "add_objects":
            res = "add " + ", ".join(
                f"`{a['symbol']}` {st['class']} at {a['place']}" for a in st["added"]
            )
        elif do == "retire_places":
            res = f"remove {len(st['retired'])} places (obstacles): {', '.join(st['retired'])}"
        elif do == "restore_places":
            res = f"restore {len(st['restored'])} places: {', '.join(st['restored'])}"
        elif do == "add_region":
            took = (
                ", ".join(f"{v} from {k}" for k, v in st["taken_from"].items())
                or "none"
            )
            res = (f"region `{st['symbol']}` **{st['name']}** ({st['class']}), {len(st['members'])} places: "
                   f"{', '.join(st['members'])}; taken from other regions: {took}")  # fmt: skip
        else:
            res = json.dumps({k: v for k, v in st.items() if k != "do"})
        lines.append(f"| {do} | {res} |")
    lines += ["", f"Ops: {rep['counts']}", ""]
    if rep["unresolved"]:
        lines += ["**Unresolved:**", *[f"- {u}" for u in rep["unresolved"]], ""]
    if rep["warnings"]:
        lines += ["Warnings:", *[f"- {u}" for u in rep["warnings"]], ""]
    return "\n".join(lines)


def cmd_compile(a):
    intent = json.loads(pathlib.Path(a.intent).read_text())
    drv = _driver(a)
    snap = _snapshot_after(a, drv)
    f = make_file(a.name, intent, snap, a.summary or "")
    if a.after:
        f["after"] = [pathlib.Path(p).name for p in a.after]
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(f, indent=1))
    md = out.with_name(out.stem + "_report.md")
    md.write_text(report_md(f))
    print(report_md(f))
    print(f"\nwrote {out} and {md}")
    return 1 if f["report"]["unresolved"] else 0


def cmd_render(a):
    from ..geo import GeoAnchor
    from .render import render

    f = validate_file(json.loads(pathlib.Path(a.file).read_text()))
    drv = _driver(a)
    before = _snapshot_after(a, drv)
    _, rep, after = compile_intent(f["intent"], before)
    anchor = GeoAnchor.load(a.geo_anchor) if a.geo_anchor else None
    d = pathlib.Path(a.out_dir)
    d.mkdir(parents=True, exist_ok=True)
    refs = referenced_places(f["intent"])
    outs = []
    for sat in (False, True):
        if sat and anchor is None:
            print("no --geo-anchor: skipping the satellite renders")
            continue
        for zoom in (False, True):
            name = f"{f['name']}_{'satellite' if sat else 'plain'}_{'zoom' if zoom else 'full'}.png"
            title = f"{f['name']}: {f.get('summary') or ''}" + (
                " (satellite)" if sat else ""
            )
            outs.append(render(before, after, rep, str(d / name), anchor=anchor, satellite=sat, zoom=zoom,
                               title=title, referenced=refs))  # fmt: skip
    print("\n".join(outs))
    return 0


def cmd_verify(a):
    f = validate_file(json.loads(pathlib.Path(a.file).read_text()))
    snap = snapshot_from_neo4j(_driver(a))
    res = prepare(f, snap)
    print(json.dumps({k: res[k] for k in ("prior_ok", "same_as_file", "problems", "map_prior", "file_prior")}
                     | {"counts": res["report"]["counts"]}, indent=1))  # fmt: skip
    return 1 if res["problems"] else 0


def cmd_apply(a):
    if not a.yes:
        sys.exit("apply writes to the database: pass --yes (test databases only)")
    from heracles import graph_edit as ge

    f = validate_file(json.loads(pathlib.Path(a.file).read_text()))
    drv = _driver(a)
    res = prepare(f, snapshot_from_neo4j(drv))
    if res["problems"]:
        sys.exit(f"unresolved: {res['problems']}")
    out = ge.apply_edit(drv, res["ops"], provenance={"source": "mission_prep cli", "prep_file": f["name"]},
                        summary=f"mission prep: {f['name']}")  # fmt: skip
    print(
        json.dumps({k: out.get(k) for k in ("mutation_id", "map_version", "revision")})
    )
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m adt4_console.mission_prep", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--neo4j", required=True, help="bolt://host:port of the map")
        sp.add_argument("--user")
        sp.add_argument("--password")

    c = sub.add_parser("compile")
    common(c)
    c.add_argument("--intent", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--summary")
    c.add_argument("--out", required=True)
    c.add_argument(
        "--after", nargs="*", help="earlier files (not yet applied) this one builds on"
    )
    r = sub.add_parser("render")
    common(r)
    r.add_argument("--file", required=True)
    r.add_argument("--geo-anchor")
    r.add_argument("--out-dir", required=True)
    r.add_argument("--after", nargs="*")
    v = sub.add_parser("verify")
    common(v)
    v.add_argument("--file", required=True)
    ap = sub.add_parser("apply")
    common(ap)
    ap.add_argument("--file", required=True)
    ap.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    return {
        "compile": cmd_compile,
        "render": cmd_render,
        "verify": cmd_verify,
        "apply": cmd_apply,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
