// The chat conversation, outside the ChatPanel so it survives switching pages
// (the panel unmounts) and a reload or another browser tab (localStorage).
// A turn still streaming when the panel unmounts keeps writing here.
import {create} from "zustand";
import type {ChatItem} from "../viewer/ChatPanel";
import type {TranscriptItem} from "../lib/chatTranscript";

export const CHAT_STORAGE_KEY = "adt4.chat.v1";
const MAX_ITEMS = 300;

interface ChatStore {
  items: ChatItem[];
  /** what the model sees next turn (server-built transcript) */
  transcript: TranscriptItem[];
  busy: boolean;
  /** index of the current turn's first item (scroll anchor) */
  turnStart: number;
  /** text to put in the message box (e.g. "Re-plan in chat"); the panel takes it */
  prefill: string | null;
  /** the unsent message box text: here, not in the panel, because the Chat tab
   *  unmounts when a click on the map brings Inspect forward (and on any other
   *  tab / pane switch); the docked tab and the mission planner's chat pane
   *  are the same conversation, so they share one draft */
  draft: string;
  /** bumped by insertIntoChat: a mounted chat box moves its caret to `caret` */
  insertSeq: number;
}

/** What is kept on disk: no busy flag, and no embedded candidate images (data
 *  URIs can be hundreds of kB; a candidate's pending id expires anyway). */
export function storable(items: ChatItem[]): ChatItem[] {
  return items.slice(-MAX_ITEMS).map(it => (it.type === "object_candidate" && it.image_uri ? {...it, image_uri: null} : it));
}

function load(): Pick<ChatStore, "items" | "transcript"> {
  try {
    const raw = window.localStorage.getItem(CHAT_STORAGE_KEY);
    const v = raw ? JSON.parse(raw) : null;
    if (v && Array.isArray(v.items) && Array.isArray(v.transcript)) return {items: v.items, transcript: v.transcript};
  } catch {
    /* private window / bad data */
  }
  return {items: [], transcript: []};
}

export const useChat = create<ChatStore>(() => ({...load(), busy: false, turnStart: 0, prefill: null, draft: "", insertSeq: 0}));

export function setChatDraft(draft: string): void {
  useChat.setState({draft});
}

/** Where the next insertion goes: the chat box's last caret / selection
 *  (the box is unfocused by the time an Inspect button is pressed, and may be
 *  unmounted). null: the end of the draft. */
let caret: {start: number; end: number} | null = null;

export function noteChatCaret(start: number, end: number): void {
  caret = {start, end};
}

export function chatCaret(): {start: number; end: number} | null {
  return caret;
}

/** `token` into `text` in place of [start, end), with a space on either side
 *  unless one is already there (or it is the start / end of the text). */
export function insertToken(text: string, token: string, start = text.length, end = start): {text: string; caret: number} {
  const s = Math.max(0, Math.min(start, text.length));
  const e = Math.max(s, Math.min(end, text.length));
  const before = text.slice(0, s);
  const after = text.slice(e);
  const pre = before && !/\s$/.test(before) ? " " : "";
  const post = after && !/^\s/.test(after) ? " " : after ? "" : " ";
  const out = before + pre + token + post + after;
  return {text: out, caret: (before + pre + token + post).length};
}

/** Insert a node symbol ("O(12)") into the chat draft at the caret: the chat
 *  cannot see map clicks, so Inspect hands it the symbol. */
export function insertIntoChat(token: string): void {
  const draft = useChat.getState().draft;
  const at = caret && caret.end <= draft.length ? caret : {start: draft.length, end: draft.length};
  const r = insertToken(draft, token, at.start, at.end);
  caret = {start: r.caret, end: r.caret};
  useChat.setState(st => ({draft: r.text, insertSeq: st.insertSeq + 1}));
}

/** Put `text` in the chat's message box (the mission planner shows the chat pane). */
export function prefillChat(text: string): void {
  useChat.setState({prefill: text});
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
useChat.subscribe((s, prev) => {
  if (s.items === prev.items && s.transcript === prev.transcript) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      window.localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify({items: storable(s.items), transcript: s.transcript}));
    } catch {
      /* quota / private window: the in-memory chat still works */
    }
  }, 300);
});

/** The in-flight request, so "New chat" can stop it from any mount. */
export const chatAbort: {current: AbortController | null} = {current: null};

export function clearChat(): void {
  chatAbort.current?.abort();
  chatAbort.current = null;
  useChat.setState({items: [], transcript: [], busy: false, turnStart: 0});
}
