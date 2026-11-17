// Robot status card (Robot view): tap a robot in the map, its name tag or its
// legend dot. Says whether the robot is ready to map / ready to localize, what
// is broken first, hydra_multi's loop closures and last optimization time, and
// every check behind it (backend adt4_console/readiness.py). The ring under
// each robot (Scene.tsx ReadinessHalo) uses the same verdict.
import {useEffect, useState} from "react";
import "./robotStatus.css";
import {useRobots} from "../stores/robots";
import {useViewer} from "../stores/viewer";
import {useRobotStatus} from "../stores/robotStatus";
import {useRobotColorOf} from "../lib/palette";
import {fmtAge, hydraCountText, hydraOptText, readinessPhase, ringLabel, ringTone, type Phase, type RingTone} from "../lib/readiness";
import type {Readiness, ReadinessItem, ReadinessList} from "../types";

/** Re-render every `ms` (ages tick; a robot that stops reporting goes grey). */
export function useTick(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export function usePriorLoaded(): boolean {
  return useViewer(s => s.order.some(id => id.startsWith("file:prior:") && !!s.sources[id]));
}

export interface RobotReadiness {
  r: Readiness | undefined;
  phase: Phase;
  tone: RingTone;
  /** seconds since the robot's last state (null: never) */
  staleS: number | null;
}

/** A robot's readiness, the phase that matters and the ring tone; `now` from
 *  :func:`useTick`. */
export function useRobotReadiness(robot: string, now: number): RobotReadiness {
  const r = useRobots(s => s.state[robot]?.readiness);
  const at = useRobots(s => s.stateAt[robot]);
  const override = useRobotStatus(s => s.phase);
  const prior = usePriorLoaded();
  const phase = readinessPhase(prior, override);
  const staleS = at ? Math.max(0, (now - at) / 1000) : null;
  return {r, phase, tone: ringTone(r, phase, staleS), staleS};
}

const STATE_WORD = {ok: "OK", warn: "WARN", fail: "FAIL", unknown: "?"} as const;

function Item({it, since}: {it: ReadinessItem; since: number}) {
  return (
    <li className={`rs-item rs-${it.state}${it.info ? " rs-info" : ""}`} data-testid={`rs-item-${it.key}`}>
      <span className={`rs-badge rs-${it.state}`} aria-label={it.state}>
        {STATE_WORD[it.state]}
      </span>
      <span className="rs-item-body">
        <span className="rs-item-name">
          {it.name}
          {it.info ? <span className="rs-tag">info only</span> : !it.blocking && it.state !== "ok" ? <span className="rs-tag">acceptable</span> : null}
        </span>
        <span className="rs-item-reason">{it.reason}</span>
        <span className="rs-item-src">
          {it.source}
          {it.age_s !== null ? ` · ${fmtAge(it.age_s + since)} old` : ""}
        </span>
      </span>
    </li>
  );
}

function Checklist({title, list, since, current}: {title: string; list: ReadinessList; since: number; current: boolean}) {
  const main = list.items.filter(i => !i.info);
  const info = list.items.filter(i => i.info);
  const verdict = list.state === "ok" ? "ready" : list.state === "fail" ? "blocked" : list.state === "unknown" ? "no data" : "not yet";
  return (
    <section className={`rs-list${current ? " rs-current" : ""}`} aria-label={title}>
      <h4>
        {title}
        <span className={`rs-verdict rs-${list.state}`}>{verdict}</span>
        {current ? <span className="rs-tag">current phase</span> : null}
      </h4>
      <ul>
        {main.map(it => (
          <Item key={it.key} it={it} since={since} />
        ))}
      </ul>
      {info.length > 0 && (
        <details className="rs-more">
          <summary>Informational ({info.length}): shown, never block</summary>
          <ul>
            {info.map(it => (
              <Item key={it.key} it={it} since={since} />
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/** The card's content for one robot (no positioning: tests render it alone). */
export function RobotStatusBody({robot, onClose}: {robot: string; onClose?: () => void}) {
  const now = useTick(1000);
  const {r, phase, tone, staleS} = useRobotReadiness(robot, now);
  const override = useRobotStatus(s => s.phase);
  const setPhase = useRobotStatus(s => s.setPhase);
  const colorOf = useRobotColorOf();
  const since = staleS ?? 0;
  const lists: [Phase, string, ReadinessList | undefined][] = [
    ["map", "Ready to map", r?.map],
    ["localize", "Ready to localize", r?.localize],
  ];
  if (phase === "localize") lists.reverse();
  const broken = (r?.broken ?? []).filter(b => phase === "localize" || b.list === "map");
  return (
    <>
      <div className="rs-head">
        <span className="swatch" style={{background: colorOf(robot)}} />
        <strong>{robot}</strong>
        <span className={`rs-pill rs-tone-${tone}`} data-testid={`rs-verdict-${robot}`}>
          {ringLabel(tone, phase)}
        </span>
        {onClose && (
          <button type="button" className="btn rs-close" aria-label="Close robot status" onClick={onClose}>
            ×
          </button>
        )}
      </div>
      {staleS === null ? (
        <p className="rs-stale">No state received from {robot} yet.</p>
      ) : tone === "none" && r ? (
        <p className="rs-stale">No state from {robot} for {fmtAge(staleS)}: showing its last report.</p>
      ) : null}
      {r && (
        <>
          <div className="rs-broken" data-testid={`rs-broken-${robot}`}>
            {broken.length ? (
              <>
                <span className="rs-broken-title">{broken[0].state === "fail" ? "Broken first" : "Waiting on"}</span>
                <ol>
                  {broken.slice(0, 3).map(b => (
                    <li key={`${b.list}-${b.key}`} className={`rs-${b.state}`}>
                      {b.reason}
                    </li>
                  ))}
                </ol>
              </>
            ) : (
              <span className="rs-ok-line">Nothing blocking {phase === "map" ? "mapping" : "mapping or localizing"}.</span>
            )}
          </div>
          <dl className="rs-hydra">
            <dt>hydra_multi loop closures</dt>
            <dd data-testid={`rs-hydra-${robot}`}>{hydraCountText(r, since)}</dd>
            <dt>Last optimization</dt>
            <dd data-testid={`rs-opt-${robot}`}>{hydraOptText(r, since)}</dd>
          </dl>
          {lists.map(([key, title, list]) =>
            list ? <Checklist key={key} title={title} list={list} since={since} current={key === phase} /> : null,
          )}
        </>
      )}
      <div className="rs-phase" role="group" aria-label="What the rings judge">
        <span className="muted">Rings judge:</span>
        {(
          [
            [null, "Auto"],
            ["map", "Map"],
            ["localize", "Localize"],
          ] as [Phase | null, string][]
        ).map(([p, label]) => (
          <button key={label} type="button" className={`btn rs-seg${override === p ? " active" : ""}`} aria-pressed={override === p} onClick={() => setPhase(p)}>
            {label}
          </button>
        ))}
      </div>
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
  const {tone, phase, r} = useRobotReadiness(robot, now);
  const toggle = useRobotStatus(s => s.toggle);
  const label = `${robot}: ${ringLabel(tone, phase)}${r && tone !== "ok" && tone !== "none" && r.broken[0] ? ` (${r.broken[0].reason})` : ""}. Open status`;
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
