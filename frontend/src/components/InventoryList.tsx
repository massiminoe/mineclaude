interface Props {
  inventory: { name: string; count: number; slot: number }[];
}

export function InventoryList({ inventory }: Props) {
  if (!inventory || inventory.length === 0) {
    return <div className="empty-state">Empty</div>;
  }

  return (
    <div className="inventory-list">
      {inventory.map((item) => (
        <div key={item.slot} className="inventory-row">
          <span className="inventory-name">{item.name}</span>
          <span className="inventory-count">x{item.count}</span>
        </div>
      ))}
    </div>
  );
}
