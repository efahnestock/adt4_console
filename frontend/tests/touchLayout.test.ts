import {describe, expect, it} from "vitest";
import {MISSION_BOTH_PANES_W, defaultPanes, onePanelAtATime} from "../src/viewer/paneLayout";

// CSS viewports (px) of the target devices
const TAB_S6 = {land: 1280, port: 800};
const TAB_ACTIVE3_LAND = 962; // 1920 x 1200 at dpr 2 (1280 at dpr 1.5 is the Tab S6 case)

describe("panes a touch tablet opens with", () => {
  it("Tab S6 landscape: everything side by side, as on a laptop", () => {
    expect(defaultPanes(TAB_S6.land, false)).toEqual({left: true, right: true});
    expect(defaultPanes(TAB_S6.land, true)).toEqual({left: true, right: true});
  });
  it("a ~960 px tablet's mission planner keeps the map: chat yes, side panel on demand", () => {
    expect(defaultPanes(TAB_ACTIVE3_LAND, true)).toEqual({left: true, right: false});
    expect(TAB_ACTIVE3_LAND).toBeLessThan(MISSION_BOTH_PANES_W);
    // the robot view is unchanged there
    expect(defaultPanes(TAB_ACTIVE3_LAND, false)).toEqual({left: false, right: true});
  });
  it("Tab S6 portrait: no panel over the map until asked", () => {
    expect(defaultPanes(TAB_S6.port, false)).toEqual({left: false, right: false});
    expect(defaultPanes(TAB_S6.port, true)).toEqual({left: false, right: false});
  });
});

describe("one side panel at a time", () => {
  it("on phones and on tablets <= 900 px, where panels lie over the map", () => {
    expect(onePanelAtATime(true, false)).toBe(true);
    expect(onePanelAtATime(false, false)).toBe(true);
  });
  it("not on wide screens, where they sit beside the map", () => {
    expect(onePanelAtATime(false, true)).toBe(false);
  });
});
