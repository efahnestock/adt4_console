// The recording bar: on every page, under the top bar. Start a trial (a
// recording on every machine), see that each machine is still recording and
// how long its disk lasts, add flags, stop.
import {useEffect, useRef, useState} from "react";
import {errorText, getJSON, postJSON} from "../lib/api";
import {fmtBytes, fmtRate} from "../lib/fuzzy";
import {answerAge, fmtAge, fmtElapsed, fmtMinutes, LIVE, machineLabel, machineTone, recordingCount, shortestLeft, staleAfter, topicCheckLabel,
  type PreflightMachine, type Trial, type TrialMachine, type TrialPayload} from "../lib/trial";
import {serverNow, useTrial} from "../stores/trial";
import {Dot, ErrorBox, Modal, Spinner} from "./ui";

/** Re-render every second (elapsed time, answer ages). */
function useTick(ms = 1000): number {
  const [t, setT] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setT(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return t;
}

function machineTitle(m: TrialMachine, now: number): string {
  return [
    `${m.name}: ${m.message ?? m.state}`,
    m.dir ? `bag: ${m.dir}` : "",
    m.last_seen ? `last answer ${fmtAge(answerAge(m, now))}` : "no answer yet",
    m.free_bytes != null ? `${fmtBytes(m.free_bytes)} free` : "",
    m.clock_offset_s != null ? `clock ${m.clock_offset_s >= 0 ? "+" : ""}${m.clock_offset_s.toFixed(2)} s vs this console` : "",
    m.topic_check?.missing?.length ? `no publisher for: ${m.topic_check.missing.join(", ")}` : "",
    m.topic_check && !m.topic_check.ok ? `topic check: ${m.topic_check.error ?? "failed"}` : "",
    ...(m.warnings ?? []),
  ].filter(Boolean).join("\n");
}

function MachineChip({m, now, pollS, onRetry}: {m: TrialMachine; now: number; pollS: number; onRetry?: () => void}) {
  const tone = machineTone(m, now, pollS);
  const age = answerAge(m, now);
  const stale = m.state === "recording" && (age === null || age > staleAfter(pollS));
  const topics = m.state === "recording" ? topicCheckLabel(m) : null;
  return (
    <span className={`trial-chip trial-chip-${tone}`} title={machineTitle(m, now)} data-testid={`trial-machine-${m.name}`}>
      <Dot tone={tone} />
      <span className="strong">{m.name}</span>
      <span>{stale ? "no recent answer" : machineLabel(m)}</span>
      {m.state === "recording" && !stale && (
        <span className="muted">
          {fmtBytes(m.bytes)}
          {m.rate_bps != null && ` · ${fmtRate(m.rate_bps)}`}
          {m.minutes_left != null && ` · ${fmtMinutes(m.minutes_left)} left`}
        </span>
      )}
      {(stale || m.state === "unreachable" || m.state === "stop_pending") && <span className="muted">last answer {fmtAge(age)}</span>}
      {m.state === "stopped" && m.bytes != null && <span className="muted">{fmtBytes(m.bytes)}</span>}
      {topics && <span className={topics.bad ? "trial-note" : "muted"} data-testid={`trial-topics-${m.name}`}>{topics.text}</span>}
      {(m.warnings ?? []).map(w => (
        <span key={w} className="trial-note">{w.replace(/^(disk|bag): /, "")}</span>
      ))}
      {(m.state === "failed" || m.state === "lost") && m.message && <span className="trial-note">{m.message}</span>}
      {onRetry && (m.state === "failed" || m.state === "lost") && (
        <button className="btn btn-sm" onClick={onRetry}>Retry</button>
      )}
    </span>
  );
}

function FlagBox({trial}: {trial: Trial}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement>(null);
  const submit = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await postJSON<{event: {t: number; elapsed_s: number}}>("api/trials/flag", {text: t});
      setSaved(`flag saved at ${fmtElapsed(r.event.elapsed_s)}`);
      setText("");
    } catch (e) {
      setError(`FLAG NOT SAVED: ${errorText(e)}`); // the text stays in the box
    } finally {
      setBusy(false);
      ref.current?.focus();
    }
  };
  return (
    <form className="trial-flag" onSubmit={e => { e.preventDefault(); void submit(); }}>
      <input ref={ref} className="input input-sm" value={text} maxLength={2000} placeholder="Flag: what just happened? (Enter saves)"
        onChange={e => { setText(e.target.value); setSaved(null); }} aria-label="Flag text" data-testid="trial-flag-input" />
      <button className="btn btn-sm" type="submit" disabled={busy || !text.trim()} data-testid="trial-flag-save">Flag</button>
      <span className={`small ${error ? "tone-err" : "muted"}`} role="status" data-testid="trial-flag-status">
        {error ?? saved ?? `${trial.flags} flag${trial.flags === 1 ? "" : "s"}`}
      </span>
    </form>
  );
}

function StartDialog({onClose}: {onClose: () => void}) {
  const [desc, setDesc] = useState("");
  const [pre, setPre] = useState<PreflightMachine[] | null>(null);
  const [preError, setPreError] = useState<unknown>(null);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    getJSON<{machines: PreflightMachine[]}>("api/trials/preflight")
      .then(r => {
        if (!live) return;
        setPre(r.machines);
        setPicked(Object.fromEntries(r.machines.map(m => [m.name, m.ready])));
      })
      .catch(e => live && setPreError(e));
    return () => { live = false; };
  }, []);
  const names = pre?.filter(m => picked[m.name]).map(m => m.name) ?? [];
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      useTrial.getState().set(await postJSON<TrialPayload>("api/trials/start", {description: desc.trim(), machines: names}));
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal title="Start recording" onClose={busy ? undefined : onClose} wide
      footer={<>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn btn-danger" onClick={() => void start()} disabled={busy || !desc.trim() || names.length === 0} data-testid="trial-start-confirm">
          {busy ? "Starting ..." : `Start recording on ${names.length} machine${names.length === 1 ? "" : "s"}`}
        </button>
      </>}>
      <label className="field">
        <span className="field-label">Mission description</span>
        <input className="input" autoFocus value={desc} maxLength={500} placeholder="e.g. two spots search building 31, door closed"
          onChange={e => setDesc(e.target.value)} onKeyDown={e => e.key === "Enter" && desc.trim() && names.length > 0 && !busy && void start()} data-testid="trial-description" />
        <span className="field-hint">Goes into the trial's name and into every machine's trial.json.</span>
      </label>
      {preError ? <ErrorBox error={preError} compact /> : !pre ? <Spinner label="Asking every machine for its disk and recorder" /> : (
        <table className="table trial-pre">
          <thead><tr><th></th><th>Machine</th><th>Free disk</th><th>Lasts about</th><th></th></tr></thead>
          <tbody>
            {pre.map(m => (
              <tr key={m.name} className={m.ready ? "" : "muted"}>
                <td><input type="checkbox" checked={!!picked[m.name]} disabled={!m.ready} onChange={e => setPicked(p => ({...p, [m.name]: e.target.checked}))} aria-label={`Record on ${m.name}`} /></td>
                <td className="strong">{m.name}</td>
                <td>{m.free_bytes != null ? fmtBytes(m.free_bytes) : "?"}</td>
                <td title={m.rate_bps ? `at ${fmtRate(m.rate_bps)}, the rate of this machine's last trial` : "no trial recorded on this machine yet: the rate is not known"}>
                  {m.minutes != null ? fmtMinutes(m.minutes) : "unknown until its first trial"}
                </td>
                <td className={m.ready ? "muted small" : "tone-err small"}>{[m.error, ...m.notes].filter(Boolean).join("; ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error != null && <ErrorBox error={error} compact />}
    </Modal>
  );
}

function StopDialog({trial, onClose}: {trial: Trial; onClose: () => void}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const stop = async () => {
    setBusy(true);
    try {
      useTrial.getState().set(await postJSON<TrialPayload>("api/trials/stop", {}));
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  const n = Object.values(trial.machines).filter(m => LIVE.includes(m.state)).length;
  return (
    <Modal title="Stop recording?" onClose={busy ? undefined : onClose}
      footer={<>
        <button className="btn" onClick={onClose} disabled={busy}>Keep recording</button>
        <button className="btn btn-danger" onClick={() => void stop()} disabled={busy} data-testid="trial-stop-confirm">{busy ? "Stopping ..." : "Stop recording"}</button>
      </>}>
      <p>Closes the bag on {n} machine{n === 1 ? "" : "s"} and ends <span className="mono">{trial.trial_id}</span>. The robots' stacks keep running.</p>
      <p className="muted small">A machine that cannot be reached keeps recording; the stop is retried until it answers.</p>
      {error != null && <ErrorBox error={error} compact />}
    </Modal>
  );
}

export function TrialBar() {
  const payload = useTrial(s => s.payload);
  const receivedAt = useTrial(s => s.receivedAt);
  const clientNow = useTick();
  const [dialog, setDialog] = useState<"start" | "stop" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  useEffect(() => void useTrial.getState().load(), []);
  if (!payload) return null; // a server without trials: no bar
  const now = serverNow(payload, receivedAt, clientNow);
  const t = payload.active;
  const act = async (path: string, body?: unknown) => {
    setActionError(null);
    try {
      useTrial.getState().set(await postJSON<TrialPayload>(path, body));
    } catch (e) {
      setActionError(errorText(e));
    }
  };

  if (!t) {
    const last = payload.last;
    const bad = last ? Object.values(last.machines).filter(m => m.state !== "stopped" && m.state !== "skipped") : [];
    const warned = last ? Object.values(last.machines).filter(m => m.warnings?.length) : [];
    return (
      <div className="trial-bar" data-testid="trial-bar">
        <span className="trial-state"><Dot tone="off" /> Not recording</span>
        <button className="btn btn-sm btn-danger" onClick={() => setDialog("start")} data-testid="trial-start">Start recording</button>
        {last && (
          <span className={`small ${last.state === "failed" || bad.length ? "tone-err" : warned.length ? "tone-warn" : "muted"}`} title={last.console_dir}>
            Last: <span className="mono">{last.trial_id}</span>{" "}
            {last.state === "failed" ? "did not start on any machine" : `${fmtElapsed((last.stopped ?? last.started) - last.started)}, ${last.flags} flag${last.flags === 1 ? "" : "s"}`}
            {bad.length > 0 && `; not confirmed stopped: ${bad.map(m => m.name).join(", ")}`}
            {bad.length === 0 && warned.length > 0 && `; warnings on ${warned.map(m => m.name).join(", ")}`}
            {last.note ? `; ${last.note}` : ""}
          </span>
        )}
        {last && (bad.length > 0 || warned.length > 0 || last.state === "failed") && (
          <span className="trial-machines">{Object.values(last.machines).map(m => <MachineChip key={m.name} m={m} now={now} pollS={payload.poll_s} />)}</span>
        )}
        {dialog === "start" && <StartDialog onClose={() => setDialog(null)} />}
      </div>
    );
  }

  const count = recordingCount(t, now, payload.poll_s);
  const stateAge = (clientNow - receivedAt) / 1000;
  const staleState = stateAge > staleAfter(payload.poll_s);
  const allOk = count.ok === count.of && !staleState;
  const left = shortestLeft(t);
  const stopping = t.state === "stopping";
  const stuck = Object.values(t.machines).filter(m => m.state === "stop_failed" || m.state === "stop_pending");
  return (
    <div className={`trial-bar trial-bar-on ${allOk ? "" : "trial-bar-warn"}`} data-testid="trial-bar" role="status">
      <div className="trial-row">
        <span className="trial-state" data-testid="trial-state">
          <span className={`rec-dot ${stopping ? "rec-dot-off" : ""}`} aria-hidden />
          {t.state === "starting" ? "STARTING" : stopping ? "STOPPING" : "REC"} {fmtElapsed(now - t.started)}
        </span>
        <span className={`trial-count ${allOk ? "" : "tone-err"}`} data-testid="trial-count">{count.ok} of {count.of} recording</span>
        <span className="trial-desc" title={t.trial_id}>{t.description}</span>
        {left && <span className={left.minutes < 10 ? "tone-err small" : "muted small"} title="The machine whose disk fills first, at its current rate">disk: {fmtMinutes(left.minutes)} left ({left.name})</span>}
        <span className="grow" />
        {!stopping && <FlagBox trial={t} />}
        {!stopping && <button className="btn btn-sm" onClick={() => setDialog("stop")} data-testid="trial-stop">Stop recording</button>}
        {stopping && stuck.some(m => m.state === "stop_failed") && <button className="btn btn-sm btn-danger" onClick={() => void act("api/trials/stop", {force: true})}>Stop harder</button>}
        {stopping && stuck.length > 0 && <button className="btn btn-sm" onClick={() => void act("api/trials/close")} title="End the trial here; the machines listed keep their state and are named in the trial">Close trial anyway</button>}
      </div>
      <div className="trial-machines">
        {Object.values(t.machines).map(m => (
          <MachineChip key={m.name} m={m} now={now} pollS={payload.poll_s} onRetry={stopping ? undefined : () => void act(`api/trials/machines/${encodeURIComponent(m.name)}/start`)} />
        ))}
        {staleState && <span className="trial-note">this page has had no update for {Math.round(stateAge)} s: the states shown may be old</span>}
        {t.log_error && <span className="trial-note">EVENT LOG NOT WRITING: {t.log_error}</span>}
        {actionError && <span className="trial-note">{actionError}</span>}
      </div>
      {dialog === "stop" && <StopDialog trial={t} onClose={() => setDialog(null)} />}
    </div>
  );
}
