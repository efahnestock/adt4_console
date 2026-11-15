import {create} from "zustand";
import {ws} from "../ws";

/** Who has the console open now (`console/viewers`): distinct viewers (by
 *  address; one browser's tabs count once) and live connections. */
export interface Viewers {
  viewers: number;
  tabs: number;
}

export const useViewers = create<Viewers | {viewers: null; tabs: null}>(() => ({viewers: null, tabs: null}));

export function bindViewers() {
  ws.on("console/viewers", d => {
    const v = d as Partial<Viewers> | null;
    if (v && typeof v.viewers === "number") useViewers.setState({viewers: v.viewers, tabs: v.tabs ?? v.viewers});
  });
}
