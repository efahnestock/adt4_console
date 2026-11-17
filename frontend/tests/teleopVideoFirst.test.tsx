// Teleop: video first, stills as the backup. The drive layout's camera opens as
// live video; when no frame arrives (VideoView's watchdog) it falls back to stills
// with a "Try video" button; the plain Camera tab keeps stills by default.
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {useRobots} from "../src/stores/robots";
import {useViewer} from "../src/stores/viewer";
import {useFleet} from "../src/stores/fleet";

// the stub reports "unavailable" when the test asks it to
let failVideo: ((reason: string) => void) | null = null;
vi.mock("../src/viewer/VideoView", async importOriginal => ({
  ...(await importOriginal<typeof import("../src/viewer/VideoView")>()),
  VideoView: ({robot, cam, onUnavailable}: {robot: string; cam: string; onUnavailable?: (r: string) => void}) => {
    failVideo = onUnavailable ?? null;
    return <div data-testid="video" data-robot={robot} data-cam={cam} />;
  },
}));

const {CameraPanel} = await import("../src/viewer/CameraPanel");

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

describe("CameraPanel in the drive layout (teleop)", () => {
  let root: Root | null = null;
  let container: HTMLDivElement;
  beforeEach(() => {
    failVideo = null;
    useFleet.setState({machines: {network: "test", machines: [{name: "husky1", role: "robot", ip: "", user: ""}]}} as never);
    useRobots.setState({state: {husky1: {platform: "husky"}}, link: {}, plan: {}, teleop: {}} as never);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/cameras")) return new Response(JSON.stringify({cameras: ["front"]}), {headers: {"Content-Type": "application/json"}});
        return new Response("no", {status: 503});
      }),
    );
  });
  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    container?.remove();
    vi.unstubAllGlobals();
    useViewer.getState().setFocus("all");
  });

  async function render(el: React.ReactElement) {
    useViewer.getState().setFocus("husky1");
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(el));
    await act(async () => {
      await new Promise(r => setTimeout(r, 0));
    });
  }
  const video = () => container.querySelector<HTMLElement>("[data-testid=video]");
  const fallback = () => container.querySelector<HTMLElement>("[data-testid=video-fallback]");

  it("opens as live video", async () => {
    await render(<CameraPanel compact preferVideo />);
    expect(video()?.dataset.cam).toBe("front");
    expect(fallback()).toBeNull();
  });

  it("falls back to stills when no video arrives, and Try video goes back", async () => {
    await render(<CameraPanel compact preferVideo />);
    expect(failVideo).not.toBeNull();
    await act(async () => failVideo!("no video frames arrived"));
    expect(video()).toBeNull();
    expect(fallback()?.textContent).toContain("Video unavailable (no video frames arrived)");
    const retry = [...fallback()!.querySelectorAll("button")].find(b => b.textContent === "Try video")!;
    await act(async () => retry.click());
    expect(video()?.dataset.cam).toBe("front");
    expect(fallback()).toBeNull();
  });

  it("the Camera tab outside the drive layout keeps stills by default", async () => {
    await render(<CameraPanel />);
    expect(video()).toBeNull();
  });

  it("entering the drive layout with the panel open switches stills to video", async () => {
    await render(<CameraPanel />);
    expect(video()).toBeNull();
    await act(async () => root!.render(<CameraPanel compact preferVideo />));
    expect(video()?.dataset.cam).toBe("front");
  });
});
