import {describe, expect, it} from "vitest";
import {pickLayer, type TileLayer} from "../src/lib/geo";

describe("pickLayer", () => {
  const L = (id: string, extra: Partial<TileLayer> = {}): TileLayer => ({id, attribution: "", zmin: null, zmax: null, bbox_lonlat: null, cached: null, live: false, default: false, ...extra});
  const layers = [L("esri_world_imagery", {live: true}), L("city", {default: true}), L("quad")];
  it("uses the viewer's pick when the server has it", () => {
    expect(pickLayer(layers, "quad")?.id).toBe("quad");
  });
  it("falls back to the manifest default for no pick or an unknown one", () => {
    expect(pickLayer(layers, null)?.id).toBe("city");
    expect(pickLayer(layers, "gone")?.id).toBe("city");
  });
  it("then live Esri, then the first layer", () => {
    expect(pickLayer([L("a"), L("esri_world_imagery")], null)?.id).toBe("esri_world_imagery");
    expect(pickLayer([L("a"), L("b")], null)?.id).toBe("a");
    expect(pickLayer([], "x")).toBeUndefined();
  });
});
