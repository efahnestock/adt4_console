// Which robot's status card is open (viewer/RobotStatusCard.tsx): opened by
// tapping the robot in the 3D map, its name tag or its legend dot.
import {create} from "zustand";

interface RobotStatusStore {
  open: string | null;
  show: (robot: string) => void;
  toggle: (robot: string) => void;
  close: () => void;
}

export const useRobotStatus = create<RobotStatusStore>(set => ({
  open: null,
  show: robot => set({open: robot}),
  toggle: robot => set(s => ({open: s.open === robot ? null : robot})),
  close: () => set({open: null}),
}));
