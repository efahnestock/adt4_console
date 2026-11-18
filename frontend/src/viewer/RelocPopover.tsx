// The robot legend's relocalization chip, clicked: the evidence behind the
// verdict in one place — hydra_multi's accepted prior-map loop closures (the
// relocalized rule: at least RELOC_MIN_HYDRA_LCS), fleetlink's ROMAN counter and
// the robot's roman_lc row, and hydra_multi's last optimization (how long ago,
// ticking, and how long it took; from its status note).  Meridian RPGO sessions
// (no hydra_multi) show meridian_rpgo's verdict and inlier counts instead.
import {useEffect, useState} from "react";
import {useRobots} from "../stores/robots";
import {hydraMatches, hydraOptimization, meridianStatus, RELOC_MIN_HYDRA_LCS, type RelocContext} from "../lib/frameAlign";
import {hydraCountText, hydraOptText} from "../lib/readiness";

export function fmtAgo(s: number): string {
  if (s < 90) return `${s.toFixed(0)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

export function fmtTook(s: number): string {
  return s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(2)} s`;
}

export function RelocPopover({robot, ctx, onClose}: {robot: string; ctx: RelocContext; onClose: () => void}) {
  const reloc = useRobots(s => s.state[robot]?.reloc ?? s.link[robot]?.reloc);
  const at = useRobots(s => s.stateAt[robot]);
  // the console remembers hydra_multi's last count / optimization while its
  // row says only "time since last spin" (backend readiness.py)
  const ready = useRobots(s => s.state[robot]?.readiness);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  const hm = hydraMatches(ctx.hydraMulti);
  const opt = hydraOptimization(ctx.hydraMulti);
  const since = at ? Math.max(0, (now - at) / 1000) : 0;
  const need = RELOC_MIN_HYDRA_LCS;
  const hydraText = hm
    ? `${hm.accepted} accepted / ${hm.total} total`
    : ready?.hydra_multi?.accepted != null
      ? hydraCountText(ready, since)
      : ctx.hydraMulti
      ? `no count (${ctx.hydraMulti.status ?? "?"}${ctx.hydraMulti.notes ? `: ${ctx.hydraMulti.notes}` : ""})`
      : ctx.hydraMulti === null
        ? "not in the robot's status table"
        : "no status table from the robot";
  // a Meridian RPGO session: meridian_rpgo in the table, hydra_multi not
  const meridianOnly = !!ctx.meridian && !ctx.hydraMulti;
  const mr = meridianStatus(ctx.meridian);
  const meridianText = mr
    ? `${mr.state.replace("_", " ")} · ${mr.inliers} / ${mr.candidates} loop closures inliers`
    : ctx.meridian
      ? `no inlier count (${ctx.meridian.status ?? "?"}${ctx.meridian.notes ? `: ${ctx.meridian.notes}` : ""})`
      : "";
  const romanText = reloc ? `${reloc.prior_lcs} prior-map matches seen by fleetlink` : "no ROMAN counter from fleetlink";
  const optText = opt
    ? null
    : ready?.hydra_multi?.took_s != null
      ? hydraOptText(ready, since)
      : /no optimization yet/i.test(ctx.hydraMulti?.notes ?? "")
      ? "none yet"
      : "not reported (hydra_multi without optimization timing)";
  return (
    <div
      className="reloc-pop"
      role="dialog"
      aria-label={`${robot} localization`}
      data-testid={`reloc-pop-${robot}`}
      onClick={e => e.stopPropagation()}
      onKeyDown={e => e.stopPropagation()}
    >
      <div className="reloc-pop-head">
        <strong>{robot} localization</strong>
        <button type="button" className="btn btn-xs" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>
      <dl className="reloc-pop-grid">
        {meridianOnly ? (
          <>
            <dt>Meridian RPGO</dt>
            <dd data-testid={`reloc-pop-meridian-${robot}`}>
              {meridianText}
              <div className="muted">meridian_rpgo {ctx.meridian?.status ?? "?"} · relocalized by its own min_num_inliers</div>
            </dd>
          </>
        ) : (
          <>
            <dt>Hydra loop closures</dt>
            <dd data-testid={`reloc-pop-hydra-${robot}`}>
              {hydraText}
              <div className="muted">relocalized at {need} accepted</div>
            </dd>
          </>
        )}
        <dt>ROMAN loop closures</dt>
        <dd data-testid={`reloc-pop-roman-${robot}`}>
          {romanText}
          {ctx.roman ? (
            <div className="muted">
              roman_lc {ctx.roman.status ?? "?"}
              {ctx.roman.notes ? ` — ${ctx.roman.notes}` : ""}
            </div>
          ) : null}
        </dd>
        {meridianOnly ? null : (
          <>
            <dt>Last Hydra optimization</dt>
            <dd data-testid={`reloc-pop-opt-${robot}`}>
              {opt ? (
                // Hydra main's note says only how long it took (no "s ago")
                opt.agoS !== null ? (
                  <>
                    {fmtAgo(opt.agoS + since)} · took {fmtTook(opt.tookS)}
                  </>
                ) : (
                  <>took {fmtTook(opt.tookS)}</>
                )
              ) : (
                optText
              )}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}
