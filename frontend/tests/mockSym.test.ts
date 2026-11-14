// Mock objects get object symbols O(900001), ... when planted.
import {describe, expect, it} from "vitest";
import {nextMockSym} from "../src/stores/mission";

describe("mock object symbols", () => {
  it("takes the next free one", () => {
    expect(nextMockSym([])).toBe("O(900001)");
    expect(nextMockSym([{label: "a", pos: [0, 0], sym: "O(900001)"}, {label: "b", pos: [0, 0], sym: "O(900003)"}])).toBe("O(900002)");
    expect(nextMockSym([{label: "a", pos: [0, 0]}])).toBe("O(900001)");
  });
});
