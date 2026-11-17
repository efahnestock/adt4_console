// Robot readiness for the Robot view: the ring under each robot and the
// status card (viewer/RobotStatusCard.tsx). The console backend computes the
// two checklists ("Ready to map", "Ready to localize": adt4_console/readiness.py)
// and sends them with each robot state; this decides which one matters now and
// how the ring looks.
import type {Readiness, ReadyState} from "../types";

/** No robot state for this long: the ring goes grey, the card says so. */
export const STATE_STALE_S = 10;

export type Phase = "map" | "localize";
export type RingTone = "ok" | "warn" | "fail" | "none";

/** Localize when a prior map is registered with the console (a relocalize
 *  session), otherwise map; the operator can pick one in the card. hydra_multi
 *  alone does not decide: multi-robot mapping runs it without a prior map. */
export function readinessPhase(priorLoaded: boolean, override: Phase | null = null): Phase {
  return override ?? (priorLoaded ? "localize" : "map");
}

const RANK: Record<ReadyState, number> = {ok: 0, unknown: 1, warn: 1, fail: 2};

function tone(st: ReadyState): RingTone {
  return st === "ok" ? "ok" : st === "fail" ? "fail" : "warn";
}

/** Green: ready for the phase; amber: partly; red: blocked; grey: no (fresh) data.
 *  Localizing needs mapping too (hydra_multi matches what Hydra builds). */
export function ringTone(r: Readiness | null | undefined, phase: Phase, staleS: number | null): RingTone {
  if (!r || staleS === null || staleS > STATE_STALE_S) return "none";
  if (!r.map.items.length && !r.localize.items.length) return "none";
  const lists = phase === "map" ? [r.map.state] : [r.map.state, r.localize.state];
  const worst = lists.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), "ok" as ReadyState);
  return tone(worst);
}

export function ringLabel(t: RingTone, phase: Phase): string {
  const what = phase === "map" ? "map" : "localize";
  if (t === "ok") return `Ready to ${what}`;
  if (t === "warn") return `Not yet ready to ${what}`;
  if (t === "fail") return `Blocked: cannot ${what}`;
  return "No status from the robot";
}

/** Ring colours on the 3D background (bright on dark, deeper on light; the
 *  canvas background follows the theme). */
export function ringColor(t: RingTone, lightBg: boolean): string {
  const dark = {ok: "#22c55e", warn: "#f5a524", fail: "#ef4444", none: "#8a96a3"};
  const light = {ok: "#15803d", warn: "#b45309", fail: "#c81e1e", none: "#6b7785"};
  return (lightBg ? light : dark)[t];
}

export function fmtAge(s: number): string {
  if (s < 90) return `${Math.max(0, s).toFixed(0)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

export function fmtTook(s: number): string {
  return s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(2)} s`;
}

/** "7 / 9 accepted (relocalized at 5)" plus, when hydra_multi's row no longer
 *  says, how long ago it was reported. `since` = seconds since the state arrived. */
export function hydraCountText(r: Readiness | null | undefined, since = 0): string {
  const h = r?.hydra_multi;
  if (!h) return "no status from the robot";
  if (h.accepted === null) {
    if (!h.in_table) return "hydra_multi is not in the robot's status table";
    return `no count yet (hydra_multi ${h.status ?? "?"}${h.note ? `: ${h.note}` : ""})`;
  }
  const base = `${h.accepted} / ${h.total} accepted (relocalized at ${h.needed})`;
  return h.count_stale && h.count_age_s !== null ? `${base} · last reported ${fmtAge(h.count_age_s + since)} ago` : base;
}

/** "took 412 ms" (+ "12 s ago" when the note says, + "last reported 20 s ago"
 *  when hydra_multi's row no longer says). */
export function hydraOptText(r: Readiness | null | undefined, since = 0): string {
  const h = r?.hydra_multi;
  if (!h || h.took_s === null) {
    if (h?.note && /no optimization yet/i.test(h.note)) return "none yet";
    return h?.in_table ? "not reported yet" : "no hydra_multi status";
  }
  const parts = [`took ${fmtTook(h.took_s)}`];
  if (h.ago_s !== null) parts.push(`${fmtAge(h.ago_s + (h.opt_age_s ?? 0) + since)} ago`);
  else if (h.opt_stale && h.opt_age_s !== null) parts.push(`last reported ${fmtAge(h.opt_age_s + since)} ago`);
  return parts.join(" · ");
}
