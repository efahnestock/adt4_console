// A name the operator gives a node in the Map tab is what its label shows.
import {describe, expect, it} from "vitest";
import type {NodeRecord} from "../src/codec";
import {GraphModel} from "../src/lib/graphModel";
import {layoutGraph} from "../src/viewer/layout";

const rec = (sym: string, layer: number, part: number, label: number, name = ""): NodeRecord => ({
  sym, layer, part, pos: [0, 0, 0], label, bbox: null, color: [0, 0, 0], name, active: false,
});
const base = {hiddenLayers: {}, edgeKinds: {}, flat: false, stackHeight: 2, style: "hydra" as const, text: true};

describe("operator names on labels", () => {
  it("an object shows its name instead of its class; unnamed ones keep the class", () => {
    const m = new GraphModel();
    m.labelspaces["2/0"] = {"3": "car"};
    m.nodes.set("O604", rec("O604", 2, 0, 3, "blue van"));
    m.nodes.set("O605", rec("O605", 2, 0, 3));
    m.nodes.set("O606", rec("O606", 2, 0, 3, "car"));
    const t = Object.fromEntries(layoutGraph(m, base).texts.filter(x => x.kind === "objects").map(x => [x.sym, x.text]));
    expect(t).toEqual({O604: "blue van O604", O605: "car O605", O606: "car O606"});
  });
  it("a place shows its name next to its symbol; a region shows its name", () => {
    const m = new GraphModel();
    m.nodes.set("t7", rec("t7", 3, 2, -1, "sidewalk end"));
    m.nodes.set("R2", rec("R2", 4, 0, -1, "north sidewalk"));
    const t = layoutGraph(m, base).texts;
    expect(t.find(x => x.sym === "t7")?.text).toBe("t(7) sidewalk end");
    expect(t.find(x => x.sym === "R2")?.text).toBe("north sidewalk R(2)");
  });
});
