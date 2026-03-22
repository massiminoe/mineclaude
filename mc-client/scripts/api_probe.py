"""Minescript API discovery — run as \\api_probe in game.

Enhanced for Phase 4: tests player-control, container, and inventory APIs.
Writes results to /tmp/api_probe_results.json for programmatic consumption.
"""
import json
import inspect
import minescript

results = {
    "all_apis": [],
    "player_control": {},
    "container": {},
    "inventory": {},
    "block": {},
    "entity": {},
    "notes": [],
}

print("=== Minescript API Probe (Phase 4) ===")

# Enumerate all public APIs
for name in sorted(dir(minescript)):
    if name.startswith("_"):
        continue
    obj = getattr(minescript, name)
    kind = type(obj).__name__
    if callable(obj):
        try:
            sig = str(inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(?)"
        print(f"  {name}{sig}  [{kind}]")
        results["all_apis"].append({"name": name, "signature": sig, "type": kind})
    else:
        print(f"  {name} = {obj!r}  [{kind}]")
        results["all_apis"].append({"name": name, "value": repr(obj), "type": kind})

# --- Player Control APIs (needed for break, place, attack) ---
print("\n=== Player Control APIs ===")

player_control_apis = [
    "player_set_orientation",
    "player_orientation",
    "player_press_attack",
    "player_press_use",
    "player_press_drop",
    "player_select_slot",
    "player_press_forward",
    "player_press_backward",
    "player_press_left",
    "player_press_right",
    "player_press_jump",
    "player_press_sneak",
    "player_press_sprint",
]

for api_name in player_control_apis:
    exists = hasattr(minescript, api_name)
    results["player_control"][api_name] = exists
    status = "OK" if exists else "MISSING"
    sig = ""
    if exists:
        try:
            sig = str(inspect.signature(getattr(minescript, api_name)))
        except (ValueError, TypeError):
            sig = "(?)"
    print(f"  {api_name}: {status} {sig}")

# Try calling orientation to verify it works
try:
    yaw, pitch = minescript.player_orientation()
    results["player_control"]["orientation_test"] = {"yaw": yaw, "pitch": pitch}
    print(f"  orientation_test: yaw={yaw:.1f}, pitch={pitch:.1f}")
except Exception as e:
    results["player_control"]["orientation_test"] = {"error": str(e)}
    print(f"  orientation_test: FAILED ({e})")

# --- Container APIs (needed for craft, equip) ---
print("\n=== Container / Screen APIs ===")

container_apis = [
    "screen_name",
    "container_get_items",
    "container_click",
    "close_screen",
    "player_press_inventory",
    "open_inventory",
]

for api_name in container_apis:
    exists = hasattr(minescript, api_name)
    results["container"][api_name] = exists
    status = "OK" if exists else "MISSING"
    sig = ""
    if exists:
        try:
            sig = str(inspect.signature(getattr(minescript, api_name)))
        except (ValueError, TypeError):
            sig = "(?)"
    print(f"  {api_name}: {status} {sig}")

# --- Inventory APIs ---
print("\n=== Inventory APIs ===")

inventory_apis = [
    "player_inventory",
    "player_hand_items",
    "player_select_slot",
]

for api_name in inventory_apis:
    exists = hasattr(minescript, api_name)
    results["inventory"][api_name] = exists
    status = "OK" if exists else "MISSING"
    sig = ""
    if exists:
        try:
            sig = str(inspect.signature(getattr(minescript, api_name)))
        except (ValueError, TypeError):
            sig = "(?)"
    print(f"  {api_name}: {status} {sig}")

# Test inventory
try:
    inv = minescript.player_inventory()
    items = []
    for item in inv:
        if item is not None:
            name = getattr(item, "item", None)
            if name and "air" not in str(name):
                items.append({"item": name, "count": getattr(item, "count", 0), "slot": getattr(item, "slot", -1)})
    results["inventory"]["test"] = {"count": len(items), "sample": items[:5]}
    print(f"  inventory_test: {len(items)} non-air items")
except Exception as e:
    results["inventory"]["test"] = {"error": str(e)}
    print(f"  inventory_test: FAILED ({e})")

# --- Block APIs ---
print("\n=== Block APIs ===")

block_apis = [
    "getblock",
    "getblocklist",
    "getblock_with_nbt",
]

for api_name in block_apis:
    exists = hasattr(minescript, api_name)
    results["block"][api_name] = exists
    status = "OK" if exists else "MISSING"
    print(f"  {api_name}: {status}")

# --- Entity APIs ---
print("\n=== Entity APIs ===")

entity_apis = [
    "entities",
    "player",
    "player_position",
    "player_health",
]

for api_name in entity_apis:
    exists = hasattr(minescript, api_name)
    results["entity"][api_name] = exists
    status = "OK" if exists else "MISSING"
    print(f"  {api_name}: {status}")

# --- Summary ---
print("\n=== Summary ===")
pc_available = sum(1 for v in results["player_control"].items() if isinstance(v[1], bool) and v[1])
pc_total = sum(1 for v in results["player_control"].items() if isinstance(v[1], bool))
ct_available = sum(1 for v in results["container"].items() if isinstance(v[1], bool) and v[1])
ct_total = sum(1 for v in results["container"].items() if isinstance(v[1], bool))

print(f"  Player control: {pc_available}/{pc_total} available")
print(f"  Container: {ct_available}/{ct_total} available")

can_break = all(results["player_control"].get(a, False) for a in ["player_set_orientation", "player_press_attack"])
can_place = all(results["player_control"].get(a, False) for a in ["player_set_orientation", "player_press_use", "player_select_slot"])
can_attack = can_break
can_craft = all(results["container"].get(a, False) for a in ["container_click", "container_get_items"])
can_equip = results["container"].get("container_click", False) and results["inventory"].get("player_inventory", False)
can_discard = results["player_control"].get("player_press_drop", False) and results["player_control"].get("player_select_slot", False)

results["capabilities"] = {
    "break_block": can_break,
    "place_block": can_place,
    "attack_entity": can_attack,
    "craft_item": can_craft,
    "equip_item": can_equip,
    "discard_item": can_discard,
}

print(f"\n  Capability: break_block = {can_break}")
print(f"  Capability: place_block = {can_place}")
print(f"  Capability: attack_entity = {can_attack}")
print(f"  Capability: craft_item = {can_craft}")
print(f"  Capability: equip_item = {can_equip}")
print(f"  Capability: discard_item = {can_discard}")

# Write results to file
with open("/tmp/api_probe_results.json", "w") as f:
    json.dump(results, f, indent=2, default=str)

print("\nResults written to /tmp/api_probe_results.json")
print("=== End Probe ===")
