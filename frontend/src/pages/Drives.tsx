// USB bag drives on the fleet's machines: what is plugged in where, eject, and
// copying a machine's recorded bags onto its drive (the copy runs on the
// machine itself; the console starts it and follows the job).
import {useEffect, useMemo, useState} from "react";
import {errorText, postJSON, useApi} from "../lib/api";
import {fmtBytes} from "../lib/fuzzy";
import {fetchJob, isActive, useJobs} from "../stores/jobs";
import {Badge, Card, Empty, ErrorBox, Loading, PageHeader, ProgressBar, Spinner, Toggle} from "../components/ui";
import {JobStateBadge} from "../components/Jobs";

export interface Drive {
  name: string;
  label: string;
  uuid: string;
  fstype: string;
  model: string;
  mountpoint: string;
  /** where the machine's user can write: the drive root, or its bags/ folder; "" = read-only */
  write_dir: string;
  mounted: boolean;
  managed: boolean;
  size_bytes: number;
  free_bytes: number;
  /** negotiated USB link speed; 0 = unknown */
  usb_speed_mbps: number;
  slow_link: boolean;
  error: string;
  /** the copy job writing to this drive now, if any */
  copy_job: string | null;
}

export interface DriveMachine {
  name: string;
  role?: string | null;
  online: boolean;
  /** false: the bag-drive helper is not on this machine; null: could not ask */
  installed: boolean | null;
  drives: Drive[];
  error: string;
}

interface DrivesPayload {
  updated: number;
  machines: DriveMachine[];
}

export interface BagRun {
  name: string;
  mtime: number;
  bytes: number;
  /** a bag file changed in the last minute: still being written, not copyable */
  recording?: boolean;
  /** what of this run is on the target drive: "verified" (all there, hashes
   *  checked), "copied" (all there, not hashed), "partial" (an interrupted
   *  copy), "" (nothing) */
  on_drive?: "verified" | "copied" | "partial" | "";
  dest_bytes?: number;
  bags: {name: string; bytes: number; files: number; metadata?: boolean}[];
}

interface BagsPayload {
  machine: string;
  root: string;
  runs: BagRun[];
  note?: string;
}

export function linkLabel(d: Pick<Drive, "usb_speed_mbps" | "slow_link">): string {
  if (!d.usb_speed_mbps) return "link speed unknown";
  if (d.slow_link) return `USB 2.0 (${d.usb_speed_mbps} Mbit/s): slow`;
  return d.usb_speed_mbps >= 1000 ? `${d.usb_speed_mbps / 1000} Gbit/s` : `${d.usb_speed_mbps} Mbit/s`;
}

/** What the run's badge says about its copy on the drive (null: nothing there). */
export function onDriveLabel(r: Pick<BagRun, "on_drive" | "dest_bytes" | "bytes">): {tone: "ok" | "info" | "warn"; text: string; title: string} | null {
  if (r.on_drive === "verified") return {tone: "ok", text: "on drive", title: "Copied, and the SHA-256 of every file matched the robot's copy."};
  if (r.on_drive === "copied") return {tone: "info", text: "on drive, not hashed", title: "All bytes are on the drive but the hashes were not checked: copy again to check them (nothing is rewritten)."};
  if (r.on_drive === "partial") {
    const pct = r.bytes > 0 ? Math.floor((100 * (r.dest_bytes ?? 0)) / r.bytes) : 0;
    return {tone: "warn", text: `partial ${pct}%`, title: "An interrupted copy. Copy again to continue where it stopped."};
  }
  return null;
}

/** A drive bags can be copied to: mounted and writable by the machine's user. */
export const usable = (d: Drive) => d.mounted && Boolean(d.write_dir) && !d.error;

const when = (t: number) => new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});

function DriveRow({machine, drive, onChanged, onMessage}: {machine: string; drive: Drive; onChanged: () => void; onMessage: (m: {ok: boolean; text: string}) => void}) {
  const [busy, setBusy] = useState(false);
  const job = useJobs(s => (drive.copy_job ? s.jobs[drive.copy_job] : undefined));
  const copying = Boolean(drive.copy_job) && (!job || isActive(job));
  const used = drive.size_bytes > 0 ? 100 * (1 - drive.free_bytes / drive.size_bytes) : 0;

  const pause = async () => {
    if (!drive.copy_job) return;
    setBusy(true);
    try {
      await postJSON(`api/jobs/${encodeURIComponent(drive.copy_job)}/cancel`);
      onMessage({ok: true, text: `${machine}: copy interrupted. What was written stays on ${drive.label || drive.name}; start the copy again later to continue. The drive can be ejected now.`});
      onChanged();
    } catch (e) {
      onMessage({ok: false, text: errorText(e)});
    } finally {
      setBusy(false);
    }
  };

  const eject = async () => {
    setBusy(true);
    try {
      const r = await postJSON<{message: string}>("api/drives/eject", {machine, drive: drive.name});
      onMessage({ok: true, text: `${machine}: ${r.message || `${drive.name} ejected`}`});
      onChanged();
    } catch (e) {
      onMessage({ok: false, text: errorText(e)});
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="drive-row" data-testid={`drive-${machine}-${drive.name}`}>
      <div className="drive-main">
        <div className="drive-name">
          <span className="mono">{drive.label || drive.name}</span>
          <Badge tone="off">{drive.fstype || "?"}</Badge>
          <Badge tone={drive.slow_link ? "warn" : drive.usb_speed_mbps ? "ok" : "off"} title="Negotiated USB link speed. A USB 2.0 link means the wrong port or cable.">
            {linkLabel(drive)}
          </Badge>
          {!drive.mounted && <Badge tone="err">not mounted</Badge>}
          {drive.mounted && !drive.write_dir && <Badge tone="warn">read-only</Badge>}
          {copying && job && <JobStateBadge state={job.state} />}
        </div>
        <div className="muted small">
          {fmtBytes(drive.free_bytes)} free of {fmtBytes(drive.size_bytes)}
          {drive.write_dir && <> · writes to <span className="mono">{drive.write_dir}</span></>}
        </div>
        {drive.size_bytes > 0 && !copying && <ProgressBar pct={used} label="used" tone={used > 90 ? "warn" : "info"} />}
        {copying && job && Object.entries(job.progress ?? {}).map(([key, pct]) => (
          <ProgressBar key={key} pct={pct} label={key.endsWith("verify") ? "Checking hashes" : "Copying"} tone={key.endsWith("verify") ? "ok" : "info"} />
        ))}
        {drive.error && <div className="small tone-err">{drive.error}</div>}
      </div>
      {copying && (
        <button className="btn btn-sm btn-danger-outline" disabled={busy} onClick={pause} data-testid="pause-copy"
          title="Stop the copy now and keep what is on the drive. Starting the copy again later continues where it stopped.">
          Interrupt copy
        </button>
      )}
      <button className="btn btn-sm" disabled={busy || copying || !drive.managed} onClick={eject}
        title={copying ? "A copy is writing to this drive: interrupt it or wait" : !drive.managed ? "Mounted by the desktop, not by bag-drive: eject it there" : "Sync, unmount and power the drive down so it is safe to unplug"}>
        {busy ? <Spinner label="Ejecting" /> : "Eject"}
      </button>
    </div>
  );
}

function BagPicker({machine, drives, refresh, remove, onStarted, onMessage}: {machine: string; drives: Drive[]; refresh: number; remove: boolean; onStarted: () => void; onMessage: (m: {ok: boolean; text: string}) => void}) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [target, setTarget] = useState(drives[0]?.name ?? "");
  // asked with the target drive, so each run says what of it is already there
  const bags = useApi<BagsPayload>(`api/drives/bags?machine=${encodeURIComponent(machine)}${target ? `&drive=${encodeURIComponent(target)}` : ""}`);
  const [busy, setBusy] = useState(false);
  const drive = drives.find(d => d.name === target) ?? drives[0];
  useEffect(() => {
    if (!drives.some(d => d.name === target)) setTarget(drives[0]?.name ?? "");
  }, [drives, target]);

  const runs = bags.data?.runs ?? [];
  const ready = runs.filter(r => !r.recording);
  const total = useMemo(() => runs.filter(r => picked.has(r.name)).reduce((n, r) => n + r.bytes, 0), [runs, picked]);
  const job = useJobs(s => (drive?.copy_job ? s.jobs[drive.copy_job] : undefined));
  const copying = Boolean(drive?.copy_job) && (!job || isActive(job));
  // a copy that ended (finished, failed or interrupted) changed what is on the drive
  const jobState = job?.state;
  // the page's Refresh button asks for the bag lists again too
  useEffect(() => {
    if (refresh) bags.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh]);
  useEffect(() => {
    if (jobState && jobState !== "running" && jobState !== "waiting") {
      bags.reload();
      onStarted();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobState]);
  const unfinished = ready.filter(r => r.on_drive !== "verified");
  const resuming = runs.some(r => picked.has(r.name) && r.on_drive === "partial");
  const toCopy = useMemo(() => runs.filter(r => picked.has(r.name)).reduce((n, r) => n + Math.max(0, r.bytes - (r.dest_bytes ?? 0)), 0), [runs, picked]);
  const fits = !drive || toCopy <= drive.free_bytes;

  const toggle = (name: string) =>
    setPicked(old => {
      const next = new Set(old);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const copy = async () => {
    if (!drive) return;
    setBusy(true);
    try {
      const r = await postJSON<{job_id: string}>("api/drives/copy", {machine, drive: drive.name, runs: runs.filter(x => picked.has(x.name)).map(x => x.name), remove_source: remove});
      // not trackJob: it opens the Jobs drawer over this card, and the progress
      // and the Interrupt button are on the drive's row
      useJobs.getState().upsert({id: r.job_id, kind: "drive_copy", title: `Copy bags ${machine} -> ${drive.name}`, state: "running", created: Date.now() / 1000});
      void fetchJob(r.job_id);
      setPicked(new Set());
      onStarted();
    } catch (e) {
      onMessage({ok: false, text: errorText(e)});
    } finally {
      setBusy(false);
    }
  };

  if (bags.loading && !bags.data) return <Loading label={`Listing bags on ${machine}`} />;
  if (bags.error) return <ErrorBox error={bags.error} onRetry={bags.reload} compact />;
  if (runs.length === 0) return <div className="muted small">No recorded bags under <span className="mono">{bags.data?.root}</span>.</div>;

  return (
    <div className="bag-picker">
      <div className="table-wrap">
        <table className="table compact">
          <thead>
            <tr>
              <th />
              <th>Run</th>
              <th>Recorded</th>
              <th className="num">Bag size</th>
            </tr>
          </thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.name} onClick={() => !r.recording && toggle(r.name)} className={r.recording ? "" : "clickable"}>
                <td>
                  <input type="checkbox" checked={picked.has(r.name)} disabled={r.recording} onChange={() => toggle(r.name)} onClick={e => e.stopPropagation()} aria-label={`Select ${r.name}`} />
                </td>
                <td className="mono">
                  {r.name} {r.recording && <Badge tone="warn" title="This bag is still being written. Stop the recording before copying it.">recording</Badge>}
                  {(() => { const l = onDriveLabel(r); return l && <Badge tone={l.tone} title={l.title}>{l.text}</Badge>; })()}
                </td>
                <td className="small muted">{when(r.mtime)}</td>
                <td className="num">{fmtBytes(r.bytes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="bag-actions">
        <button className="btn btn-xs" onClick={() => setPicked(picked.size === ready.length ? new Set() : new Set(ready.map(r => r.name)))}>
          {picked.size === ready.length && ready.length > 0 ? "Select none" : "Select all"}
        </button>
        {unfinished.length > 0 && unfinished.length < ready.length && (
          <button className="btn btn-xs" onClick={() => setPicked(new Set(unfinished.map(r => r.name)))} title="Select every run that is not yet on the drive with its hashes checked">
            Select unfinished
          </button>
        )}
        <button className="btn btn-xs" onClick={bags.reload} disabled={bags.loading}>Refresh bags</button>
        {drives.length > 1 && (
          <select className="input input-xs" value={drive?.name ?? ""} onChange={e => setTarget(e.target.value)} aria-label="Target drive">
            {drives.map(d => <option key={d.name} value={d.name}>{d.label || d.name}</option>)}
          </select>
        )}
        <span className={`small ${fits ? "muted" : "tone-err"}`}>
          {picked.size} selected, {fmtBytes(total)}
          {toCopy < total && `, ${fmtBytes(toCopy)} left to copy`}
          {drive && !fits && ` (only ${fmtBytes(drive.free_bytes)} free)`}
        </span>
        <button className="btn btn-sm btn-primary" disabled={busy || copying || picked.size === 0 || !fits || !drive} onClick={copy}
          title={copying ? "A copy to this drive is already running" : undefined}>
          {busy ? <Spinner label="Starting" /> : `${resuming ? "Resume copy" : remove ? "Move" : "Copy"} to ${drive?.label || drive?.name || "drive"}`}
        </button>
      </div>
    </div>
  );
}

function MachineCard({m, refresh, onChanged, onMessage}: {m: DriveMachine; refresh: number; onChanged: () => void; onMessage: (m: {ok: boolean; text: string}) => void}) {
  const targets = m.drives.filter(usable);
  // on by default: a bag is deleted only after every byte of its copy has been
  // checked against the original, and the job asks first
  const [remove, setRemove] = useState(true);
  return (
    <Card title={<>{m.name} {m.role && <span className="muted small">{m.role}</span>}</>}
      actions={targets.length > 0 && (
        <span data-testid={`remove-after-copy-${m.name}`}>
          <Toggle checked={remove} onChange={setRemove} label={`Delete from ${m.name} after a verified copy`}
            title="After copying, every byte of the copy is checked against the original. The job then asks before deleting anything from this machine. Switch off to keep the bags here." />
        </span>
      )}>
      {m.installed === false ? (
        <div className="muted small">The bag-drive helper is not installed on this machine, so drives do not mount by themselves here.</div>
      ) : m.error ? (
        <div className="small tone-err">{m.error}</div>
      ) : m.drives.length === 0 ? (
        <div className="muted small">No drive plugged in.</div>
      ) : (
        <>
          {m.drives.map(d => <DriveRow key={d.name} machine={m.name} drive={d} onChanged={onChanged} onMessage={onMessage} />)}
          {targets.length > 0 && <BagPicker machine={m.name} drives={targets} refresh={refresh} remove={remove} onStarted={onChanged} onMessage={onMessage} />}
        </>
      )}
    </Card>
  );
}

export function DrivesPage() {
  const drives = useApi<DrivesPayload>("api/drives", 8_000);
  const [msg, setMsg] = useState<{ok: boolean; text: string} | null>(null);
  const [refresh, setRefresh] = useState(0);
  const machines = drives.data?.machines ?? [];
  const online = machines.filter(m => m.online);
  const offline = machines.filter(m => !m.online);

  return (
    <div className="page">
      <PageHeader title="Drives" sub="USB drives plugged into the robots: copy recorded bags onto them, then eject."
        actions={<button className="btn btn-sm" onClick={() => { drives.reload(); setRefresh(n => n + 1); }} disabled={drives.loading} data-testid="drives-refresh">Refresh</button>} />
      {msg && (
        <div className={`banner ${msg.ok ? "banner-ok" : "banner-err"}`} role="status">
          {msg.text}
          <button className="icon-btn" aria-label="Dismiss" onClick={() => setMsg(null)}>×</button>
        </div>
      )}
      {drives.error && !drives.data && <ErrorBox error={drives.error} onRetry={drives.reload} />}
      {!drives.data && drives.loading && <Loading label="Asking the machines for their drives" />}
      {drives.data && online.length === 0 && <Empty title="No machine is online">Drives show up here once a robot is reachable.</Empty>}
      <div className="grid-2">
        {online.map(m => <MachineCard key={m.name} m={m} refresh={refresh} onChanged={drives.reload} onMessage={setMsg} />)}
      </div>
      {offline.length > 0 && <div className="muted small drives-offline">Offline: {offline.map(m => m.name).join(", ")}</div>}
    </div>
  );
}
