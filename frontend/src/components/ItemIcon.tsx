// A pixel-art Minecraft item icon. Pure renderer — the caller passes a `lookup`
// from useItemIcons() (so a list of icons shares one hook subscription).

import { romanize, titleCase } from "../types";
import type { Enchantment } from "../types";

/** Falls back to a dimmed monogram square when no texture exists for the name
 *  (or the map is still loading), so a cell is never blank. */
export function ItemIcon({
  name,
  size = 32,
  lookup,
}: {
  name: string;
  size?: number;
  lookup: (name: string) => string | undefined;
}) {
  const src = lookup(name);
  if (src) {
    return (
      <img
        className="item-icon"
        src={src}
        alt={name}
        width={size}
        height={size}
        draggable={false}
      />
    );
  }
  const mono = name.replace(/^.*:/, "").replace(/_/g, " ").slice(0, 2);
  return (
    <span className="item-icon-fallback" style={{ width: size, height: size }}>
      {mono}
    </span>
  );
}

/** MC-style durability bar pinned to the bottom of a slot. Hidden when the item
 *  is undamaged (or has no durability). Colour shifts red→green with the ratio. */
export function DurabilityBar({ remaining, max }: { remaining: number; max: number }) {
  if (!max || remaining >= max) return null;
  const ratio = Math.max(0, Math.min(1, remaining / max));
  const hue = Math.round(ratio * 120); // 0 = red, 120 = green
  return (
    <span className="dura">
      <span className="dura-fill" style={{ width: `${ratio * 100}%`, background: `hsl(${hue} 75% 45%)` }} />
    </span>
  );
}

/** Flight-deck-styled hover popover listing an enchanted item's enchantments.
 *  Renders nothing without any — drop it inside any `position: relative` slot
 *  (`.invc` / `.aslot` / `.hbc` all already qualify) and it shows on `:hover`
 *  via CSS, no JS wiring needed. */
export function EnchantTip({ name, enchantments }: { name: string; enchantments: Enchantment[] | undefined }) {
  if (!enchantments || enchantments.length === 0) return null;
  return (
    <div className="ench-tip">
      <span className="ench-tip-lbl">Enchantments</span>
      <span className="ench-tip-item">{titleCase(name)}</span>
      {enchantments.map((e, i) => (
        <div className="ench-tip-line" key={i}>
          {titleCase(e.name)} {romanize(e.level)}
        </div>
      ))}
    </div>
  );
}
