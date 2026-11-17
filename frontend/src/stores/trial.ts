import {create} from "zustand";
import {ws} from "../ws";
import {getJSON} from "../lib/api";
import type {TrialPayload} from "../lib/trial";

interface TrialStore {
  payload: TrialPayload | null;
  /** this browser's clock (ms) when `payload` arrived */
  receivedAt: number;
  set: (p: TrialPayload) => void;
  load: () => Promise<void>;
}

export const useTrial = create<TrialStore>((set, get) => ({
  payload: null,
  receivedAt: 0,
  set: p => set({payload: p, receivedAt: Date.now()}),
  load: async () => {
    try {
      get().set(await getJSON<TrialPayload>("api/trials"));
    } catch {
      /* no trials on this server, or it is unreachable: the bar says so */
    }
  },
}));

/** The console server's clock now: its `now` plus the time since it arrived. */
export function serverNow(p: TrialPayload, receivedAt: number, clientNowMs = Date.now()): number {
  return p.now + (clientNowMs - receivedAt) / 1000;
}

export function bindTrial() {
  ws.on("trial/state", d => {
    if (d && typeof d === "object") useTrial.getState().set(d as TrialPayload);
  });
  ws.onReconnect(() => void useTrial.getState().load());
}
