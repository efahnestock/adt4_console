// Mock objects get the next free object symbol after the map's real objects.
import {describe, expect, it} from "vitest";
import {nextMockSym} from "../src/stores/mission";

describe("mock object symbols", () => {
  it("numbers on from the map's objects and the other mocks", () => {
    expect(nextMockSym([], 700)).toBe("O(700)");
    expect(nextMockSym([{label: "a", pos: [0, 0], sym: "O(700)"}, {label: "b", pos: [0, 0], sym: "O(702)"}], 700)).toBe("O(703)");
    expect(nextMockSym([{label: "a", pos: [0, 0]}], 12)).toBe("O(12)");
    expect(nextMockSym([], undefined)).toBeUndefined(); // no map info: the server numbers it
  });
});
