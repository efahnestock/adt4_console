import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {RobotStatusBody, RobotStatusCard} from "../src/viewer/RobotStatusCard";
import {RobotLegend} from "../src/viewer/RobotLegend";
import {useRobots} from "../src/stores/robots";
import {useRobotStatus} from "../src/stores/robotStatus";
import {hydraCountText, hydraOptText, ringColor, ringLabel, ringTone, STATE_STALE_S} from "../src/lib/readiness";
import type {Readiness} from "../src/types";
// computed by the backend (adt4_console/readiness.py) from status tables
// modelled on the 2026-11-15 willow logs (tests/test_readiness.py)
import FIX from "./fixtures/readiness.json";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const NO_ODOM = FIX.no_odom as unknown as Readiness;
const RELOC_IDLE = FIX.relocalized_idle as unknown as Readiness;
const RELOCALIZING = FIX.relocalizing as unknown as Readiness;
const SUSPECT = FIX.suspect as unknown as Readiness;

describe("localization ring", () => {
  it("is green relocalized, amber relocalizing, red blocked", () => {
    expect(ringTone(RELOC_IDLE, 0)).toBe("ok"); // hydra_multi idle keeps the last count
    expect(ringTone(RELOCALIZING, 0)).toBe("warn");
    expect(ringTone(NO_ODOM, 0)).toBe("fail");
    expect(ringTone(SUSPECT, 0)).toBe("fail");
    expect(ringLabel("ok")).toBe("Relocalized");
    expect(ringLabel("warn")).toBe("Not relocalized yet");
    expect(ringLabel("fail")).toBe("Blocked: cannot relocalize");
  });

  it("is red when the robot stops reporting, grey with no data", () => {
    expect(ringTone(RELOC_IDLE, STATE_STALE_S + 1)).toBe("fail");
    expect(ringLabel("fail", STATE_STALE_S + 1)).toBe("No state from the robot");
    expect(ringTone(undefined, 0)).toBe("none");
    expect(ringTone(RELOC_IDLE, null)).toBe("none");
    expect(ringLabel("none")).toBe("No status from the robot");
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
    useRobotStatus.setState({open: null});
  });
  const mount = (el: React.ReactNode) => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(el));
    return host;
  };
  const set = (r: Readiness, at = Date.now()) => useRobots.setState({state: {gauss: {readiness: r}} as never, stateAt: {gauss: at}, link: {}, plan: {}});

  it("puts a failing odometry check on one line at the top; no mapping checklist", () => {
    set(NO_ODOM);
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-verdict-gauss]")?.textContent).toBe("Blocked: cannot relocalize");
    expect(h.querySelector("[data-testid=rs-odom-gauss]")?.textContent).toMatch(/^No odometry: .*gauss\/odom -> base_link/);
    expect(h.textContent).not.toMatch(/Ready to map|semantic inference|Depth correction|ZED depth/i);
    expect(h.querySelector("[data-testid=rs-hydra-gauss]")?.textContent).toMatch(/^no count yet/);
    // GPS relocalization monitor and tf_network: shown, never blocking
    expect(h.querySelector("[data-testid=rs-item-relocalization_monitor]")?.textContent).toMatch(/GPS-based: cannot go green indoors/);
    expect(h.querySelector("[data-testid=rs-item-tf_network]")?.textContent).toMatch(/No TF received on \/gauss\/tf/);
  });

  it("shows hydra_multi's loop closures, last optimization and ROMAN LCs when relocalized", () => {
    set(RELOC_IDLE);
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-verdict-gauss]")?.textContent).toBe("Relocalized");
    expect(h.querySelector("[data-testid=rs-odom-gauss]")).toBeNull();
    expect(h.querySelector("[data-testid=rs-hydra-gauss]")?.textContent).toMatch(/^7 \/ 9 accepted/);
    expect(h.querySelector("[data-testid=rs-opt-gauss]")?.textContent).toMatch(/^took 412 ms · last reported 19 s ago$/);
    expect(h.querySelector("[data-testid=rs-roman-gauss]")?.textContent).toBeTruthy();
    expect(h.querySelector("[data-testid=rs-broken-gauss]")?.textContent).toMatch(/Relocalized: nothing in the way/);
  });

  it("flags a suspect placement", () => {
    set(SUSPECT);
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-broken-gauss]")?.textContent).toMatch(/Broken first.*Suspect placement: the placement jumped 2x/);
  });

  it("opens from the legend dot, closes with Escape", () => {
    set(RELOCALIZING);
    const h = mount(
      <>
        <RobotLegend />
        <RobotStatusCard />
      </>,
    );
    const dot = h.querySelector("[data-testid=readiness-dot-gauss]") as HTMLButtonElement;
    expect(dot.className).toMatch(/rs-tone-warn/);
    expect(dot.getAttribute("aria-label")).toMatch(/^gauss: Not relocalized yet \(Relocalizing: 2 of the 5/);
    act(() => dot.click());
    expect(useRobotStatus.getState().open).toBe("gauss");
    expect(h.querySelector("[data-testid=robot-status-gauss]")).not.toBeNull();
    expect(h.querySelector("[data-testid=rs-broken-gauss]")?.textContent).toMatch(/Waiting on.*Relocalizing: 2 of the 5/);
    act(() => {
      dot.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    });
    expect(useRobotStatus.getState().open).toBeNull();
  });

  it("says when the robot stopped reporting", () => {
    set(RELOC_IDLE, Date.now() - 60_000);
    const h = mount(<RobotStatusBody robot="gauss" />);
    expect(h.querySelector("[data-testid=rs-verdict-gauss]")?.textContent).toBe("No state from the robot");
    expect(h.textContent).toMatch(/No state from gauss for 60 s: showing its last report/);
  });
});
