import { useEffect } from "react";
import { OFFHAND_SLOT, usedMainSlots } from "../types";
import type { GameState, InventoryItem } from "../types";
import { ItemIcon, DurabilityBar } from "./ItemIcon";
import { useItemIcons } from "../icons";

// Faithful in-game inventory layout: a 3x9 main grid above the 9-slot hotbar,
// armor column on the left (mainhand + 4 pieces), held slot outlined amber.
// Opens over the monitor; Esc or backdrop-click closes.

const ARMOR_SLOTS = [
  { key: "head", label: "Head" },
  { key: "chest", label: "Chest" },
  { key: "legs", label: "Legs" },
  { key: "feet", label: "Feet" },
] as const;

function Cell({
  item,
  held,
  lookup,
}: {
  item: InventoryItem | undefined;
  held: boolean;
  lookup: (name: string) => string | undefined;
}) {
  if (!item) return <div className="invc empty" />;
  return (
    <div
      className={`invc${held ? " held" : ""}`}
      title={`${item.name}${item.count > 1 ? ` ×${item.count}` : ""} · slot ${item.slot}`}
    >
      <ItemIcon name={item.name} size={44} lookup={lookup} />
      {item.count > 1 && <span className="ct">{item.count}</span>}
      {item.durability && <DurabilityBar {...item.durability} />}
    </div>
  );
}

function ArmorSlot({
  name,
  label,
  glyph,
  lookup,
  durability,
}: {
  name: string | null;
  label: string;
  glyph: string;
  lookup: (name: string) => string | undefined;
  durability?: { remaining: number; max: number };
}) {
  return (
    <div className={`aslot${name ? " filled" : ""}`} title={name ? `${label}: ${name}` : `${label}: empty`}>
      {name ? <ItemIcon name={name} size={44} lookup={lookup} /> : <span className="glyph">{glyph}</span>}
      {durability && <DurabilityBar {...durability} />}
    </div>
  );
}

export function InventoryModal({ game, onClose }: { game: GameState; onClose: () => void }) {
  const lookup = useItemIcons();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const bySlot = new Map<number, InventoryItem>();
  for (const it of game.inventory) bySlot.set(it.slot, it);
  const held = game.held_slot ?? -1;
  const equipped = game.equipped;
  const offhand = bySlot.get(OFFHAND_SLOT)?.name ?? null;
  const filled = usedMainSlots(game.inventory);
  // equipped carries names only; pull durability from the inventory array
  // (armor/offhand slots) by item name so worn gear shows wear too.
  const durByName = new Map<string, { remaining: number; max: number }>();
  for (const it of game.inventory) if (it.durability) durByName.set(it.name, it.durability);
  const dur = (n: string | null) => (n ? durByName.get(n) : undefined);

  return (
    <div className="inv-scrim" onClick={onClose}>
      <div className="inv-modal" onClick={(e) => e.stopPropagation()}>
        <div className="inv-modal-hd">
          <span className="ttl">Inventory</span>
          <span className="cnt">{filled} / 36 slots</span>
          <button className="x" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="inv-body">
          <div className="armorcol">
            <span className="col-lbl">Equipped</span>
            <ArmorSlot
              name={equipped?.hand ?? null}
              label="Mainhand"
              glyph="hand"
              lookup={lookup}
              durability={dur(equipped?.hand ?? null)}
            />
            <div className="armorgap" />
            {ARMOR_SLOTS.map((s) => (
              <ArmorSlot
                key={s.key}
                name={equipped?.[s.key] ?? null}
                label={s.label}
                glyph={s.label.toLowerCase()}
                lookup={lookup}
                durability={dur(equipped?.[s.key] ?? null)}
              />
            ))}
            <div className="armorgap" />
            <ArmorSlot name={offhand} label="Offhand" glyph="off" lookup={lookup} durability={dur(offhand)} />
          </div>

          <div className="gridcol">
            <div className="invgrid">
              {Array.from({ length: 27 }, (_, k) => (
                <Cell key={9 + k} item={bySlot.get(9 + k)} held={false} lookup={lookup} />
              ))}
            </div>
            <span className="col-lbl hotbar-lbl">Hotbar</span>
            <div className="invgrid">
              {Array.from({ length: 9 }, (_, k) => (
                <Cell key={k} item={bySlot.get(k)} held={k === held} lookup={lookup} />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
