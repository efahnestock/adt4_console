import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {DrivesPage, linkLabel, onDriveLabel, usable, type Drive, outputLabel} from "../src/pages/Drives";
import {useJobs} from "../src/stores/jobs";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));
const GB = 1e9;

const drive = (over: Partial<Drive> = {}): Drive => ({
  name: "dcist_ssd3", label: "dcist_ssd3", uuid: "7DFF-FFAA", fstype: "exfat", model: "",
  mountpoint: "/mnt/bag_drives/dcist_ssd3", write_dir: "/mnt/bag_drives/dcist_ssd3",
  mounted: true, managed: true, size_bytes: 2000 * GB, free_bytes: 100 * GB,
  usb_speed_mbps: 10000, slow_link: false, error: "", copy_job: null, ...over,
});

const DRIVES = (d: Drive[]) => ({
  updated: 1,
  machines: [
    {name: "hamilton", role: "robot", online: true, installed: true, drives: d, error: ""},
    {name: "euclid", role: "robot", online: true, installed: false, drives: [], error: "bag-drive is not installed on this machine"},
    {name: "gauss", role: "robot", online: false, installed: null, drives: [], error: ""},
  ],
});
const BAGS = {
  machine: "hamilton", root: "/home/swarm/adt4_output",
  runs: [
    {name: "nov11_b45", mtime: 1794440000, bytes: 30 * GB, bags: [{name: "recorded_data", bytes: 30 * GB, files: 2}]},
    {name: "big_run", mtime: 1794350000, bytes: 90 * GB, bags: [{name: "recorded_data", bytes: 90 * GB, files: 2}]},
  ],
};
// the same robot after an interrupted copy: one run done, one partly on the drive
const BAGS_RESUME = {
  ...BAGS,
  runs: [
    {...BAGS.runs[0], on_drive: "verified", dest_bytes: 30 * GB},
    {...BAGS.runs[1], on_drive: "partial", dest_bytes: 45 * GB},
  ],
};

const button = (text: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].find(b => (b.textContent || "").includes(text));
const box = (run: string) => document.querySelector<HTMLInputElement>(`input[aria-label="Select ${run}"]`)!;

describe("Drives page", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
    useJobs.setState({jobs: {}, order: [], drawerOpen: false, focusId: null});
  });
  const mount = async (drives: Drive[], post: (url: string, body: unknown) => Response = () => json({ok: true, message: "ejected"}), bags: unknown = BAGS) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return post(url, JSON.parse(String(init.body ?? "{}")));
      if (url.startsWith("api/drives/bags")) return json(bags);
      if (url === "api/drives") return json(DRIVES(drives));
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(<DrivesPage />));
    await flush();
    await flush();
    return fetchMock;
  };

  it("labels the link speed and what is usable", () => {
    expect(linkLabel({usb_speed_mbps: 10000, slow_link: false})).toBe("10 Gbit/s");
    expect(linkLabel({usb_speed_mbps: 480, slow_link: true})).toContain("USB 2.0");
    expect(linkLabel({usb_speed_mbps: 0, slow_link: false})).toBe("link speed unknown");
    expect(usable(drive())).toBe(true);
    expect(usable(drive({write_dir: ""}))).toBe(false);
    expect(usable(drive({mounted: false}))).toBe(false);
  });

  it("shows each machine's drives, a missing helper and offline machines", async () => {
    await mount([drive({usb_speed_mbps: 480, slow_link: true})]);
    const text = document.body.textContent || "";
    expect(text).toContain("hamilton");
    expect(text).toContain("USB 2.0 (480 Mbit/s): slow");
    expect(text).toContain("helper is not installed");
    expect(text).toContain("Offline: gauss");
    expect(text).toContain("nov11_b45");
  });

  it("ejects a drive", async () => {
    const posts: [string, unknown][] = [];
    await mount([drive()], (url, body) => { posts.push([url, body]); return json({ok: true, message: "ejected dcist_ssd3: safe to unplug"}); });
    await act(async () => button("Eject")!.click());
    await flush();
    expect(posts).toEqual([["api/drives/eject", {machine: "hamilton", drive: "dcist_ssd3"}]]);
    expect(document.body.textContent).toContain("safe to unplug");
  });

  it("ejects a drive the desktop mounted", async () => {
    await mount([drive({managed: false, mountpoint: "/media/swarm/dcist_ssd3"})]);
    expect(button("Eject")!.disabled).toBe(false);
  });

  it("does not eject while a copy is writing to the drive", async () => {
    useJobs.getState().upsert({id: "drive_copy-1", kind: "drive_copy", title: "copy", state: "running", created: 1});
    await mount([drive({copy_job: "drive_copy-1"})]);
    expect(button("Eject")!.disabled).toBe(true);
  });

  it("interrupts a running copy from the drive's row", async () => {
    useJobs.getState().upsert({id: "drive_copy-1", kind: "drive_copy", title: "copy", state: "running", created: 1, progress: {"hamilton copy": 42, "hamilton verify": 10}});
    const posts: string[] = [];
    await mount([drive({copy_job: "drive_copy-1"})], url => { posts.push(url); return json({ok: true, state: "cancelled"}); });
    expect(document.body.textContent).toContain("Copying42%");
    expect(document.body.textContent).toContain("Checking hashes10%");
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="pause-copy"]')!.click());
    await flush();
    expect(posts).toEqual(["api/jobs/drive_copy-1/cancel"]);
    expect(document.body.textContent).toContain("start the copy again later to continue");
  });

  it("asks for the bags with the target drive and offers to resume", async () => {
    const posts: [string, unknown][] = [];
    const fetchMock = await mount([drive({free_bytes: 50 * GB})], (url, body) => { posts.push([url, body]); return json({job_id: "drive_copy-9"}); }, BAGS_RESUME);
    expect(fetchMock.mock.calls.some(c => c[0] === "api/drives/bags?machine=hamilton&drive=dcist_ssd3")).toBe(true);
    expect(onDriveLabel(BAGS_RESUME.runs[1] as never)?.text).toBe("partial 50%");
    expect(document.body.textContent).toContain("partial 50%");
    expect(document.body.textContent).toContain("on drive");
    // "Select unfinished" picks only the run that is not verified on the drive
    await act(async () => button("Select unfinished")!.click());
    expect(box("big_run").checked).toBe(true);
    expect(box("nov11_b45").checked).toBe(false);
    // 90 GB selected but 45 GB are there already: it fits in 50 GB free
    expect(document.body.textContent).toContain("45.0 GB left to copy");
    const resume = button("Resume copy to dcist_ssd3")!;
    expect(resume.disabled).toBe(false);
    await act(async () => resume.click());
    await flush();
    expect(posts).toEqual([["api/drives/copy", {machine: "hamilton", drive: "dcist_ssd3", runs: ["big_run"], remove_source: true}]]);
  });

  it("copies the selected runs and refuses a selection that does not fit", async () => {
    const posts: [string, unknown][] = [];
    await mount([drive()], (url, body) => { posts.push([url, body]); return json({job_id: "drive_copy-7", bytes: 30 * GB}); });
    const copy = () => button("Move to dcist_ssd3")!;
    expect(copy().disabled).toBe(true); // nothing selected
    await act(async () => box("big_run").click());
    await act(async () => box("nov11_b45").click());
    expect(document.body.textContent).toContain("only 100 GB free"); // 120 GB picked
    expect(copy().disabled).toBe(true);
    await act(async () => box("big_run").click());
    expect(copy().disabled).toBe(false);
    await act(async () => copy().click());
    await flush();
    // deleting from the robot after a verified copy is the default
    expect(posts).toEqual([["api/drives/copy", {machine: "hamilton", drive: "dcist_ssd3", runs: ["nov11_b45"], remove_source: true}]]);
    // the job is tracked, but the Jobs drawer stays shut: progress is on the drive's row
    expect(useJobs.getState().jobs["drive_copy-7"]?.state).toBe("running");
    expect(useJobs.getState().drawerOpen).toBe(false);
  });

  it("keeps the bags on the robot when the switch at the top of its card is off", async () => {
    const posts: [string, unknown][] = [];
    await mount([drive()], (url, body) => { posts.push([url, body]); return json({job_id: "drive_copy-8", bytes: 30 * GB}); });
    const sw = document.querySelector<HTMLInputElement>('.card-head [data-testid="remove-after-copy-hamilton"] input')!;
    expect(sw.checked).toBe(true);
    await act(async () => sw.click());
    await act(async () => box("nov11_b45").click());
    await act(async () => button("Copy to dcist_ssd3")!.click());
    await flush();
    expect(posts).toEqual([["api/drives/copy", {machine: "hamilton", drive: "dcist_ssd3", runs: ["nov11_b45"], remove_source: false}]]);
  });

  it("says what happens to a trial's run output", () => {
    const o = {name: "exp_a", bytes: 4e6, files: 12};
    expect(outputLabel(o).text).toBe("+ run output exp_a, 4.0 MB");
    expect(outputLabel({...o, live: true}).tone).toBe("warn");
    // only a folder the running stack writes to says "stack running"
    expect(outputLabel({...o, live: true, stack: true}).text).toContain("stack running");
    expect(outputLabel({...o, live: true}).text).toContain("still being written");
    expect(outputLabel({...o, live: true}).text).not.toContain("stack running");
    const unsure = outputLabel({...o, confirmed: false, note: "every file in it was written after the trial stopped"});
    expect(unsure.tone).toBe("warn");
    expect(unsure.text).toBe("+ run output exp_a, 4.0 MB, launch not confirmed");
    expect(unsure.title).toContain("written after the trial stopped");
    expect(outputLabel({...o, on_drive: "verified"}).text).toBe("+ run output exp_a on drive");
    const gone = outputLabel({...o, missing: true, note: "the folder is not on this machine"});
    expect(gone.text).toBe("run output exp_a missing");
    expect(gone.title).toContain("Only the bag is copied");
  });

  it("lists the bags and offers to delete them with no drive plugged in", async () => {
    const posts: [string, unknown][] = [];
    await mount([], (url, body) => { posts.push([url, body]); return json({job_id: "bag_delete-1", bytes: 30 * GB, not_on_drive: ["nov11_b45"]}); });
    const del = document.querySelector<HTMLButtonElement>('[data-testid="delete-bags-hamilton"]')!;
    const card = del.closest(".card")!;
    expect(card.textContent).toContain("No drive plugged in.");
    expect(card.textContent).toContain("nov11_b45");
    // nothing to copy to: no copy button, and delete waits for a selection
    expect([...card.querySelectorAll("button")].some(b => /^(Copy|Move) to/.test(b.textContent || ""))).toBe(false);
    expect(del.disabled).toBe(true);
    await act(async () => card.querySelector<HTMLInputElement>('input[aria-label="Select nov11_b45"]')!.click());
    expect(del.disabled).toBe(false);
    await act(async () => del.click());
    await flush();
    expect(posts).toEqual([["api/drives/delete", {machine: "hamilton", runs: ["nov11_b45"], drive: null}]]);
    // the typed confirmation is in the Jobs drawer, which opens
    expect(useJobs.getState().drawerOpen).toBe(true);
    // an offline machine has no list
    expect(document.querySelector('[data-testid="delete-bags-gauss"]')).toBeNull();
  });
});
