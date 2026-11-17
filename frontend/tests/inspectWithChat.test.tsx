import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {ChatPanel} from "../src/viewer/ChatPanel";
import {InspectCard} from "../src/viewer/InspectCard";
import {inspectPlacement} from "../src/viewer/paneLayout";
import {chatCaret, clearChat, insertIntoChat, insertToken, noteChatCaret, setChatDraft, useChat} from "../src/stores/chat";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const base = {missionMode: false, onePanel: false, leftOpen: false, rightOpen: true, dockTab: "inspect", layersInMission: false, inspectFloats: false, chatFloats: false};

describe("a picked node does not take the chat away", () => {
  it("robot view: with the Chat tab docked the node opens on a card", () => {
    expect(inspectPlacement({...base, dockTab: "chat"})).toBe("card");
    // phones: the chat is the open sheet, the same rule
    expect(inspectPlacement({...base, dockTab: "chat", onePanel: true})).toBe("card");
  });
  it("robot view: any other tab (or a closed panel) still brings Inspect forward", () => {
    expect(inspectPlacement({...base, dockTab: "plan"})).toBe("tab");
    expect(inspectPlacement({...base, dockTab: "chat", rightOpen: false})).toBe("tab");
    // the chat in a floating window is not hidden by the Inspect tab
    expect(inspectPlacement({...base, dockTab: "chat", chatFloats: true})).toBe("tab");
    // Inspect in a floating window: raise it, the chat stays where it is
    expect(inspectPlacement({...base, dockTab: "chat", inspectFloats: true})).toBe("tab");
  });
  it("mission planner: desktop keeps Inspect in the side panel next to the chat pane", () => {
    const m = {...base, missionMode: true, leftOpen: true, dockTab: "mission"};
    expect(inspectPlacement(m)).toBe("tab");
    // a phone shows one sheet at a time: opening Inspect would close the chat sheet
    expect(inspectPlacement({...m, onePanel: true})).toBe("card");
    // ...unless that sheet shows Layers, or no sheet is open
    expect(inspectPlacement({...m, onePanel: true, layersInMission: true})).toBe("tab");
    expect(inspectPlacement({...m, onePanel: true, leftOpen: false})).toBe("tab");
  });
});

describe("insert a symbol into the chat", () => {
  afterEach(() => {
    clearChat();
    setChatDraft("");
    noteChatCaret(0, 0);
  });
  it("spaces the symbol from its neighbours", () => {
    expect(insertToken("", "O(12)")).toEqual({text: "O(12) ", caret: 6});
    expect(insertToken("go to", "O(12)")).toEqual({text: "go to O(12) ", caret: 12});
    expect(insertToken("go to  then", "O(12)", 6)).toEqual({text: "go to O(12) then", caret: 11});
    expect(insertToken("goto,then", "R(3)", 4)).toEqual({text: "goto R(3) ,then", caret: 10});
    // a selection is replaced
    expect(insertToken("go to X now", "P(850)", 6, 7)).toEqual({text: "go to P(850) now", caret: 12});
  });
  it("goes in at the last caret of the chat box, then after the inserted symbol", () => {
    setChatDraft("compare  and ");
    noteChatCaret(8, 8);
    insertIntoChat("O(1)");
    expect(useChat.getState().draft).toBe("compare O(1) and ");
    insertIntoChat("O(2)");
    expect(useChat.getState().draft).toBe("compare O(1) O(2) and ");
  });
  it("a stale caret past the draft's end appends", () => {
    setChatDraft("hi");
    noteChatCaret(40, 40);
    insertIntoChat("O(5)");
    expect(useChat.getState().draft).toBe("hi O(5) ");
  });
  it("the mounted chat box shows it with the caret after the symbol", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", {status: 200})));
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root: Root = createRoot(host);
    await act(async () => root.render(<ChatPanel />));
    const ta = host.querySelector<HTMLTextAreaElement>("textarea[aria-label='Chat message']")!;
    setChatDraft("go to  now");
    await act(async () => {});
    // React's onSelect: the focused box's selection changed (keyup / mouseup / selectionchange)
    act(() => {
      ta.focus();
      ta.setSelectionRange(6, 6);
      ta.dispatchEvent(new KeyboardEvent("keyup", {bubbles: true}));
    });
    act(() => ta.blur());
    expect(chatCaret()).toEqual({start: 6, end: 6});
    act(() => insertIntoChat("O(12)"));
    expect(ta.value).toBe("go to O(12) now");
    expect(ta.selectionStart).toBe(11);
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });
});

describe("the inspect card", () => {
  it("shows the node and its insert / expand / close actions", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const onClose = vi.fn();
    const onExpand = vi.fn();
    const onInsert = vi.fn();
    await act(async () =>
      root.render(
        <InspectCard title="bag" sym="O(9)" onInsert={onInsert} onClose={onClose} onExpand={onExpand} expandLabel="Inspect tab">
          <p>node</p>
        </InspectCard>,
      ),
    );
    expect(host.querySelector("[data-testid=inspect-card]")?.textContent).toContain("node");
    act(() => host.querySelector<HTMLButtonElement>("[data-testid=inspect-card-close]")!.click());
    act(() => host.querySelector<HTMLButtonElement>("[data-testid=inspect-card-expand]")!.click());
    act(() => host.querySelector<HTMLButtonElement>("[data-testid=inspect-card-insert]")!.click());
    expect(onInsert).toHaveBeenCalledWith("O(9)");
    expect(host.querySelector("[data-testid=inspect-card]")?.textContent).toContain("bag");
    expect(onClose).toHaveBeenCalledOnce();
    expect(onExpand).toHaveBeenCalledOnce();
    act(() => root.unmount());
    host.remove();
  });
});
