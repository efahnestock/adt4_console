import {useEffect, useRef, useState} from "react";
import {ApiError, getJSON, postJSON} from "../lib/api";
import {withToken} from "../lib/auth";
import "./manipulationApproval.css";
import "./pickResult.css";

/** After a pick the robot believes worked, it asks whether it really holds the
 *  object and waits (with the object in the gripper) for one of these. */
export type PickAnswer = "picked" | "retry" | "give_up";

export interface PickQuestion {
  id: string;
  robot: string;
  object_id: string;
  object_class: string;
  /** 1 for the first pick of this object, 2 after one retry, ... */
  attempt: number;
  /** Spot's own gripper sensor (null: unknown). A hint, not the answer. */
  gripper_holding: boolean | null;
  images: {name: string; width: number; height: number}[];
  received_at: number;
}

interface Pending {
  available: boolean;
  error: string | null;
  requests: PickQuestion[];
}

function detail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

const CAMERA_LABELS: Record<string, string> = {
  hand_color_image: "Gripper camera",
  frontleft_fisheye_image: "Front left camera",
  frontright_fisheye_image: "Front right camera",
};

export function objectLabel(question: PickQuestion): string {
  const id = question.object_id || "the object";
  return question.object_class ? `${id} (${question.object_class})` : id;
}

const DONE: Record<PickAnswer, (q: PickQuestion) => string> = {
  picked: q => `${q.robot}: marked ${q.object_id || "the object"} as picked. The plan continues.`,
  retry: q => `${q.robot}: picking ${q.object_id || "the object"} again. It will ask for a grasp point, then for the result.`,
  give_up: q => `${q.robot}: pick of ${q.object_id || "the object"} given up. Its plan has ended.`,
};

/** Global host: the question pops up on whatever console page is open, because
 *  the robot stands waiting with the object. "Later" hides it behind a button. */
export function PickResultHost() {
  const [pending, setPending] = useState<Pending>({available: false, error: null, requests: []});
  const [connected, setConnected] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [answered, setAnswered] = useState<Set<string>>(new Set());
  const [later, setLater] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      try {
        const state = await getJSON<Pending>("api/pick_result/pending", controller.signal);
        if (controller.signal.aborted) return;
        setPending(state);
        setConnected(true);
        setFetchError(null);
      } catch (error) {
        if (controller.signal.aborted) return;
        setConnected(false);
        setFetchError(detail(error));
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 1000);
    };
    void poll();
    return () => { controller.abort(); if (timer !== undefined) window.clearTimeout(timer); };
  }, []);

  const requests = pending.requests.filter(request => !answered.has(request.id));
  const active = requests.find(request => !later.has(request.id));
  const hidden = requests.filter(request => later.has(request.id));

  const respond = async (question: PickQuestion, result: PickAnswer) => {
    await postJSON(`api/pick_result/requests/${encodeURIComponent(question.id)}/respond`, {result});
    setAnswered(old => new Set([...old, question.id]));
    setNotice(DONE[result](question));
  };

  return <>
    {notice && !active && <div className="manipulation-notice pick-result-notice" role="status">
      <span>{notice}</span>
      <button type="button" className="icon-btn" aria-label="Dismiss pick result status" onClick={() => setNotice(null)}>×</button>
    </div>}
    {!active && hidden.length > 0 && <button type="button"
      className="pick-result-pending btn btn-primary is-pulsing" data-testid="pick-result-open"
      title="A robot is waiting to hear whether its pick worked"
      onClick={() => setLater(new Set())}>
      {hidden.length === 1
        ? `Pick result: ${hidden[0].robot} is waiting — open`
        : `Pick results (${hidden.length}): ${hidden.map(q => q.robot).join(", ")} — open`}
    </button>}
    {active && <PickResultDialog key={active.id} question={active} waiting={requests.length - 1}
      available={connected && pending.available}
      serviceError={fetchError || pending.error}
      onLater={() => setLater(old => new Set([...old, active.id]))}
      onRespond={result => respond(active, result)} />}
  </>;
}

export function PickResultDialog({question, waiting = 0, available = true, serviceError = null, onRespond, onLater}: {
  question: PickQuestion;
  /** other robots' questions queued behind this one */
  waiting?: number;
  available?: boolean;
  serviceError?: string | null;
  onRespond: (result: PickAnswer) => Promise<void>;
  onLater: () => void;
}) {
  const [busy, setBusy] = useState<PickAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmGiveUp, setConfirmGiveUp] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => { dialog.current?.focus(); }, []);

  const send = async (result: PickAnswer) => {
    if (busy) return;
    setBusy(result);
    setError(null);
    try {
      await onRespond(result);
    } catch (problem) {
      setError(detail(problem));
      setBusy(null);
    }
  };
  const what = objectLabel(question);
  const disabled = Boolean(busy) || !available;

  return <div className="manipulation-backdrop pick-result-backdrop">
    <div ref={dialog} tabIndex={-1} className="manipulation-dialog pick-result-dialog" role="dialog" aria-modal="true"
      aria-labelledby="pick-result-title" data-testid="pick-result-dialog" onKeyDown={event => {
        event.stopPropagation();
        if (event.key === "Escape" && !busy) { event.preventDefault(); onLater(); }
      }}>
      <header className="manipulation-head">
        <div><h2 id="pick-result-title">Did {question.robot} pick up {what}?</h2>
          <p className="muted"><strong>{question.robot}</strong> thinks the pick worked and is waiting for your answer
            {question.attempt > 1 ? ` (attempt ${question.attempt})` : ""}.
            {waiting > 0 ? ` ${waiting} more robot${waiting === 1 ? " is" : "s are"} waiting.` : ""}</p></div>
        <button type="button" className="btn" disabled={Boolean(busy)} onClick={onLater}
          title="Hide this question for now. The robot keeps waiting.">Later</button>
      </header>
      <div className="manipulation-body">
        <p className={`pick-result-sensor ${question.gripper_holding === false ? "is-warn" : ""}`} data-testid="pick-result-sensor">
          {question.gripper_holding === true ? "Gripper sensor: holding something."
            : question.gripper_holding === false ? "Gripper sensor: holding nothing."
            : "Gripper sensor: no reading."}
        </p>
        {question.images.length > 0 ? <div className="pick-result-images">
          {question.images.map((image, index) => <figure key={image.name + index}>
            <img alt={`${CAMERA_LABELS[image.name] ?? image.name} after the pick`} draggable={false}
              src={withToken(`api/pick_result/requests/${encodeURIComponent(question.id)}/images/${index}`)} />
            <figcaption>{CAMERA_LABELS[image.name] ?? image.name}</figcaption>
          </figure>)}
        </div> : <p className="muted">The robot sent no camera images. Check its camera view before answering.</p>}
        {!available && <p className="manipulation-error" role="alert">
          {serviceError || "The console cannot reach the robots right now."}</p>}
        {error && <p className="manipulation-error" role="alert">{error}</p>}
      </div>
      <footer className="manipulation-foot pick-result-foot">
        {confirmGiveUp ? <div className="row pick-result-confirm" data-testid="pick-result-confirm">
          <span>Give up this pick? {question.robot} lets go and its plan ends here.</span>
          <button type="button" className="btn" disabled={Boolean(busy)} onClick={() => setConfirmGiveUp(false)}>Back</button>
          <button type="button" className="btn btn-danger" disabled={disabled} onClick={() => void send("give_up")}>
            {busy === "give_up" ? "Sending…" : "Give up the pick"}</button>
        </div> : <>
          <button type="button" className="btn btn-danger" disabled={disabled} onClick={() => setConfirmGiveUp(true)}
            title="The pick failed and should not be tried again: the robot lets go and its plan ends">Failed — give up</button>
          <div className="row">
            <button type="button" className="btn" disabled={disabled} onClick={() => void send("retry")}
              title="The pick failed: the robot lets go, stows the arm and picks again">
              {busy === "retry" ? "Sending…" : "Failed — pick again"}</button>
            <button type="button" className="btn btn-primary" disabled={disabled} onClick={() => void send("picked")}
              title="The robot holds the object: continue with the plan">
              {busy === "picked" ? "Sending…" : "Picked — continue"}</button>
          </div>
        </>}
      </footer>
    </div>
  </div>;
}
