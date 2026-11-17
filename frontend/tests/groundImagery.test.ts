// Ground imagery defaults: on unless the viewer turned it off, the quad ortho
// composite over its site, and the robot view's anchor lookup for a map that
// is not a mission map (a bag's live map).
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {geoCenter, layerCovers, pickLayer, type GeoInfo, type TileLayer} from "../src/lib/geo";
import {imageryBox} from "../src/viewer/SatelliteGround";

const L = (id: string, extra: Partial<TileLayer> = {}): TileLayer => ({id, attribution: "", zmin: null, zmax: null, bbox_lonlat: null, cached: null, live: false, default: false, ...extra});
const PENN: [number, number] = [-75.2, 39.941];
const city = L("meridian_phl_2025_76cm", {default: true, bbox_lonlat: [-75.2035, 39.9382, -75.1953, 39.9445]});
// the server reports an ortho with a fallback over both areas (geo.py TileCache.layers)
const quad = L("quad_penn_2024", {kind: "ortho", priority: 10, fallback: city.id, bbox_lonlat: city.bbox_lonlat});
const geoAt = (lon: number, lat: number): GeoInfo => ({
  anchor: {epsg: 32618, yaw: 0, t: [0, 0], z0: 0},
  affine: {center: [0, 0], map_to_lonlat: [[1e-5, 0, lon], [0, 1e-5, lat]], lonlat_to_map: [[1e5, 0, -lon * 1e5], [0, 1e5, -lat * 1e5]], max_err_m: 0},
});

describe("default imagery layer", () => {
  it("is the quad ortho composite over its site, ahead of the manifest default", () => {
    expect(pickLayer([city, quad], null, PENN)?.id).toBe("quad_penn_2024");
    expect(pickLayer([quad, city], null, PENN)?.id).toBe("quad_penn_2024");
  });
  it("is the satellite default for a map the ortho does not cover", () => {
    expect(pickLayer([city, quad], null, [-71.09, 42.36])?.id).toBe(city.id);
  });
  it("leaves the viewer's own pick alone", () => {
    expect(pickLayer([city, quad], city.id, PENN)?.id).toBe(city.id);
    expect(pickLayer([city, quad], "gone", PENN)?.id).toBe("quad_penn_2024");
  });
  it("takes the highest priority, and ignores priority 0", () => {
    const sharper = L("drone_2026", {kind: "ortho", priority: 20, bbox_lonlat: [-75.21, 39.93, -75.19, 39.95]});
    expect(pickLayer([city, quad, sharper], null, PENN)?.id).toBe("drone_2026");
    expect(pickLayer([city, L("x", {priority: 0})], null, PENN)?.id).toBe(city.id);
  });
  it("coverage: live layers and layers without a box count as covering", () => {
    expect(layerCovers(L("e", {live: true, bbox_lonlat: [0, 0, 1, 1]}), PENN)).toBe(true);
    expect(layerCovers(L("n"), PENN)).toBe(true);
    expect(layerCovers(L("b", {bbox_lonlat: [0, 0, 1, 1]}), PENN)).toBe(false);
  });
  it("uses the anchor's fit point as the map centre", () => {
    expect(geoCenter(geoAt(-75.2, 39.941))).toEqual([-75.2, 39.941]);
    expect(geoCenter(null)).toBeNull();
  });
});

describe("imagery extent", () => {
  it("is the mission world's, else the scene's snapped out to 50 m", () => {
    expect(imageryBox([1, 2, 3, 4], {min: [0, 0, 0], max: [9, 9, 9]})).toEqual([1, 2, 3, 4]);
    expect(imageryBox(null, {min: [-12, 3, 0], max: [61, 49, 2]})).toEqual([-50, 0, 100, 50]);
    expect(imageryBox(null, null)).toBeNull();
    // no map yet (a bag just started): the ground around the anchor's fit point
    expect(imageryBox(null, null, [10, -5])).toEqual([-90, -105, 110, 95]);
  });
});

describe("ground imagery on by default", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.resetModules();
  });
  afterEach(() => vi.doUnmock("../src/lib/api"));

  it("is on when the viewer never chose, and the choice is kept", async () => {
    const {useMission, imageryOnByDefault} = await import("../src/stores/mission");
    expect(imageryOnByDefault(null)).toBe(true);
    expect(useMission.getState().satellite).toBe(true);
    useMission.getState().set({satellite: false});
    expect(window.localStorage.getItem("adt4.mission.satellite.v1")).toBe("0");
    vi.resetModules();
    const again = await import("../src/stores/mission");
    expect(again.useMission.getState().satellite).toBe(false);
  });

  it("an explicit 'on' stays on", async () => {
    window.localStorage.setItem("adt4.mission.satellite.v1", "1");
    const {useMission} = await import("../src/stores/mission");
    expect(useMission.getState().satellite).toBe(true);
  });

  it("robot view: a map with no mission world takes the anchor of the map the viewer shows", async () => {
    const urls: string[] = [];
    vi.doMock("../src/lib/api", async orig => ({
      ...(await orig<typeof import("../src/lib/api")>()),
      getJSON: async (url: string) => {
        urls.push(url);
        if (url === "api/geo/layers") return {layers: [city, quad]};
        if (url.startsWith("api/missions/world")) throw new Error("500");
        if (url.startsWith("api/geo/anchor/")) {
          const g = geoAt(-75.2, 39.941);
          return {anchor: g.anchor, affine: g.affine};
        }
        throw new Error(`unexpected ${url}`);
      },
    }));
    const {useViewer} = await import("../src/stores/viewer");
    const {useMission, loadGeo, imageryLayer} = await import("../src/stores/mission");
    useViewer.setState({order: ["file:prior", "live:bag"], sources: {"file:prior": {enabled: false}, "live:bag": {enabled: true}}} as never);
    await loadGeo(null);
    expect(urls).toContain("api/geo/anchor/live%3Abag?cx=0&cy=0");
    const s = useMission.getState();
    expect(s.geoSrc).toBe("live:bag");
    expect(s.geo).not.toBeNull();
    expect(imageryLayer(s)?.id).toBe("quad_penn_2024");
  });
});
