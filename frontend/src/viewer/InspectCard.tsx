// A picked node, inspected over the map instead of in the Inspect tab: used
// while the chat is open where that tab would replace it (ViewerPage,
// paneLayout inspectPlacement), so the operator can look at objects and drop
// their symbols into the message without losing the chat, its scroll or draft.
// The head carries the node and its "insert into chat" action, so it is useful
// even where only the head fits (a phone held sideways).
import type {ReactNode} from "react";

export function InspectCard({title, sym, onInsert, onClose, onExpand, expandLabel, children}: {title: string; sym: string; onInsert: (sym: string) => void; onClose: () => void; onExpand: () => void; expandLabel: string; children: ReactNode}) {
  return (
    <section className="inspect-card" aria-label={`Inspect ${title}`} data-testid="inspect-card">
      <header className="inspect-card-head">
        <div className="inspect-card-title" title={`${title} · ${sym}`}>
          <span className="inspect-card-name">{title}</span> <span className="mono small muted">{sym}</span>
        </div>
        <button className="btn btn-xs btn-primary" onClick={() => onInsert(sym)} title={`Type ${sym} into the chat message at the cursor (the chat cannot see map clicks)`} data-testid="inspect-card-insert">
          Insert in chat
        </button>
        <button className="btn btn-xs" onClick={onExpand} title="Show it in the Inspect tab instead (the chat folds away)" data-testid="inspect-card-expand">
          {expandLabel}
        </button>
        <button className="icon-btn sm" onClick={onClose} aria-label="Close inspect card" data-testid="inspect-card-close">
          ×
        </button>
      </header>
      <div className="inspect-card-body">{children}</div>
    </section>
  );
}
