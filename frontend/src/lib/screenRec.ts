// A video of the console page for a trial (API.md §23): the browser records
// the tab at 10 fps and sends it to the console in pieces, which appends them
// to the trial's console/screen_<session>.webm. Nothing here uses the radio.
import {create} from "zustand";
import {postJSON, request} from "./api";
import {useTrial} from "../stores/trial";

export const SCREEN_FPS = 10;
const CHUNK_MS = 5000;
const BITS_PER_SECOND = 2_500_000;
const PREF_KEY = "adt4.trial.screen.v1";

export type ScreenStatus = "off" | "recording" | "denied" | "ended" | "error" | "unsupported";
interface ScreenState {
  status: ScreenStatus;
  bytes: number;
  session: string | null;
  detail: string;
}
export const useScreenRec = create<ScreenState>(() => ({status: "off", bytes: 0, session: null, detail: ""}));

/** Record the screen with each trial? On unless switched off in this browser. */
export function screenPref(): boolean {
  try {
    return window.localStorage.getItem(PREF_KEY) !== "0";
  } catch {
    return true;
  }
}
export function setScreenPref(on: boolean) {
  try {
    window.localStorage.setItem(PREF_KEY, on ? "1" : "0");
  } catch {
    /* private window: the choice holds for this page only */
  }
}

export const screenSupported = () => typeof navigator !== "undefined" && !!navigator.mediaDevices?.getDisplayMedia && typeof MediaRecorder !== "undefined";

/** Why this page cannot record the screen, and what to do about it. Browsers
 *  only share the screen with a page on localhost or https; a console opened
 *  from another computer as http://<base station>:8090 is neither. */
export function remoteHint(loc: {hostname: string; port: string} = window.location): string {
  const port = loc.port || "80";
  return `The browser only shares the screen with a page on localhost or https, and this page is on http://${loc.hostname}. To record it, on this computer run "ssh -L ${port}:localhost:${port} <user>@${loc.hostname}" and open http://localhost:${port} instead.`;
}

export function pickMime(supported: (m: string) => boolean = m => MediaRecorder.isTypeSupported(m)): string {
  return ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm", "video/mp4"].find(supported) ?? "";
}

/** Sends pieces one at a time, in order; a piece that fails is retried, then
 *  skipped (the server logs the gap) so later ones still arrive. */
export function makeUploader(send: (seq: number, data: Blob, final: boolean) => Promise<void>, onError: (e: unknown) => void, retries = 3, waitMs = 1000) {
  let seq = 0;
  let chain: Promise<void> = Promise.resolve();
  return (data: Blob, final = false): Promise<void> => {
    const mine = seq++;
    chain = chain.then(async () => {
      for (let i = 0; ; i++) {
        try {
          return await send(mine, data, final);
        } catch (e) {
          if (i >= retries) return onError(e);
          await new Promise(r => setTimeout(r, waitMs));
        }
      }
    });
    return chain;
  };
}

/** Ask the browser for this tab. Must be called from a click. Null when the
 *  browser cannot, or the operator said no (the status says which). */
export async function acquireScreen(): Promise<MediaStream | null> {
  if (!screenSupported()) {
    useScreenRec.setState({status: "unsupported", detail: remoteHint()});
    return null;
  }
  try {
    return await navigator.mediaDevices.getDisplayMedia({
      video: {frameRate: {ideal: SCREEN_FPS, max: SCREEN_FPS}},
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: "include",
    } as DisplayMediaStreamOptions);
  } catch (e) {
    useScreenRec.setState({status: "denied", detail: (e as Error)?.message ?? "permission denied"});
    return null;
  }
}

let recorder: MediaRecorder | null = null;
let stream: MediaStream | null = null;

export function note(what: string, detail = "") {
  void postJSON("api/trials/screen/note", {session: useScreenRec.getState().session ?? "", what, detail}).catch(() => undefined);
}

export function startScreenRecording(s: MediaStream) {
  stopScreenRecording();
  const mime = pickMime();
  const session = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const upload = makeUploader(
    async (seq, data, final) => {
      const q = new URLSearchParams({session, seq: String(seq), mime: mime || "video/webm", final: String(final), t_client: String(Date.now() / 1000)});
      await request(`api/trials/screen?${q}`, {method: "POST", body: data, headers: {"Content-Type": "application/octet-stream"}});
      useScreenRec.setState(st => (st.session === session ? {bytes: st.bytes + data.size} : st));
    },
    e => useScreenRec.setState(st => (st.session === session ? {status: "error", detail: `a piece of the video was not saved: ${(e as Error)?.message ?? e}`} : st)),
  );
  const rec = new MediaRecorder(s, {mimeType: mime || undefined, videoBitsPerSecond: BITS_PER_SECOND});
  rec.ondataavailable = e => {
    if (e.data.size) void upload(e.data);
  };
  rec.onstop = () => void upload(new Blob([]), true);
  rec.onerror = () => useScreenRec.setState({status: "error", detail: "the browser's recorder failed"});
  s.getVideoTracks()[0]?.addEventListener("ended", () => {
    // the operator pressed the browser's "Stop sharing"
    if (recorder === rec) {
      useScreenRec.setState({status: "ended", detail: "screen sharing was stopped in the browser"});
      note("ended", "screen sharing was stopped in the browser");
      if (rec.state !== "inactive") rec.stop();
      recorder = stream = null;
    }
  });
  recorder = rec;
  stream = s;
  useScreenRec.setState({status: "recording", bytes: 0, session, detail: ""});
  rec.start(CHUNK_MS);
}

export function stopScreenRecording() {
  const rec = recorder;
  const s = stream;
  recorder = stream = null;
  if (rec && rec.state !== "inactive") rec.stop();
  s?.getTracks().forEach(t => t.stop());
  if (rec) useScreenRec.setState({status: "off", detail: ""});
}

/** The recording ends with the trial. */
export function bindScreenRec() {
  useTrial.subscribe(st => {
    const t = st.payload?.active;
    if (recorder && (!t || t.state === "stopping")) stopScreenRecording();
    if (!t && useScreenRec.getState().status !== "off") useScreenRec.setState({status: "off", detail: "", session: null});
  });
}
