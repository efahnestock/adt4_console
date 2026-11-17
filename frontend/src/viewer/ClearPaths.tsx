// "Clear paths": hide plan / executor-path drawings on the map, in this console
// only (stores/clearedPaths.ts). A new plan or path draws again.
// "Clear map" also hides the trajectories drawn so far and stale robots
// (stores/mapCleared.ts); "Show all" brings those back.
import {useEffect, useState} from "react";
import {usePlanPreview} from "../stores/planPreview";
import {useRobots} from "../stores/robots";
import {useViewer} from "../stores/viewer";
import {hasDrawn, planCleared, pathsCleared, useClearedPaths} from "../stores/clearedPaths";
import {mapClearable, useMapCleared} from "../stores/mapCleared";

const TITLE = "Hide every robot's plan and path drawings on the map (console only: nothing is sent to the robots or the planner). A new plan or path is drawn again.";
const MAP_TITLE =
  "Clear the map: hide every plan and path, the robot trajectories drawn so far (trails start again from here) and robots that stopped reporting (shown again when they report). Console only: nothing is sent to the robots, the planner or the map.";

/** Can "Clear map" hide anything beyond plans and paths? Re-checked as robots go stale. */
function useMapClearable(): boolean {
  useViewer(s => s.sources);
  useViewer(s => s.showTrajectory);
  useRobots(s => s.stateAt);
  const c = useMapCleared();
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick(n => n + 1), 2000);
    return () => clearInterval(t);
  }, []);
  return mapClearable(Date.now(), c);
}

function clearMap() {
  useClearedPaths.getState().clearAll();
  useMapCleared.getState().clear();
}

/** Robots with a plan or path drawn right now. */
function useDrawnRobots(): string[] {
  const plan = useRobots(s => s.plan);
  const paths = useRobots(s => s.paths);
  const plans = useClearedPaths(s => s.plans);
  const cpaths = useClearedPaths(s => s.paths);
  const robots = [...new Set([...Object.keys(plan), ...Object.keys(paths)])];
  return robots.filter(r => hasDrawn(r, {plan, paths}, {plans, paths: cpaths}));
}

/** All robots: the viewer toolbar's button ("Clear map": paths, trajectories so far, stale robots). */
export function ClearPathsButton({compact = false}: {compact?: boolean}) {
  const drawn = useDrawnRobots();
  const preview = usePlanPreview(s => s.preview);
  const more = useMapClearable();
  const none = drawn.length === 0 && !preview && !more;
  return (
    <button className="btn btn-sm panel-btn" disabled={none} onClick={clearMap} title={none ? "Nothing to clear" : MAP_TITLE} aria-label="Clear map" data-testid="clear-paths">
      {compact ? "Clear" : "Clear map"}
    </button>
  );
}

/** Display panel row: clear the map, and show its trajectories and stale robots again. */
export function ClearMapRow() {
  const drawn = useDrawnRobots();
  const preview = usePlanPreview(s => s.preview);
  const more = useMapClearable();
  const traj = useMapCleared(s => s.traj);
  const robots = useMapCleared(s => s.robots);
  const hidden = Object.keys(traj).length > 0 || Object.keys(robots).length > 0;
  return (
    <div className="layout-row">
      <span className="muted small">Trajectories & stale robots</span>
      <span className="row gap-sm">
        <button className="btn btn-xs" disabled={!drawn.length && !preview && !more} onClick={clearMap} title={MAP_TITLE} data-testid="clear-map">
          Clear map
        </button>
        <button className="btn btn-xs" disabled={!hidden} onClick={() => useMapCleared.getState().showAll()} title="Draw the full trajectories and every robot again" data-testid="show-map">
          Show all
        </button>
      </span>
    </div>
  );
}

/** Display panel row: clear all, and bring cleared ones back. */
export function ClearPathsRow() {
  const drawn = useDrawnRobots();
  const preview = usePlanPreview(s => s.preview);
  const plans = useClearedPaths(s => s.plans);
  const cpaths = useClearedPaths(s => s.paths);
  const {clearAll, restore} = useClearedPaths.getState();
  const plan = useRobots(s => s.plan);
  const paths = useRobots(s => s.paths);
  // cleared drawings that would come back (the robot still has them)
  const hidden = [...new Set([...Object.keys(plans), ...Object.keys(cpaths)])].filter(
    r => planCleared({plans}, r, plan[r]) || pathsCleared({paths: cpaths}, r, paths[r]),
  );
  return (
    <div className="layout-row">
      <span className="muted small">Plans & paths on the map</span>
      <span className="row gap-sm">
        <button className="btn btn-xs" disabled={!drawn.length && !preview} onClick={clearAll} title={TITLE} data-testid="clear-paths-all">
          Clear all
        </button>
        <button className="btn btn-xs" disabled={!hidden.length} onClick={() => hidden.forEach(restore)} title="Draw the cleared plans and paths again" data-testid="restore-paths">
          Show cleared{hidden.length ? ` (${hidden.length})` : ""}
        </button>
      </span>
    </div>
  );
}

/** One robot (Plan tab): clear its drawing, or show it again. */
export function ClearRobotPaths({robot}: {robot: string}) {
  const plan = useRobots(s => s.plan[robot]);
  const paths = useRobots(s => s.paths[robot]);
  const plans = useClearedPaths(s => s.plans);
  const cpaths = useClearedPaths(s => s.paths);
  const {clear, restore} = useClearedPaths.getState();
  const isHidden = planCleared({plans}, robot, plan) || pathsCleared({paths: cpaths}, robot, paths);
  const drawn = hasDrawn(robot, {plan: plan ? {[robot]: plan} : {}, paths: paths ? {[robot]: paths} : {}}, {plans, paths: cpaths});
  if (!drawn && !isHidden) return null;
  return drawn ? (
    <button className="btn btn-sm" onClick={() => clear(robot)} title={`Hide ${robot}'s plan and path on the map (console only). Its next plan is drawn again.`} data-testid={`clear-paths-${robot}`}>
      Clear path
    </button>
  ) : (
    <button className="btn btn-sm" onClick={() => restore(robot)} title={`Draw ${robot}'s cleared plan and path again`} data-testid={`restore-paths-${robot}`}>
      Show path
    </button>
  );
}
