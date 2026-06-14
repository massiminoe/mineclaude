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
