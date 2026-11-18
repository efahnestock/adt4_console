// Mission-prep edit files (adt4-mission-prep/1): pick JSON files that edit the map
// for a mission (delete / add objects, remove places as obstacles, add regions).
//
// - "launch": the Launch window's box. The picked files go with the launch and are
//   applied in order once the base station has reloaded the prior map.
// - "live": the map editor's box. Check / Apply a file on the running map now (each
//   file is one edit, one Undo).
// Both list the files already applied to the current map and have Reset: undo the
// applied files (live edits are kept), or reload the prior map (drops every edit).
import { useRef, useState } from "react";
import { errorText, getJSON, postJSON, useApi } from "../lib/api";

export const PREP_FORMAT = "adt4-mission-prep/1";

export interface PrepFile {
  format: string;
  name: string;
  summary?: string;
  intent: unknown[];
  report?: {
    counts?: Record<string, number>;
    steps?: {
      do: string;
      name?: string;
      members?: string[];
      retired?: string[];
    }[];
  };
  [k: string]: unknown;
}

interface BaseFile {
  path: string;
  name: string;
  summary: string;
  prior: string | null;
  counts: Record<string, number>;
  mtime: number;
}

interface PrepRecord {
  name: string;
  summary?: string;
  hash?: string;
  map_version?: number;
  t?: number;
  via?: string;
  mutation_id?: string;
  seq?: number;
}

interface PrepStatus {
  target: string | null;
  epoch: string | null;
  version: number | null;
  prior: string | null;
  applied: PrepRecord[];
}

interface CheckResult {
  ok: boolean;
  problems: string[];
  warnings: string[];
  counts: Record<string, number>;
  same_as_file: boolean;
  prior_ok: boolean;
  map: { prior: string | null; version: number | null };
  apply_s?: number;
  nothing_to_do?: boolean;
  applied?: { map_version?: number } | null;
}

/** Parse a picked file; throws a readable error. */
export function parsePrepFile(text: string, fileName = "file"): PrepFile {
  let f: unknown;
  try {
    f = JSON.parse(text);
  } catch (e) {
    throw new Error(`${fileName}: not JSON (${String(e)})`);
  }
  const p = f as PrepFile;
  if (!p || typeof p !== "object" || p.format !== PREP_FORMAT)
    throw new Error(
      `${fileName}: not a mission-prep file (format must be ${PREP_FORMAT})`,
    );
  if (!p.name || !Array.isArray(p.intent) || !p.intent.length)
    throw new Error(`${fileName}: needs a name and a non-empty intent`);
  return p;
}

/** "29 deleted · 3 added · 11 places removed · 4 regions" from the file's compiled report. */
export function prepSummary(f: PrepFile): string {
  const c = f.report?.counts ?? {};
  const parts: string[] = [];
  if (c.delete_node) parts.push(`${c.delete_node} deleted`);
  if (c.create_node) parts.push(`${c.create_node} added`);
  if (c.retire_node) parts.push(`${c.retire_node} places removed`);
  if (c.restore_node) parts.push(`${c.restore_node} places restored`);
  if (c.group) parts.push(`${c.group} region${c.group > 1 ? "s" : ""}`);
  return parts.join(" · ") || `${f.intent.length} steps`;
}

const fmtTime = (t?: number) =>
  t ? new Date(t * 1000).toLocaleTimeString() : "";

export function PrepFiles({
  mode,
  files = [],
  onFiles,
}: {
  mode: "launch" | "live";
  files?: PrepFile[];
  onFiles?: (f: PrepFile[]) => void;
}) {
  const status = useApi<PrepStatus>("api/prep");
  const input = useRef<HTMLInputElement>(null);
  const [live, setLive] = useState<PrepFile[]>([]);
  const [checks, setChecks] = useState<Record<string, CheckResult | string>>(
    {},
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const picked = mode === "launch" ? files : live;
  const setPicked = (f: PrepFile[]) =>
    mode === "launch" ? onFiles?.(f) : setLive(f);
  const applied = Array.isArray(status.data?.applied)
    ? status.data.applied
    : [];

  // files on the base station (settings.prep_dir), the usual source
  const baseFiles = useApi<{ dir: string; files: BaseFile[] }>(
    "api/prep/files",
  );
  const [basePick, setBasePick] = useState("");
  const baseList = Array.isArray(baseFiles.data?.files)
    ? baseFiles.data.files
    : [];
  const addBase = async (path: string) => {
    if (!path) return;
    setErr(null);
    try {
      const f = parsePrepFile(
        JSON.stringify(
          await getJSON<PrepFile>(
            `api/prep/file?path=${encodeURIComponent(path)}`,
          ),
        ),
        path,
      );
      if (!picked.some((o) => o.name === f.name)) setPicked([...picked, f]);
      setBasePick("");
    } catch (e) {
      setErr(errorText(e));
    }
  };

  const pick = async (list: FileList | null) => {
    if (!list) return;
    setErr(null);
    const out = [...picked];
    for (const file of Array.from(list)) {
      try {
        const f = parsePrepFile(await file.text(), file.name);
        if (!out.some((o) => o.name === f.name)) out.push(f);
      } catch (e) {
        setErr(errorText(e));
      }
    }
    setPicked(out);
    if (input.current) input.current.value = "";
  };

  const check = async (f: PrepFile) => {
    setChecks((c) => ({ ...c, [f.name]: "checking…" }));
    try {
      const r = await postJSON<CheckResult>("api/prep/check", { file: f });
      setChecks((c) => ({ ...c, [f.name]: r }));
    } catch (e) {
      setChecks((c) => ({ ...c, [f.name]: errorText(e) }));
    }
  };

  const apply = async (f: PrepFile) => {
    if (
      !window.confirm(
        `Apply "${f.name}" to the live map now?\n${prepSummary(f)}\n\nOne edit: Undo or Reset reverts it.`,
      )
    )
      return;
    setBusy(true);
    setErr(null);
    try {
      const r = await postJSON<CheckResult>("api/prep/apply", { file: f });
      setMsg(
        r.nothing_to_do
          ? `${f.name}: nothing to do on this map`
          : `${f.name} applied (map version ${r.applied?.map_version ?? "?"}${r.apply_s != null ? `, ${r.apply_s} s` : ""})`,
      );
      setLive((l) => l.filter((x) => x.name !== f.name));
      status.reload();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (mode === "launch" && picked.length && !applied.length) {
      setPicked([]);
      return;
    }
    const typed = window.prompt(
      `Undo the ${applied.length} edit file(s) applied to the live map (${applied.map((a) => a.name).join(", ")})?\n` +
        "Edits made outside the files are kept. Type reset to continue.",
    );
    if (typed === null) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await postJSON<{
        ok: boolean;
        undone?: string[];
        error?: string;
      }>("api/prep/reset", { mode: "files", confirm: typed });
      if (r.ok) {
        setMsg(
          `Reset: undid ${r.undone?.length ? r.undone.join(", ") : "nothing"}`,
        );
        if (mode === "launch") setPicked([]);
      } else {
        setErr(r.error ?? "reset failed");
        const again = window.prompt(
          `${r.error}\n\nReload the prior map instead? This drops ALL edits (live ones too). Type reseed to continue.`,
        );
        if (again !== null) {
          const p = await postJSON<{ ok: boolean; seconds?: number }>(
            "api/prep/reset",
            { mode: "prior", confirm: again },
          );
          setMsg(
            p.ok
              ? `Prior map reloaded (${p.seconds ?? "?"} s)`
              : "reseed failed",
          );
          setErr(null);
        }
      }
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
      status.reload();
    }
  };

  return (
    <div className="prep-files" data-testid={`prep-files-${mode}`}>
      <div className="row gap wrap">
        <input
          ref={input}
          type="file"
          accept=".json,application/json"
          multiple
          hidden
          aria-label="Mission edit files"
          onChange={(e) => void pick(e.target.files)}
        />
        <select
          className="input input-sm"
          aria-label="Edit files on the base station"
          value={basePick}
          disabled={busy}
          onChange={(e) => {
            setBasePick(e.target.value);
            void addBase(e.target.value);
          }}
          title={
            baseFiles.data
              ? `Files in ${baseFiles.data.dir} on the base station`
              : "Files on the base station"
          }
          data-testid={`prep-base-files-${mode}`}
        >
          <option value="">
            {baseFiles.error
              ? "base station files unavailable"
              : !baseFiles.data
                ? "loading files…"
                : baseList.length
                  ? `${mode === "launch" ? "Add" : "Load"} a file from the base station (${baseList.length})…`
                  : `no edit files in ${baseFiles.data.dir}`}
          </option>
          {baseList.map((f) => (
            <option key={f.path} value={f.path}>
              {f.path}
              {f.prior ? ` [${f.prior}]` : ""}
              {f.summary && f.summary !== f.name
                ? ` · ${f.summary.slice(0, 70)}`
                : ""}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn btn-xs"
          title="Reload the list of files on the base station"
          onClick={() => baseFiles.reload()}
        >
          ↻
        </button>
        <button
          type="button"
          className="btn btn-xs"
          disabled={busy}
          title="Upload a file from this computer instead"
          onClick={() => input.current?.click()}
        >
          From this computer…
        </button>
        <button
          type="button"
          className="btn btn-sm btn-danger"
          disabled={busy || (!picked.length && !applied.length)}
          title={
            applied.length
              ? "Undo the edit files applied to the live map"
              : "Clear the picked files"
          }
          onClick={() => void reset()}
          data-testid={`prep-reset-${mode}`}
        >
          Reset
        </button>
        {mode === "launch" && (
          <span className="muted small">
            applied in order after the base station loads the prior map
          </span>
        )}
      </div>
      {picked.length > 0 && (
        <ol className="prep-list small">
          {picked.map((f, i) => {
            const c = checks[f.name];
            return (
              <li key={f.name} data-testid={`prep-file-${f.name}`}>
                <strong>{f.name}</strong>{" "}
                <span className="muted">{prepSummary(f)}</span>
                {f.summary && f.summary !== f.name && (
                  <div className="muted">{f.summary}</div>
                )}
                <div className="row gap">
                  <button
                    type="button"
                    className="btn btn-xs"
                    disabled={busy}
                    onClick={() => void check(f)}
                  >
                    Check against current map
                  </button>
                  {mode === "live" && (
                    <button
                      type="button"
                      className="btn btn-xs btn-primary"
                      disabled={busy}
                      onClick={() => void apply(f)}
                    >
                      Apply
                    </button>
                  )}
                  {mode === "launch" && i > 0 && (
                    <button
                      type="button"
                      className="btn btn-xs"
                      onClick={() =>
                        setPicked(
                          picked.map((x, j) =>
                            j === i - 1 ? f : j === i ? picked[i - 1] : x,
                          ),
                        )
                      }
                      title="Apply earlier"
                    >
                      ↑
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn-xs"
                    onClick={() =>
                      setPicked(picked.filter((x) => x.name !== f.name))
                    }
                    title="Remove from the list"
                  >
                    ×
                  </button>
                </div>
                {c && (
                  <div
                    className={
                      typeof c === "string" || !c.ok
                        ? "error small"
                        : "ok small"
                    }
                  >
                    {typeof c === "string"
                      ? c
                      : c.ok
                        ? `OK on ${c.map.prior ?? "the map"} (version ${c.map.version ?? "?"})${c.same_as_file ? "" : ": re-resolved, the map changed since the file was made"}${c.prior_ok ? "" : " (made for another prior map)"}`
                        : `Cannot apply: ${c.problems.join("; ")}`}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
      <div
        className="small muted prep-applied"
        data-testid={`prep-applied-${mode}`}
      >
        {status.error
          ? "live map: unavailable"
          : applied.length
            ? `Applied to the live map${status.data?.prior ? ` (${status.data.prior})` : ""}: ` +
              applied
                .map(
                  (a) => `${a.name} @v${a.map_version ?? "?"} ${fmtTime(a.t)}`,
                )
                .join(" → ")
            : "No edit files applied to the live map"}
      </div>
      {msg && <div className="ok small">{msg}</div>}
      {err && <div className="error small">{err}</div>}
    </div>
  );
}
