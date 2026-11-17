// Robot status card (Robot view): tap a robot in the map, its name tag or its
// legend dot. Says whether the robot is relocalized against the prior map, what
// is in the way first, hydra_multi's loop closures and last optimization time,
// ROMAN's inter-robot loop closures, and every check behind it (backend
// adt4_console/readiness.py). A failing odometry check is one line on top.
// The ring under each robot (Scene.tsx ReadinessHalo) uses the same verdict.
import {useEffect, useState} from "react";
import "./robotStatus.css";
import {useRobots} from "../stores/robots";
import {useRobotStatus} from "../stores/robotStatus";
import {useRobotColorOf} from "../lib/palette";
import {fmtAge, hydraCountText, hydraOptText, ringLabel, ringTone, STATE_STALE_S, type RingTone} from "../lib/readiness";
import type {Readiness, ReadinessItem} from "../types";

/** Re-render every `ms` (ages tick; a robot that stops reporting turns red). */
export function useTick(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export interface RobotReadiness {
  r: Readiness | undefined;
  tone: RingTone;
  /** seconds since the robot's last state (null: never) */
  staleS: number | null;
}

/** A robot's readiness and ring tone; `now` from :func:`useTick`. */
export function useRobotReadiness(robot: string, now: number): RobotReadiness {
  const r = useRobots(s => s.state[robot]?.readiness);
  const at = useRobots(s => s.stateAt[robot]);
  const staleS = at ? Math.max(0, (now - at) / 1000) : null;
  return {r, tone: ringTone(r, staleS), staleS};
}

const STATE_WORD = {ok: "OK", warn: "WARN", fail: "FAIL", unknown: "?"} as const;

function Item({it, since}: {it: ReadinessItem; since: number}) {
  return (
    <li className={`rs-item rs-${it.state}${it.info ? " rs-info" : ""}`} data-testid={`rs-item-${it.key}`}>
      <span className={`rs-badge rs-${it.state}`} aria-label={it.state}>
        {STATE_WORD[it.state]}
      </span>
      <span className="rs-item-body">
        <span className="rs-item-name">{it.name}</span>
        <span className="rs-item-reason">{it.reason}</span>
        <span className="rs-item-src">
          {it.source}
          {it.age_s !== null ? ` · ${fmtAge(it.age_s + since)} old` : ""}
        </span>
      </span>
    </li>
  );
}

/** The card's content for one robot (no positioning: tests render it alone). */
export function RobotStatusBody({robot, onClose}: {robot: string; onClose?: () => void}) {
  const now = useTick(1000);
  const {r, tone, staleS} = useRobotReadiness(robot, now);
  const colorOf = useRobotColorOf();
  const since = staleS ?? 0;
  const items = r?.localize.items ?? [];
  const main = items.filter(i => !i.info);
  const info = items.filter(i => i.info);
  const roman = items.find(i => i.key === "roman_lc");
  const odomFail = r?.odometry?.state === "fail" ? r.odometry : null;
  const broken = (r?.broken ?? []).filter(b => b.key !== "odom");
  return (
    <>
      <div className="rs-head">
        <span className="swatch" style={{background: colorOf(robot)}} />
        <strong>{robot}</strong>
        <span className={`rs-pill rs-tone-${tone}`} data-testid={`rs-verdict-${robot}`}>
          {ringLabel(tone, staleS)}
        </span>
        {onClose && (
          <button type="button" className="btn rs-close" aria-label="Close robot status" onClick={onClose}>
            ×
          </button>
        )}
      </div>
      {staleS === null ? (
        <p className="rs-stale">No state received from {robot} yet.</p>
      ) : staleS > STATE_STALE_S ? (
        <p className="rs-stale">No state from {robot} for {fmtAge(staleS)}: showing its last report.</p>
      ) : null}
      {odomFail && (
        <p className="rs-odom" role="alert" data-testid={`rs-odom-${robot}`}>
          {odomFail.reason}
        </p>
      )}
      {r && (
        <>
          <div className="rs-broken" data-testid={`rs-broken-${robot}`}>
            {broken.length ? (
              <>
                <span className="rs-broken-title">{broken[0].state === "fail" ? "Broken first" : "Waiting on"}</span>
                <ol>
                  {broken.slice(0, 3).map(b => (
                    <li key={b.key} className={`rs-${b.state}`}>
                      {b.reason}
                    </li>
                  ))}
                </ol>
              </>
            ) : odomFail ? (
              <span>Relocalization checks pass, but odometry is not live.</span>
            ) : (
              <span className="rs-ok-line">Relocalized: nothing in the way.</span>
            )}
          </div>
          <dl className="rs-hydra">
            <dt>hydra_multi loop closures</dt>
            <dd data-testid={`rs-hydra-${robot}`}>{hydraCountText(r, since)}</dd>
            <dt>Last optimization</dt>
            <dd data-testid={`rs-opt-${robot}`}>{hydraOptText(r, since)}</dd>
            <dt>ROMAN inter-robot LCs</dt>
            <dd data-testid={`rs-roman-${robot}`}>{roman ? roman.reason : "roman_lc is not in the robot's status table"}</dd>
          </dl>
          <section className="rs-list" aria-label="Relocalization checks">
            <h4>Relocalization</h4>
            <ul>
              {main.map(it => (
                <Item key={it.key} it={it} since={since} />
              ))}
            </ul>
          </section>
          {info.length > 0 && (
            <section className="rs-list" aria-label="Information">
              <h4>
                Information <span className="rs-tag">never blocks</span>
              </h4>
              <ul>
                {info.map(it => (
                  <Item key={it.key} it={it} since={since} />
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </>
  );
}

/** The open robot's card, over the map (a bottom sheet on phones). */
export function RobotStatusCard() {
  const robot = useRobotStatus(s => s.open);
  const close = useRobotStatus(s => s.close);
  useEffect(() => {
    if (!robot) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [robot, close]);
  if (!robot) return null;
  return (
    <div className="robot-status-card" role="dialog" aria-label={`${robot} status`} data-testid={`robot-status-${robot}`} onPointerDown={e => e.stopPropagation()}>
      <RobotStatusBody robot={robot} onClose={close} />
    </div>
  );
}

/** Legend / list entry: a dot in the ring's colour that opens the card. */
export function ReadinessDot({robot}: {robot: string}) {
  const now = useTick(2000);
  const {tone, staleS, r} = useRobotReadiness(robot, now);
  const toggle = useRobotStatus(s => s.toggle);
  const why = r && tone !== "ok" && tone !== "none" && r.broken[0] ? ` (${r.broken[0].reason})` : "";
  const label = `${robot}: ${ringLabel(tone, staleS)}${why}. Open status`;
  return (
    <button
      type="button"
      className={`readiness-dot rs-tone-${tone}`}
      data-testid={`readiness-dot-${robot}`}
      aria-label={label}
      title={label}
      onClick={e => {
        e.stopPropagation();
        toggle(robot);
      }}
      // Enter / Space would also focus the robot (the chip's own keys); Escape
      // must still reach the card
      onKeyDown={e => (e.key === "Enter" || e.key === " ") && e.stopPropagation()}
    />
  );
}
