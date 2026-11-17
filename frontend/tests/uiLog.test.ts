import {describe, expect, it} from "vitest";
import {describeDialog, describeTarget} from "../src/lib/uiLog";

const el = (html: string) => {
  const d = document.createElement("div");
  d.innerHTML = html;
  return d;
};

describe("uiLog", () => {
  it("names a control by aria-label, test id, then text", () => {
    expect(describeTarget(el(`<button aria-label="Jobs"><svg></svg> Jobs 3</button>`).querySelector("svg"))).toEqual({label: "Jobs"});
    expect(describeTarget(el(`<button data-testid="trial-stop">Stop   recording</button>`).querySelector("button"))).toEqual({label: "trial-stop"});
    expect(describeTarget(el(`<a href="#/maps">  Maps\n</a>`).querySelector("a"))).toEqual({label: "Maps"});
    expect(describeTarget(el(`<p>just text</p>`).querySelector("p"))).toBeNull();
  });
  it("says which dialog a click was in, and never reads typed text", () => {
    const d = el(`<div role="dialog" aria-label="Approve pick?"><h2>Approve pick?</h2><input value="secret"><button>Approve</button></div>`);
    expect(describeTarget(d.querySelector("button"))).toEqual({label: "Approve", detail: "in dialog: Approve pick?"});
    expect(describeTarget(d.querySelector("input"))).toBeNull();
    expect(describeDialog(d.querySelector("[role=dialog]")!)).toEqual({label: "Approve pick?", detail: "Approve pick?Approve"});
  });
});
