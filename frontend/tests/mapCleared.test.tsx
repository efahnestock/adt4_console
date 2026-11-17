import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {mapClearable, robotCleared, trimTrajectory, useMapCleared} from "../src/stores/mapCleared";
import {useRobots} from "../src/stores/robots";
import {useViewer, type TrajPath} from "../src/stores/viewer";
import {ClearMapRow} from "../src/viewer/ClearPaths";

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const pts = (n: number, x0 = 0): [number, number, number][] => Array.from({length: n}, (_, i) => [x0 + i, 0, 0]);

function withTrajectory(paths: TrajPath[]) {
  useViewer.setState(s => ({
    order: ["live"],
    showTrajectory: true,
    sources: {live: {...(s.sources.live ?? {}), id: "live", enabled: true, trajectory: paths, trajectoryRobots: []} as never},
  }));
}

/** A Map-backed localStorage (the test environment has none). */
function mockStorage() {
  const m = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
  });
}

function render(el: React.ReactElement): HTMLElement {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(el));
  return host;
}

describe("clear map", () => {
  beforeEach(() => {
    mockStorage();
    useMapCleared.getState().showAll();
    useRobots.setState({state: {}, stateAt: {}});
    withTrajectory([]);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("trims each robot's trajectory to what was added after the clear", () => {
    const paths: TrajPath[] = [
      {robot: "a", points: pts(3)},
      {robot: "b", points: pts(4, 100)},
      {robot: "a", points: pts(5, 10)},
    ];
    // a: 3 + 2 hidden -> the second path from its 2nd point (the last hidden one kept)
    const out = trimTrajectory(paths, {a: 5, b: 4})!;
    expect(out).toEqual([{robot: "a", points: pts(4, 11)}]);
    expect(trimTrajectory(paths, undefined)).toBe(paths);
    expect(trimTrajectory(paths, {})).toBe(paths);
  });

  it("hides the trajectory so far and stale robots, and shows them again", () => {
    vi.useFakeTimers();
    vi.setSystemTime(100_000);
    withTrajectory([{robot: "a", points: pts(6)}]);
    useRobots.setState({
      state: {spot1: {pose: {x: 0, y: 0, z: 0}} as never, spot2: {pose: {x: 1, y: 0, z: 0}} as never},
      stateAt: {spot1: 99_000, spot2: 10_000}, // spot2 stale
    });
    expect(mapClearable()).toBe(true);
    useMapCleared.getState().clear();
    const c = useMapCleared.getState();
    expect(trimTrajectory(useViewer.getState().sources.live.trajectory, c.traj.live)).toEqual([]);
    expect(robotCleared(c, "spot2", 10_000)).toBe(true);
    expect(robotCleared(c, "spot1", 99_000)).toBe(false);
    expect(mapClearable()).toBe(false);
    // the robot reports again: drawn; the trajectory grows: only the new part
    expect(robotCleared(c, "spot2", 100_500)).toBe(false);
    withTrajectory([{robot: "a", points: pts(9)}]);
    expect(trimTrajectory(useViewer.getState().sources.live.trajectory, c.traj.live)).toEqual([{robot: "a", points: pts(4, 5)}]);
    // survives a reload (per browser), and Show all forgets it
    useMapCleared.setState({traj: {}, robots: {}});
    useMapCleared.getState().reload();
    expect(useMapCleared.getState().traj.live).toEqual({a: 6});
    useMapCleared.getState().showAll();
    useMapCleared.getState().reload();
    expect(useMapCleared.getState().traj).toEqual({});
    expect(useMapCleared.getState().robots).toEqual({});
  });

  it("the Display row clears and shows again; nothing is sent", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    withTrajectory([{robot: "a", points: pts(6)}]);
    const h = render(<ClearMapRow />);
    const clear = h.querySelector<HTMLButtonElement>('[data-testid="clear-map"]')!;
    const show = h.querySelector<HTMLButtonElement>('[data-testid="show-map"]')!;
    expect(clear.disabled).toBe(false);
    expect(show.disabled).toBe(true);
    act(() => clear.click());
    expect(clear.disabled).toBe(true);
    expect(show.disabled).toBe(false);
    act(() => show.click());
    expect(clear.disabled).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
