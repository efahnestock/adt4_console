import {pickLayer, type TileLayer} from "../lib/geo";
import {useMission} from "../stores/mission";
import {Toggle} from "../components/ui";

/** Picker label: the attribution up to its parenthesised details, else the layer id. */
export function layerLabel(l: TileLayer): string {
  const a = (l.attribution || "").split("(")[0].trim().replace(/[,;]$/, "");
  const name = a ? (a.length > 48 ? `${a.slice(0, 47)}…` : a) : l.id;
  return l.live && !l.cached ? `${name} (online)` : name;
}

/** Ground imagery under the map (Layers panel): on/off, which tile layer, opacity.
 *  Only for a map with a GPS anchor (no anchor: nothing to place the tiles with). */
export function GroundImageryControls() {
  const geo = useMission(s => s.geo);
  const layers = useMission(s => s.layers);
  const on = useMission(s => s.satellite);
  const opacity = useMission(s => s.satOpacity);
  const satLayer = useMission(s => s.satLayer);
  const set = useMission(s => s.set);
  if (!geo || !layers.length) return null;
  const shown = pickLayer(layers, satLayer);
  return (
    <div className="ground-imagery" data-testid="ground-imagery">
      <header className="vsec-head">
        <h4>Ground imagery</h4>
        <Toggle checked={on} onChange={v => set({satellite: v})} label="show" />
      </header>
      <select
        className="input input-sm"
        data-testid="sat-layer"
        value={shown?.id ?? ""}
        onChange={e => set({satLayer: e.target.value || null})}
        aria-label="Ground imagery layer"
        title={shown?.attribution || "Imagery drawn under the map"}
        style={{display: "block", width: "100%", minWidth: 0, margin: "2px 0 6px"}}
      >
        {layers.map(l => (
          <option key={l.id} value={l.id} title={l.attribution}>
            {layerLabel(l)}
          </option>
        ))}
      </select>
      <label className="slider-row">
        <span>Opacity</span>
        <input type="range" min={0.2} max={1} step={0.05} value={opacity} onChange={e => set({satOpacity: Number(e.target.value)})} aria-label="Ground imagery opacity" />
      </label>
    </div>
  );
}
