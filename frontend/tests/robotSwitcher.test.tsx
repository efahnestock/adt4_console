import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {OVERLAY_LEFT_W, TIGHT_MAP_W, overlayRightW, visibleMapWidth} from "../src/viewer/paneLayout";
import {RobotFocusPicker} from "../src/viewer/RobotFocusPicker";
import {useFleet} from "../src/stores/fleet";
import {useRobots} from "../src/stores/robots";
import {useViewer} from "../src/stores/viewer";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

describe("the map the toolbars (and robot switcher) can use", () => {
  it("side-by-side panels already shrink the canvas", () => {
    expect(visibleMapWidth(600, false, true, true, 1280)).toBe(600);
  });
  it("tablets <= 900 px: a panel over the map takes its width off", () => {
    // Galaxy Tab S6 portrait (800 css px), side panel open
    expect(visibleMapWidth(742, true, false, true, 800)).toBe(742 - 360);
    expect(visibleMapWidth(742, true, true, false, 800)).toBe(742 - OVERLAY_LEFT_W);
    // a narrow phone-sized window: the panel is 92% of it
    expect(overlayRightW(390)).toBe(359);
    expect(visibleMapWidth(100, true, true, true, 800)).toBe(0);
  });
  it("a Tab S6 portrait with its side panel open stacks the toolbars", () => {
    expect(visibleMapWidth(742, true, false, true, 800)).toBeLessThan(TIGHT_MAP_W);
    expect(visibleMapWidth(742, true, false, false, 800)).toBeGreaterThanOrEqual(TIGHT_MAP_W);
  });
});

describe("the robot switcher with a long roster", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    useFleet.setState({machines: null});
    useViewer.getState().setFocus("all");
  });
  it("compact still lists and switches to every robot", async () => {
    const names = Array.from({length: 14}, (_, i) => `robot${String(i).padStart(2, "0")}`);
    useFleet.setState({machines: {network: "t", machines: names.map(name => ({name, role: "robot", ip: "", user: ""}))}} as never);
    useRobots.setState({state: {}, plan: {}, teleop: {}, link: {}});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(<RobotFocusPicker compact />));
    const btn = host.querySelector<HTMLButtonElement>("[data-testid=focus-picker]")!;
    expect(btn.textContent).toContain("All");
    act(() => btn.click());
    const opts = host.querySelectorAll<HTMLLIElement>(".focus-menu [role=option]");
    expect(opts).toHaveLength(names.length + 1);
    act(() => opts[opts.length - 1].click());
    expect(useViewer.getState().focus).toBe("robot13");
    expect(host.querySelector("[data-testid=focus-picker]")!.textContent).toContain("robot13");
  });
});
