import {describe, expect, it} from "vitest";
import {makeUploader, pickMime, screenPref, setScreenPref} from "../src/lib/screenRec";

describe("screenRec", () => {
  it("prefers vp9 webm, falls back, and copes with nothing supported", () => {
    expect(pickMime(() => true)).toBe("video/webm;codecs=vp9");
    expect(pickMime(m => m === "video/mp4")).toBe("video/mp4");
    expect(pickMime(() => false)).toBe("");
  });
  it("is on by default and remembers being switched off", () => {
    window.localStorage.clear();
    expect(screenPref()).toBe(true);
    setScreenPref(false);
    expect(screenPref()).toBe(false);
    setScreenPref(true);
    expect(screenPref()).toBe(true);
  });
  it("uploads pieces in order, retries a failure, and moves on after giving up", async () => {
    const sent: string[] = [];
    const errors: unknown[] = [];
    let failFirst = 2; // piece 0 fails twice, then goes through
    const up = makeUploader(
      async (seq, data, final) => {
        if (seq === 0 && failFirst-- > 0) throw new Error("offline");
        if (seq === 2) throw new Error("never");
        sent.push(`${seq}:${data.size}${final ? ":final" : ""}`);
      },
      e => errors.push(e),
      3,
      1,
    );
    void up(new Blob(["a"]));
    void up(new Blob(["b"]));
    void up(new Blob(["c"]));
    await up(new Blob([]), true);
    expect(sent).toEqual(["0:1", "1:1", "3:0:final"]);
    expect(errors).toHaveLength(1);
  });
});
