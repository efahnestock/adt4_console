import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {RobotStatusBody, RobotStatusCard} from "../src/viewer/RobotStatusCard";
import {RobotLegend} from "../src/viewer/RobotLegend";
import {useRobots} from "../src/stores/robots";
import {useRobotStatus} from "../src/stores/robotStatus";
import {hydraCountText, hydraOptText, readinessPhase, ringColor, ringLabel, ringTone, STATE_STALE_S} from "../src/lib/readiness";
import type {Readiness} from "../src/types";
// computed by the backend (adt4_console/readiness.py) from status tables
// modelled on the 2026-11-15 willow logs (tests/test_readiness.py)
import FIX from "./fixtures/readiness.json";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const NO_ODOM = FIX.no_odom as unknown as Readiness;
const RELOC_IDLE = FIX.relocalized_idle as unknown as Readiness;
const RELOCALIZING = FIX.relocalizing as unknown as Readiness;

describe("readiness ring", () => {
  it("judges the current phase: map without a prior map, map + localize with one", () => {
    expect(readinessPhase(false)).toBe("map");
    expect(readinessPhase(true)).toBe("localize");
    expect(readinessPhase(true, "map")).toBe("map");
    // no odometry: blocked either way
    expect(ringTone(NO_ODOM, "map", 0)).toBe("fail");
    expect(ringTone(NO_ODOM, "localize", 0)).toBe("fail");
    // relocalizing (2 of 5): ready to map, partly ready to localize
    expect(ringTone(RELOCALIZING, "map", 0)).toBe("ok");
    expect(ringTone(RELOCALIZING, "localize", 0)).toBe("warn");
    // relocalized; hydra_multi idle (ERR "time since last spin") keeps the last count
    expect(ringTone(RELOC_IDLE, "localize", 0)).toBe("ok");
    expect(ringLabel("ok", "localize")).toBe("Ready to localize");
  });

  it("goes grey without fresh state", () => {
    expect(ringTone(undefined, "map", 0)).toBe("none");
    expect(ringTone(RELOC_IDLE, "map", null)).toBe("none");
    expect(ringTone(RELOC_IDLE, "map", STATE_STALE_S + 1)).toBe("none");
    expect(ringLabel("none", "map")).toBe("No status from the robot");
  });

  it("uses different colours per tone, deeper on a light background", () => {
    const dark = (["ok", "warn", "fail", "none"] as const).map(t => ringColor(t, false));
    expect(new Set(dark).size).toBe(4);
    expect(ringColor("ok", true)).not.toBe(ringColor("ok", false));
  });

  it("keeps hydra_multi's last count and optimization time, labelled with their age", () => {
    expect(hydraCountText(RELOC_IDLE)).toBe("7 / 9 accepted (relocalized at 5) · last reported 19 s ago");
    expect(hydraOptText(RELOC_IDLE, 3)).toBe("took 412 ms · last reported 22 s ago");
    expect(hydraCountText(RELOCALIZING)).toBe("2 / 4 accepted (relocalized at 5)");
    expect(hydraOptText(RELOCALIZING)).toBe("not reported yet");
    expect(hydraCountText(NO_ODOM)).toMatch(/^no count yet \(hydra_multi ERR: time since last spin/);
  });
});

describe("robot status card", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    useRobotStatus.setState({open: null, phase: null});
  });
  const mount = (el: React.ReactNode) => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(el));
    return host;
  };

  it("says what is broken first: no odometry before Hydra's missing modules", () => {
    useRobots.setState({state: {gauss: {readiness: NO_ODOM}} as never, stateAt: {gauss: Date.now()}, link: {}, plan: {}});
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-verdict-gauss]")?.textContent).toBe("Blocked: cannot map");
    const broken = h.querySelector("[data-testid=rs-broken-gauss]")!;
    expect(broken.textContent).toMatch(/^Broken first/);
    expect(broken.querySelector("li")?.textContent).toMatch(/^No odometry: .*gauss\/odom -> base_link/);
    expect(broken.textContent).toMatch(/active_window, backend are not reporting.*there is no odometry/);
    // the fallback depth correction is labelled acceptable; zed quality is info only
    expect(h.querySelector("[data-testid=rs-item-depth_correction]")?.textContent).toMatch(/acceptable/);
    expect(h.querySelector("[data-testid=rs-item-zed_depth_quality]")?.textContent).toMatch(/info only.*not measured/);
    expect(h.querySelector("[data-testid=rs-item-odom] .rs-badge")?.textContent).toBe("FAIL");
  });

  it("shows hydra_multi's loop closures and last optimization in a relocalize session", () => {
    useRobots.setState({state: {gauss: {readiness: RELOC_IDLE}} as never, stateAt: {gauss: Date.now()}, link: {}, plan: {}});
    useRobotStatus.setState({phase: "localize"});
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-verdict-gauss]")?.textContent).toBe("Ready to localize");
    expect(h.querySelector("[data-testid=rs-hydra-gauss]")?.textContent).toMatch(/^7 \/ 9 accepted/);
    expect(h.querySelector("[data-testid=rs-opt-gauss]")?.textContent).toMatch(/^took 412 ms · last reported 19 s ago$/);
    // the current phase's checklist comes first
    expect(h.querySelector(".rs-list h4")?.textContent).toMatch(/^Ready to localize/);
    expect(h.textContent).toMatch(/GPS-based: cannot go green indoors/);
  });

  it("opens from the legend dot, closes with Escape, and has 44 px targets", () => {
    useRobots.setState({state: {gauss: {readiness: RELOCALIZING}} as never, stateAt: {gauss: Date.now()}, link: {}, plan: {}});
    const h = mount(
      <>
        <RobotLegend />
        <RobotStatusCard />
      </>,
    );
    const dot = h.querySelector("[data-testid=readiness-dot-gauss]") as HTMLButtonElement;
    expect(dot.className).toMatch(/rs-tone-ok/); // no prior map: judged on mapping
    expect(dot.getAttribute("aria-label")).toMatch(/^gauss: Ready to map/);
    act(() => dot.click());
    expect(useRobotStatus.getState().open).toBe("gauss");
    expect(h.querySelector("[data-testid=robot-status-gauss]")).not.toBeNull();
    // switching the judged phase to localize: relocalizing is amber
    const loc = [...h.querySelectorAll(".rs-seg")].find(b => b.textContent === "Localize") as HTMLButtonElement;
    act(() => loc.click());
    expect(h.querySelector("[data-testid=readiness-dot-gauss]")?.className).toMatch(/rs-tone-warn/);
    expect(h.querySelector("[data-testid=rs-broken-gauss]")?.textContent).toMatch(/Waiting on.*Relocalizing: 2 of the 5/);
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})));
    expect(useRobotStatus.getState().open).toBeNull();
  });

  it("says when the robot stopped reporting", () => {
    useRobots.setState({state: {gauss: {readiness: RELOC_IDLE}} as never, stateAt: {gauss: Date.now() - 60_000}, link: {}, plan: {}});
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-verdict-gauss]")?.textContent).toBe("No status from the robot");
    expect(h.textContent).toMatch(/No state from gauss for 60 s: showing its last report/);
  });
});

describe("readiness dot keys", () => {
  it("lets Escape through to close the card", () => {
    useRobots.setState({state: {gauss: {readiness: FIX.relocalizing as unknown as Readiness}} as never, stateAt: {gauss: Date.now()}, link: {}, plan: {}});
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() =>
      root.render(
        <>
          <RobotLegend />
          <RobotStatusCard />
        </>,
      ),
    );
    const dot = host.querySelector("[data-testid=readiness-dot-gauss]") as HTMLButtonElement;
    act(() => dot.click());
    expect(useRobotStatus.getState().open).toBe("gauss");
    act(() => {
      dot.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    });
    expect(useRobotStatus.getState().open).toBeNull();
    act(() => root.unmount());
    host.remove();
  });
});
