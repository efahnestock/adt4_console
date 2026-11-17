// An anchored map with no tile layer (no cache, no --tiles-live): the Layers
// panel says why there is no ground imagery instead of hiding the section.
import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {useMission} from "../src/stores/mission";
import {GroundImageryControls} from "../src/viewer/GroundImagery";
import type {GeoInfo, TileLayer} from "../src/lib/geo";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const geo: GeoInfo = {
  anchor: {epsg: 32618, yaw: 0, t: [482937, 4421340], z0: 0},
  affine: {center: [0, 0], map_to_lonlat: [[1e-5, 0, -75.2], [0, 1e-5, 39.94]], lonlat_to_map: [[1e5, 0, 75.2e5], [0, 1e5, -39.94e5]], max_err_m: 0},
};
const esri: TileLayer = {id: "esri_world_imagery", attribution: "Esri", zmin: null, zmax: null, bbox_lonlat: null, cached: null, live: true, default: false};

describe("Ground imagery controls", () => {
  let root: Root | null = null;
  let host: HTMLDivElement;
  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    host?.remove();
  });
  const render = () => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    act(() => root!.render(<GroundImageryControls />));
  };

  it("anchor but no tile layer: a note on how to get imagery", () => {
    useMission.setState({geo, layers: []});
    render();
    expect(host.querySelector("[data-testid=no-imagery]")?.textContent).toContain("--tiles-live");
    expect(host.querySelector("[data-testid=sat-layer]")).toBeNull();
  });
  it("with a layer: the picker, no note", () => {
    useMission.setState({geo, layers: [esri]});
    render();
    expect(host.querySelector("[data-testid=sat-layer]")).not.toBeNull();
    expect(host.querySelector("[data-testid=no-imagery]")).toBeNull();
  });
  it("no anchor: nothing", () => {
    useMission.setState({geo: null, layers: []});
    render();
    expect(host.innerHTML).toBe("");
  });
});
