// A pixel-art Minecraft item icon. Pure renderer — the caller passes a `lookup`
// from useItemIcons() (so a list of icons shares one hook subscription).

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
