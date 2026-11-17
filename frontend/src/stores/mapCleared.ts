// Operator "clear map" (console only: nothing is sent to a robot, the planner or
// the map). On top of "clear paths" (stores/clearedPaths.ts) it hides:
//  - the trajectories drawn so far: each source's trajectory is remembered as
//    "N points per robot" at the clear, and only the points added after them are
//    drawn (a trajectory only grows, so a robot's trail starts again from where
//    it is);
//  - robots already stale at the clear (no state for STALE_MS): hidden until
//    they report again.
// "Show all" brings everything back. Remembered per browser (localStorage).
import {create} from "zustand";
import type {TrajPath} from "./viewer";
import {useViewer} from "./viewer";
import {useRobots} from "./robots";

const KEY = "adt4.mapCleared";

/** A robot with no state for this long is drawn as stale (Scene's RobotGlyphs). */
export const STALE_MS = 5000;

interface Cleared {
  /** source id -> trajectory robot key -> points hidden (the count at the clear) */
  traj: Record<string, Record<string, number>>;
  /** robot -> its last state time at the clear (hidden until a newer one) */
  robots: Record<string, number>;
}

const EMPTY: Cleared = {traj: {}, robots: {}};

function load(): Cleared {
  try {
    const raw = localStorage.getItem(KEY);
    const d = raw ? (JSON.parse(raw) as Partial<Cleared>) : null;
    const obj = <T,>(v: unknown) => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, T>) : {});
    return {traj: obj<Record<string, number>>(d?.traj), robots: obj<number>(d?.robots)};
  } catch {
    return {...EMPTY};
  }
}

function save(c: Cleared) {
  try {
    localStorage.setItem(KEY, JSON.stringify(c));
  } catch {
    /* private window / blocked storage: the in-memory choice holds this session */
  }
}

/** Points per robot key in a trajectory. */
export function trajCounts(paths: TrajPath[] | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of paths ?? []) out[p.robot] = (out[p.robot] ?? 0) + p.points.length;
  return out;
}

/** ``paths`` without each robot's first ``hidden[robot]`` points (in path order). */
export function trimTrajectory(paths: TrajPath[] | null, hidden: Record<string, number> | undefined): TrajPath[] | null {
  if (!paths || !hidden || !Object.keys(hidden).length) return paths;
  const skip = {...hidden};
  const out: TrajPath[] = [];
  for (const p of paths) {
    const n = skip[p.robot] ?? 0;
    if (n >= p.points.length) {
      skip[p.robot] = n - p.points.length;
      continue;
    }
    skip[p.robot] = 0;
    // keep the last hidden point so the new trail starts where the robot was
    const kept = n > 0 ? p.points.slice(n - 1) : p.points;
    if (kept.length > 1) out.push({robot: p.robot, points: kept});
  }
  return out;
}

/** Is ``robot`` hidden: stale at the clear and no newer state since? */
export function robotCleared(c: Pick<Cleared, "robots">, robot: string, stateAt: number | undefined): boolean {
  const at = c.robots[robot];
  return at !== undefined && (stateAt ?? 0) <= at;
}

/** Anything a clear would hide now: trajectory points or stale robots. */
export function mapClearable(now: number = Date.now(), c: Cleared = useMapCleared.getState()): boolean {
  const v = useViewer.getState();
  for (const id of v.order) {
    const src = v.sources[id];
    if (!src?.enabled || !v.showTrajectory) continue;
    const hidden = c.traj[id] ?? {};
    for (const [k, n] of Object.entries(trajCounts(src.trajectory))) if (n > (hidden[k] ?? 0) + 1) return true;
  }
  const r = useRobots.getState();
  return Object.keys(r.state).some(robot => now - (r.stateAt[robot] ?? 0) > STALE_MS && !robotCleared(c, robot, r.stateAt[robot]));
}

interface MapClearedStore extends Cleared {
  /** hide the trajectories drawn so far and the stale robots */
  clear: () => void;
  /** draw everything again */
  showAll: () => void;
  /** re-read localStorage (tests) */
  reload: () => void;
}

export const useMapCleared = create<MapClearedStore>((set, get) => ({
  ...load(),
  clear: () => {
    const v = useViewer.getState();
    const traj = {...get().traj};
    for (const id of v.order) {
      const src = v.sources[id];
      if (src?.trajectory) traj[id] = trajCounts(src.trajectory);
    }
    const r = useRobots.getState();
    const now = Date.now();
    const robots = {...get().robots};
    for (const robot of Object.keys(r.state)) {
      const at = r.stateAt[robot] ?? 0;
      if (now - at > STALE_MS) robots[robot] = at;
    }
    save({traj, robots});
    set({traj, robots});
  },
  showAll: () => {
    save(EMPTY);
    set({...EMPTY});
  },
  reload: () => set(load()),
}));
