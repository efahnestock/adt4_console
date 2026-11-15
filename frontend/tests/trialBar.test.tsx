import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {TrialBar} from "../src/components/TrialBar";
import {useTrial, serverNow} from "../src/stores/trial";
import {fmtElapsed, fmtMinutes, machineTone, recordingCount, shortestLeft, staleAfter, type Trial, type TrialMachine, type TrialPayload} from "../src/lib/trial";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));
const T0 = 1_794_800_000;

const machine = (name: string, over: Partial<TrialMachine> = {}): TrialMachine => ({
  name, role: "robot", state: "recording", message: "recording", bytes: 6e9, rate_bps: 80e6, free_bytes: 100e9,
  minutes_left: 20.8, last_seen: T0 + 118, warnings: [], ...over,
});
const trial = (machines: TrialMachine[], over: Partial<Trial> = {}): Trial => ({
  trial_id: "trial_20261115T193012Z_door-search", description: "Door search", state: "recording", started: T0, stopped: null,
  events: 9, flags: 2, machines: Object.fromEntries(machines.map(m => [m.name, m])), ...over,
});
const payload = (active: Trial | null, last: Trial | null = null): TrialPayload => ({now: T0 + 120, poll_s: 10, active, last});

describe("trial lib", () => {
  it("is green only for a fresh, warning-free recording", () => {
    const now = T0 + 120;
    expect(machineTone(machine("a"), now, 10)).toBe("ok");
    expect(machineTone(machine("a", {last_seen: now - staleAfter(10) - 1}), now, 10)).toBe("warn"); // answer too old
    expect(machineTone(machine("a", {last_seen: null}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {warnings: ["disk: 4.0 GB free"]}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {topic_check: {ok: true, missing: ["/a/fix"]}}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {state: "unreachable"}), now, 10)).toBe("warn");
    for (const state of ["lost", "failed", "stop_failed"] as const) expect(machineTone(machine("a", {state}), now, 10)).toBe("err");
  });
  it("counts only machines known to record now", () => {
    const t = trial([machine("a"), machine("b", {state: "unreachable"}), machine("c", {last_seen: T0}), machine("d", {state: "skipped"})]);
    expect(recordingCount(t, T0 + 120, 10)).toEqual({ok: 1, of: 3});
  });
  it("names the disk that fills first", () => {
    const t = trial([machine("a", {minutes_left: 43}), machine("b", {minutes_left: 8.2}), machine("c", {state: "lost", minutes_left: 1})]);
    expect(shortestLeft(t)).toEqual({name: "b", minutes: 8.2});
  });
  it("formats", () => {
    expect(fmtElapsed(75)).toBe("01:15");
    expect(fmtElapsed(3725)).toBe("1:02:05");
    expect(fmtMinutes(8.2)).toBe("8 min");
    expect(fmtMinutes(150)).toBe("2.5 h");
    expect(fmtMinutes(null)).toBe("?");
    expect(serverNow(payload(null), 1000, 6000)).toBe(T0 + 125);
  });
});

describe("TrialBar", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const mount = async (p: TrialPayload, fetchImpl?: (url: string, init?: RequestInit) => Response) => {
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => (fetchImpl ?? (() => json(p)))(String(url), init)));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(<TrialBar />));
    await flush();
    await act(async () => useTrial.getState().set(p));
  };
  const $ = (id: string) => host!.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = host = null;
    useTrial.setState({payload: null, receivedAt: 0});
    vi.unstubAllGlobals();
  });

  it("idle: offers to start and shows nothing green", async () => {
    await mount(payload(null));
    expect($("trial-bar")!.textContent).toContain("Not recording");
    expect($("trial-start")).not.toBeNull();
    expect($("trial-stop")).toBeNull();
  });

  it("recording: every machine listed with its state; one unknown machine turns the bar red", async () => {
    await mount(payload(trial([machine("euclid"), machine("hamilton")])));
    expect($("trial-count")!.textContent).toBe("2 of 2 recording");
    expect($("trial-bar")!.className).not.toContain("trial-bar-warn");
    expect($("trial-machine-euclid")!.textContent).toContain("6.0 GB");
    expect($("trial-machine-euclid")!.textContent).toContain("21 min left");

    await act(async () => useTrial.getState().set(payload(trial([machine("euclid"), machine("hamilton", {state: "unreachable", last_seen: T0 + 80})]))));
    expect($("trial-count")!.textContent).toBe("1 of 2 recording");
    expect($("trial-bar")!.className).toContain("trial-bar-warn");
    expect($("trial-machine-hamilton")!.textContent).toContain("no answer");
    expect($("trial-machine-hamilton")!.textContent).toContain("last answer 40 s ago");
  });

  it("a recorder that died is shown as NOT recording with a retry", async () => {
    await mount(payload(trial([machine("euclid", {state: "lost", message: "euclid is NOT recording"})])));
    expect($("trial-machine-euclid")!.textContent).toContain("NOT recording");
    expect($("trial-machine-euclid")!.querySelector("button")!.textContent).toBe("Retry");
  });

  it("a flag is posted, confirmed, and kept in the box when saving fails", async () => {
    const posts: string[] = [];
    let fail = false;
    await mount(payload(trial([machine("euclid")])), (url, init) => {
      if (url.endsWith("api/trials/flag")) {
        posts.push(String(init?.body));
        return fail ? json({detail: "the flag was NOT saved: events.jsonl: disk full"}, 500) : json({ok: true, event: {t: T0 + 75, elapsed_s: 75}});
      }
      return json(payload(trial([machine("euclid")])));
    });
    const input = $("trial-flag-input") as HTMLInputElement;
    const type = async (v: string) => {
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, v);
        input.dispatchEvent(new Event("input", {bubbles: true}));
      });
    };
    await type("hamilton hesitated at the door");
    await act(async () => $("trial-flag-save")!.click());
    await flush();
    expect(JSON.parse(posts[0])).toEqual({text: "hamilton hesitated at the door"});
    expect($("trial-flag-status")!.textContent).toBe("flag saved at 01:15");
    expect(input.value).toBe("");

    fail = true;
    await type("second note");
    await act(async () => $("trial-flag-save")!.click());
    await flush();
    expect($("trial-flag-status")!.textContent).toContain("FLAG NOT SAVED");
    expect(input.value).toBe("second note");
  });

  it("stopping: offers Stop harder and Close only when a machine is stuck", async () => {
    await mount(payload(trial([machine("euclid", {state: "stopped"}), machine("gauss", {state: "stop_pending", last_seen: T0 + 60})], {state: "stopping"})));
    const text = $("trial-bar")!.textContent!;
    expect(text).toContain("STOPPING");
    expect(text).toContain("Close trial anyway");
    expect(text).not.toContain("Stop harder");
    expect($("trial-flag-input")).toBeNull();
  });
});
