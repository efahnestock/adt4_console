// While a trial records, what the operator does on the page goes into the
// trial's event log (API.md §23): clicks on controls, page changes, dialogs
// opening and closing. Text typed into fields is never logged.
import {postJSON} from "./api";
import {useTrial} from "../stores/trial";

export interface UiEvent {
  kind: "click" | "page" | "dialog_open" | "dialog_close" | "change";
  label?: string;
  page?: string;
  detail?: string;
  t_client: number;
}

const CONTROL = "button,a,[role=button],[role=tab],[role=menuitem],summary,select,input[type=checkbox],input[type=radio]";
const clean = (s: string | null | undefined, n = 120) => (s ?? "").replace(/\s+/g, " ").trim().slice(0, n);

/** How a clicked control is named in the log: its aria-label, test id, text or title. */
export function describeTarget(el: Element | null): {label: string; detail?: string} | null {
  const c = el?.closest?.(CONTROL);
  if (!c) return null;
  const label = clean(c.getAttribute("aria-label")) || clean(c.getAttribute("data-testid")) || clean(c.textContent) || clean(c.getAttribute("title")) || c.tagName.toLowerCase();
  const dialog = c.closest("[role=dialog]");
  const where = dialog ? clean(dialog.getAttribute("aria-label") || dialog.querySelector("h2")?.textContent, 80) : "";
  return where ? {label, detail: `in dialog: ${where}`} : {label};
}

/** A dialog's title and the start of its text. */
export function describeDialog(d: Element): {label: string; detail: string} {
  return {label: clean(d.getAttribute("aria-label") || d.querySelector("h2")?.textContent, 120) || "dialog", detail: clean(d.textContent, 400)};
}

const buffer: UiEvent[] = [];
const recording = () => useTrial.getState().payload?.active != null;
const page = () => window.location.hash.replace(/^#/, "") || "/";

export function record(kind: UiEvent["kind"], f: {label?: string; detail?: string} = {}) {
  if (!recording()) return;
  buffer.push({kind, page: page(), t_client: Date.now() / 1000, ...f});
  if (buffer.length > 500) buffer.splice(0, buffer.length - 500);
}

export async function flushUiLog() {
  if (!buffer.length) return;
  const events = buffer.splice(0, 200);
  try {
    await postJSON("api/trials/ui", {events});
  } catch {
    if (recording()) buffer.unshift(...events); // keep them for the next try
  }
}

export function bindUiLog() {
  document.addEventListener(
    "click",
    e => {
      const d = describeTarget(e.target as Element);
      if (d) record("click", d);
    },
    true,
  );
  document.addEventListener(
    "change",
    e => {
      const t = e.target as HTMLInputElement | HTMLSelectElement;
      if (t instanceof HTMLSelectElement) record("change", {label: clean(t.getAttribute("aria-label") || t.name) || "select", detail: clean(t.value, 80)});
      else if (t instanceof HTMLInputElement && (t.type === "checkbox" || t.type === "radio")) record("change", {label: clean(t.getAttribute("aria-label") || t.name) || t.type, detail: String(t.checked)});
    },
    true,
  );
  window.addEventListener("hashchange", () => record("page"));
  const dialogs = (n: Node): Element[] => (n instanceof Element ? [...(n.matches("[role=dialog]") ? [n] : []), ...n.querySelectorAll("[role=dialog]")] : []);
  new MutationObserver(muts => {
    for (const m of muts) {
      m.addedNodes.forEach(n => dialogs(n).forEach(d => record("dialog_open", describeDialog(d))));
      m.removedNodes.forEach(n => dialogs(n).forEach(d => record("dialog_close", {label: describeDialog(d).label})));
    }
  }).observe(document.body, {childList: true, subtree: true});
  window.setInterval(() => void flushUiLog(), 2000);
  document.addEventListener("visibilitychange", () => document.visibilityState === "hidden" && void flushUiLog());
}
