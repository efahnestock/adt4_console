import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {objectLabel, PickResultDialog, PickResultHost, type PickQuestion} from "../src/viewer/PickResult";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

function question(extra: Partial<PickQuestion> = {}): PickQuestion {
  return {id: "q1", robot: "euclid", object_id: "o58", object_class: "bag", attempt: 1, gripper_holding: true,
    images: [{name: "hand_color_image", width: 640, height: 480}, {name: "frontleft_fisheye_image", width: 480, height: 640}],
    received_at: 1000, ...extra};
}

let root: Root | null = null;
let container: HTMLDivElement;
afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function mount(node: React.ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  return act(async () => root!.render(node));
}

function button(text: string) {
  const result = [...container.querySelectorAll("button")].find(item => item.textContent?.includes(text));
  if (!result) throw new Error("Button missing: " + text);
  return result;
}

describe("PickResultDialog", () => {
  it("asks about the robot and object, and shows every camera and the gripper sensor", async () => {
    await mount(<PickResultDialog question={question({attempt: 3})} waiting={1}
      onRespond={vi.fn().mockResolvedValue(undefined)} onLater={vi.fn()} />);
    expect(container.querySelector("h2")!.textContent).toBe("Did euclid pick up o58 (bag)?");
    expect(container.textContent).toContain("(attempt 3)");
    expect(container.textContent).toContain("1 more robot is waiting.");
    expect(container.querySelector('[data-testid="pick-result-sensor"]')!.textContent).toBe("Gripper sensor: holding something.");
    const images = [...container.querySelectorAll("img")];
    expect(images.map(image => image.getAttribute("src"))).toEqual([
      "api/pick_result/requests/q1/images/0", "api/pick_result/requests/q1/images/1"]);
    expect([...container.querySelectorAll("figcaption")].map(item => item.textContent)).toEqual([
      "Gripper camera", "Front left camera"]);
  });

  it("warns when the gripper sensor disagrees with the robot", async () => {
    await mount(<PickResultDialog question={question({gripper_holding: false})}
      onRespond={vi.fn().mockResolvedValue(undefined)} onLater={vi.fn()} />);
    const sensor = container.querySelector('[data-testid="pick-result-sensor"]')!;
    expect(sensor.textContent).toBe("Gripper sensor: holding nothing.");
    expect(sensor.className).toContain("is-warn");
  });

  it("sends picked and retry with one click", async () => {
    const onRespond = vi.fn().mockResolvedValue(undefined);
    await mount(<PickResultDialog question={question()} onRespond={onRespond} onLater={vi.fn()} />);
    await act(async () => button("Picked — continue").click());
    expect(onRespond).toHaveBeenLastCalledWith("picked");
    act(() => root?.unmount());
    container.remove();
    await mount(<PickResultDialog question={question()} onRespond={onRespond} onLater={vi.fn()} />);
    await act(async () => button("Failed — pick again").click());
    expect(onRespond).toHaveBeenLastCalledWith("retry");
  });

  it("asks once more before giving up", async () => {
    const onRespond = vi.fn().mockResolvedValue(undefined);
    await mount(<PickResultDialog question={question()} onRespond={onRespond} onLater={vi.fn()} />);
    await act(async () => button("Failed — give up").click());
    expect(onRespond).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="pick-result-confirm"]')!.textContent).toContain("its plan ends here");
    await act(async () => button("Back").click());
    expect(container.querySelector('[data-testid="pick-result-confirm"]')).toBeNull();
    await act(async () => button("Failed — give up").click());
    await act(async () => button("Give up the pick").click());
    expect(onRespond).toHaveBeenCalledTimes(1);
    expect(onRespond).toHaveBeenLastCalledWith("give_up");
  });

  it("shows why an answer was not sent and lets the operator try again", async () => {
    const onRespond = vi.fn().mockRejectedValueOnce(new Error("euclid's executor is not listening for the answer"))
      .mockResolvedValue(undefined);
    await mount(<PickResultDialog question={question()} onRespond={onRespond} onLater={vi.fn()} />);
    await act(async () => button("Picked — continue").click());
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("not listening");
    expect(button("Picked — continue").disabled).toBe(false);
    await act(async () => button("Picked — continue").click());
    expect(onRespond).toHaveBeenCalledTimes(2);
  });

  it("cannot answer while the console has no robot connection, but can be put off", async () => {
    const onLater = vi.fn();
    await mount(<PickResultDialog question={question()} available={false} serviceError="ROS unavailable"
      onRespond={vi.fn()} onLater={onLater} />);
    expect(button("Picked — continue").disabled).toBe(true);
    expect(button("Failed — pick again").disabled).toBe(true);
    expect(button("Failed — give up").disabled).toBe(true);
    expect(container.textContent).toContain("ROS unavailable");
    await act(async () => button("Later").click());
    expect(onLater).toHaveBeenCalled();
  });

  it("names an object without a class or id sensibly", () => {
    expect(objectLabel(question({object_class: ""}))).toBe("o58");
    expect(objectLabel(question({object_id: "", object_class: ""}))).toBe("the object");
  });
});

describe("PickResultHost", () => {
  function serve(state: {requests: PickQuestion[]}, posts: unknown[] = []) {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/respond")) {
        posts.push([url, JSON.parse(String(init?.body))]);
        const id = url.split("/")[3];
        state.requests = state.requests.filter(item => item.id !== id);
        return new Response(JSON.stringify({ok: true}), {status: 200});
      }
      return new Response(JSON.stringify({available: true, error: null, requests: state.requests}), {status: 200});
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("shows nothing while no robot asks", async () => {
    serve({requests: []});
    await mount(<PickResultHost />);
    expect(container.querySelector('[data-testid="pick-result-dialog"]')).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("pops the question up by itself and posts the answer", async () => {
    const posts: unknown[] = [];
    serve({requests: [question()]}, posts);
    await mount(<PickResultHost />);
    expect(container.querySelector('[data-testid="pick-result-dialog"]')).not.toBeNull();
    await act(async () => button("Failed — pick again").click());
    expect(posts).toEqual([["api/pick_result/requests/q1/respond", {result: "retry"}]]);
    expect(container.querySelector('[data-testid="pick-result-dialog"]')).toBeNull();
    expect(container.textContent).toContain("euclid: picking o58 again.");
  });

  it("Later hides the question behind a button that brings it back", async () => {
    serve({requests: [question()]});
    await mount(<PickResultHost />);
    await act(async () => button("Later").click());
    expect(container.querySelector('[data-testid="pick-result-dialog"]')).toBeNull();
    const open = container.querySelector<HTMLButtonElement>('[data-testid="pick-result-open"]')!;
    expect(open.textContent).toBe("Pick result: euclid is waiting — open");
    await act(async () => open.click());
    expect(container.querySelector('[data-testid="pick-result-dialog"]')).not.toBeNull();
  });

  it("asks about one robot at a time, the one waiting longest first", async () => {
    const posts: unknown[] = [];
    serve({requests: [question(), question({id: "q2", robot: "hamilton", object_id: "o2"})]}, posts);
    await mount(<PickResultHost />);
    expect(container.querySelector("h2")!.textContent).toContain("euclid");
    expect(container.textContent).toContain("1 more robot is waiting.");
    await act(async () => button("Picked — continue").click());
    expect(container.querySelector("h2")!.textContent).toContain("hamilton");
    expect(posts).toHaveLength(1);
  });
});
