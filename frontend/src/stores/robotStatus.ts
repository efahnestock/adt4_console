// Which robot's status card is open (viewer/RobotStatusCard.tsx): opened by
// tapping the robot in the 3D map, its name tag or its legend dot.
import {create} from "zustand";
import type {Phase} from "../lib/readiness";

interface RobotStatusStore {
  open: string | null;
  show: (robot: string) => void;
  toggle: (robot: string) => void;
  close: () => void;
  /** the operator's choice of what the rings judge (null: automatic) */
  phase: Phase | null;
  setPhase: (p: Phase | null) => void;
}

export const useRobotStatus = create<RobotStatusStore>(set => ({
  open: null,
  show: robot => set({open: robot}),
  toggle: robot => set(s => ({open: s.open === robot ? null : robot})),
  close: () => set({open: null}),
  phase: null,
  setPhase: phase => set({phase}),
}));
