"""Bridge client for communicating with the Minecraft bridge server."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
import websockets


@dataclass
class BridgeResponse:
    status: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BridgeClient(Protocol):
    async def get_status(self, include_events: bool = False) -> BridgeResponse: ...
    async def get_nearby_blocks(self, radius: int = 16, block_types: list[str] | None = None) -> BridgeResponse: ...
    async def get_nearby_entities(self, radius: int = 32) -> BridgeResponse: ...
    async def goto(self, x: float, z: float, y: float | None = None) -> BridgeResponse: ...
    async def mine(self, block: str, count: int = 1) -> BridgeResponse: ...
    async def follow(self, player: str, distance: int = 3) -> BridgeResponse: ...
    async def explore(self) -> BridgeResponse: ...
    async def stop(self) -> BridgeResponse: ...
    async def place(self, block: str, x: int, z: int, y: int | None = None) -> BridgeResponse: ...
    async def break_block(self, x: int, y: int, z: int) -> BridgeResponse: ...
    async def collect(self, radius: float = 3) -> BridgeResponse: ...
    async def attack(self, entity_id: str) -> BridgeResponse: ...
    async def attack_stop(self) -> BridgeResponse: ...
    async def craft(self, item: str, count: int = 1) -> BridgeResponse: ...
    async def furnace_load(
        self,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse: ...
    async def furnace_inspect(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse: ...
    async def furnace_extract(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse: ...
    async def chest_store(
        self,
        x: int,
        y: int,
        z: int,
        items: list[dict[str, Any]],
    ) -> BridgeResponse: ...
    async def chest_take(
        self,
        x: int,
        y: int,
        z: int,
        items: list[dict[str, Any]],
    ) -> BridgeResponse: ...
    async def chest_inspect(self, x: int, y: int, z: int) -> BridgeResponse: ...
    async def equip(self, item: str, slot: str = "hand") -> BridgeResponse: ...
    async def discard(self, slot: int, count: int = 1) -> BridgeResponse: ...
    async def chat(self, message: str) -> BridgeResponse: ...
    async def surface(self, timeout: float = 2.0) -> BridgeResponse: ...
    async def use_item(self, item: str, hold_ms: int | None = None) -> BridgeResponse: ...
    async def interact(self, x: int, y: int, z: int) -> BridgeResponse: ...
    async def heightmap(
        self,
        x0: int,
        z0: int,
        w: int,
        h: int,
        near_y: int | None = None,
    ) -> BridgeResponse: ...
    async def get_block(self, x: int, y: int, z: int) -> BridgeResponse: ...
    async def screenshot(
        self,
        yaw: float | None = None,
        pitch: float | None = None,
        look_at: tuple[float, float, float] | None = None,
    ) -> BridgeResponse: ...
    async def events(self, callback) -> None: ...
    async def record_roll(self, name: str | None = None) -> BridgeResponse: ...
    async def close(self) -> None: ...


# Halt endpoints (`/stop`, `/attack/stop`) are fire-and-forget: they flip a
# flag / interrupt a thread bridge-side and return immediately. They run on
# the preempt path (reflex/death), which has no other timeout — so they get a
# short dedicated deadline instead of the 90s client default. If the bridge is
# wedged we'd rather raise fast and let the caller proceed than block the whole
# agent waiting on a stop that can't be serviced. See the +780s deadlock in
# state/sessions/20260603-174507-8c32e18c.jsonl.
_HALT_TIMEOUT_S = 3.0


class RealBridgeClient:
    """HTTP/WS client for the native Fabric mod bridge.

    The mod owns every endpoint after the Phase 8 decommission of the
    legacy Minescript-backed Python bridge. HTTP lives on 8081 (JDK
    HttpServer); the events WS lives on 8082 (Java-WebSocket, separate
    listener because JDK HttpServer doesn't speak WS upgrades).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8081",
        ws_url: str = "ws://localhost:8082/events",
    ):
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        # 90s global timeout: must exceed the bridge's longest per-request
        # operation (e.g. /goto with a 60s default). A shorter client-side
        # timeout would cause spurious ReadTimeouts while the mod's
        # tick-thread executor is still working — wedging subsequent
        # requests behind the dropped one.
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=90.0)
        self._ws = None

    def _parse(self, resp: httpx.Response) -> BridgeResponse:
        data = resp.json()
        return BridgeResponse(
            status=data.get("status", "error"),
            message=data.get("message", ""),
            data=data.get("data", {}),
        )

    async def get_status(self, include_events: bool = False) -> BridgeResponse:
        # Event drain is opt-in. Only the agent's per-iteration injection
        # passes True; every other caller (reflex handlers, primitives,
        # monitor HUD poll) leaves the EventLog buffer alone so it accumulates
        # for the next gameState injection.
        params = {"include_events": "true"} if include_events else None
        return self._parse(await self._http.get("/status", params=params))

    async def get_nearby_blocks(self, radius: int = 16, block_types: list[str] | None = None) -> BridgeResponse:
        params: dict = {"r": radius}
        if block_types:
            params["types"] = ",".join(block_types)
        return self._parse(await self._http.get("/nearby/blocks", params=params))

    async def get_nearby_entities(self, radius: int = 32) -> BridgeResponse:
        return self._parse(await self._http.get("/nearby/entities", params={"r": radius}))

    async def goto(self, x: float, z: float, y: float | None = None) -> BridgeResponse:
        body: dict[str, Any] = {"x": x, "z": z}
        if y is not None:
            body["y"] = y
        return self._parse(await self._http.post("/goto", json=body))

    async def mine(self, block: str, count: int = 1) -> BridgeResponse:
        return self._parse(await self._http.post("/mine", json={"block": block, "count": count}))

    async def follow(self, player: str, distance: int = 3) -> BridgeResponse:
        return self._parse(await self._http.post("/follow", json={"player": player, "distance": distance}))

    async def explore(self) -> BridgeResponse:
        return self._parse(await self._http.post("/explore"))

    async def stop(self) -> BridgeResponse:
        return self._parse(await self._http.post("/stop", timeout=_HALT_TIMEOUT_S))

    async def place(self, block: str, x: int, z: int, y: int | None = None) -> BridgeResponse:
        body: dict[str, Any] = {"block": block, "x": x, "z": z}
        if y is not None:
            body["y"] = y
        return self._parse(await self._http.post("/place", json=body))

    async def break_block(self, x: int, y: int, z: int) -> BridgeResponse:
        return self._parse(await self._http.post("/break", json={"x": x, "y": y, "z": z}))

    async def collect(self, radius: float = 3) -> BridgeResponse:
        return self._parse(await self._http.post("/collect", json={"radius": radius}))

    async def attack(self, entity_id: str) -> BridgeResponse:
        return self._parse(await self._http.post("/attack", json={"entity_id": entity_id}))

    async def attack_stop(self) -> BridgeResponse:
        return self._parse(await self._http.post("/attack/stop", timeout=_HALT_TIMEOUT_S))

    async def craft(self, item: str, count: int = 1) -> BridgeResponse:
        return self._parse(await self._http.post("/craft", json={"item": item, "count": count}))

    async def furnace_load(
        self,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse:
        body: dict[str, Any] = {
            "input_item": input_item,
            "input_count": input_count,
            "fuel_item": fuel_item,
            "fuel_count": fuel_count,
        }
        if x is not None and y is not None and z is not None:
            body["x"], body["y"], body["z"] = x, y, z
        return self._parse(await self._http.post("/furnace/load", json=body))

    async def furnace_inspect(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse:
        params: dict[str, Any] = {}
        if x is not None and y is not None and z is not None:
            params["x"], params["y"], params["z"] = x, y, z
        return self._parse(await self._http.get("/furnace/inspect", params=params))

    async def furnace_extract(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse:
        body: dict[str, Any] = {}
        if x is not None and y is not None and z is not None:
            body["x"], body["y"], body["z"] = x, y, z
        return self._parse(await self._http.post("/furnace/extract", json=body))

    async def chest_store(
        self,
        x: int,
        y: int,
        z: int,
        items: list[dict[str, Any]],
    ) -> BridgeResponse:
        return self._parse(await self._http.post(
            "/chest/store",
            json={"x": x, "y": y, "z": z, "items": items},
        ))

    async def chest_take(
        self,
        x: int,
        y: int,
        z: int,
        items: list[dict[str, Any]],
    ) -> BridgeResponse:
        return self._parse(await self._http.post(
            "/chest/take",
            json={"x": x, "y": y, "z": z, "items": items},
        ))

    async def chest_inspect(self, x: int, y: int, z: int) -> BridgeResponse:
        return self._parse(await self._http.get(
            "/chest/inspect",
            params={"x": x, "y": y, "z": z},
        ))

    async def equip(self, item: str, slot: str = "hand") -> BridgeResponse:
        return self._parse(await self._http.post("/equip", json={"item": item, "slot": slot}))

    async def discard(self, slot: int, count: int = 1) -> BridgeResponse:
        return self._parse(await self._http.post("/discard", json={"slot": slot, "count": count}))

    async def chat(self, message: str) -> BridgeResponse:
        return self._parse(await self._http.post("/chat", json={"message": message}))

    async def surface(self, timeout: float = 2.0) -> BridgeResponse:
        return self._parse(await self._http.post("/surface", json={"timeout": timeout}))

    async def use_item(self, item: str, hold_ms: int | None = None) -> BridgeResponse:
        body: dict[str, Any] = {"item": item}
        if hold_ms is not None:
            body["hold_ms"] = hold_ms
        return self._parse(await self._http.post("/use_item", json=body))

    async def interact(self, x: int, y: int, z: int) -> BridgeResponse:
        return self._parse(await self._http.post("/interact", json={"x": x, "y": y, "z": z}))

    async def heightmap(
        self,
        x0: int,
        z0: int,
        w: int,
        h: int,
        near_y: int | None = None,
    ) -> BridgeResponse:
        params: dict[str, str] = {"x0": str(x0), "z0": str(z0), "w": str(w), "h": str(h)}
        if near_y is not None:
            params["near_y"] = str(near_y)
        return self._parse(await self._http.get("/heightmap", params=params))

    async def get_block(self, x: int, y: int, z: int) -> BridgeResponse:
        return self._parse(await self._http.get("/block", params={"x": str(x), "y": str(y), "z": str(z)}))

    async def screenshot(
        self,
        yaw: float | None = None,
        pitch: float | None = None,
        look_at: tuple[float, float, float] | None = None,
    ) -> BridgeResponse:
        params: dict[str, str] = {"format": "jpeg", "quality": "80"}
        if look_at is not None:
            if yaw is not None or pitch is not None:
                return BridgeResponse("error", "pass either yaw/pitch or look_at, not both")
            params["look_at_x"] = str(look_at[0])
            params["look_at_y"] = str(look_at[1])
            params["look_at_z"] = str(look_at[2])
        else:
            if yaw is not None:
                params["yaw"] = str(yaw)
            if pitch is not None:
                params["pitch"] = str(pitch)
        return self._parse(await self._http.get("/screenshot", params=params))

    async def events(self, callback) -> None:
        """Connect to WS event stream with reconnection backoff."""
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    backoff = 1.0
                    async for raw in ws:
                        event = json.loads(raw)
                        await callback(event)
            except (websockets.ConnectionClosed, websockets.InvalidMessage, OSError):
                self._ws = None
                jitter = random.uniform(0, backoff * 0.5)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 30.0)

    async def record_roll(self, name: str | None = None) -> BridgeResponse:
        # Cut a fresh gameplay-recording file (see RecordRoute in the bridge
        # mod). No-op on the mod side if nothing is recording, so callers can
        # fire this blindly regardless of RECORD_VIDEO. `name` labels the file.
        body = {} if name is None else {"name": name}
        return self._parse(await self._http.post("/record/roll", json=body))

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
        await self._http.aclose()


class MockBridgeClient:
    """Simulates bridge for local testing without Minecraft."""

    def __init__(self):
        self._position = {"x": 0.0, "y": 64.0, "z": 0.0}
        # Chest contents keyed by (x, y, z). Mock treats every chest as a
        # 27-slot single chest with no stack-size cap; tests assert on
        # counts and item presence rather than slot layout.
        self._chests: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
        self._health = 20.0
        self._hunger = 20
        self._inventory: list[dict] = []
        self._nearby_blocks: list[dict] = [
            {"name": "grass_block", "x": 1, "y": 64, "z": 0, "distance": 1.0},
            {"name": "dirt", "x": 0, "y": 63, "z": 0, "distance": 1.0},
            {"name": "oak_log", "x": 5, "y": 64, "z": 3, "distance": 5.8},
            {"name": "oak_log", "x": 5, "y": 65, "z": 3, "distance": 5.9},
            {"name": "oak_log", "x": 5, "y": 66, "z": 3, "distance": 6.1},
        ]
        self._nearby_entities: list[dict] = [
            {"name": "Steve", "type": "player", "x": 10, "y": 64, "z": 5, "distance": 11.2, "health": 20},
        ]
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._chat_log: list[str] = []
        self._running = True

    async def get_status(self, include_events: bool = False) -> BridgeResponse:
        data: dict[str, Any] = {
            "position": self._position.copy(),
            "health": self._health,
            "hunger": self._hunger,
            "inventory": list(self._inventory),
            "biome": "plains",
            "time": 6000,
        }
        if include_events:
            data["events"] = []
        return BridgeResponse("success", "Status retrieved", data)

    async def get_nearby_blocks(self, radius: int = 16, block_types: list[str] | None = None) -> BridgeResponse:
        blocks = [b for b in self._nearby_blocks if b["distance"] <= radius]
        if block_types:
            type_set = set(block_types)
            blocks = [b for b in blocks if b["name"] in type_set]
        return BridgeResponse("success", f"Found {len(blocks)} blocks", {"blocks": blocks})

    async def get_nearby_entities(self, radius: int = 32) -> BridgeResponse:
        entities = [e for e in self._nearby_entities if e["distance"] <= radius]
        return BridgeResponse("success", f"Found {len(entities)} entities", {"entities": entities})

    async def goto(self, x: float, z: float, y: float | None = None) -> BridgeResponse:
        # Mock: pretend the heightmap puts feet at y=64 unless caller pinned it.
        resolved_y = y if y is not None else 64.0
        self._position = {"x": x, "y": resolved_y, "z": z}
        return BridgeResponse("success", f"Moved to {x}, {resolved_y}, {z}")

    async def mine(self, block: str, count: int = 1) -> BridgeResponse:
        collected = 0
        remaining = []
        for b in self._nearby_blocks:
            if b["name"] == block and collected < count:
                collected += 1
                self._add_to_inventory(block, 1)
            else:
                remaining.append(b)
        self._nearby_blocks = remaining
        return BridgeResponse("success", f"Collected {collected} {block}", {"collected": collected})

    async def follow(self, player: str, distance: int = 3) -> BridgeResponse:
        for e in self._nearby_entities:
            if e["name"] == player:
                self._position = {"x": e["x"] - distance, "y": e["y"], "z": e["z"]}
                return BridgeResponse("success", f"Following {player}")
        return BridgeResponse("error", f"Player {player} not found")

    async def explore(self) -> BridgeResponse:
        self._position["x"] += 50
        return BridgeResponse("success", "Exploring")

    async def stop(self) -> BridgeResponse:
        return BridgeResponse("success", "Stopped")

    async def place(self, block: str, x: int, z: int, y: int | None = None) -> BridgeResponse:
        resolved_y = y if y is not None else 64
        removed = self._remove_from_inventory(block, 1)
        if not removed:
            return BridgeResponse("error", f"No {block} in inventory")
        self._nearby_blocks.append({"name": block, "x": x, "y": resolved_y, "z": z, "distance": 1.0})
        return BridgeResponse("success", f"Placed {block} at {x}, {resolved_y}, {z}")

    async def break_block(self, x: int, y: int, z: int) -> BridgeResponse:
        for b in self._nearby_blocks:
            if b["x"] == x and b["y"] == y and b["z"] == z:
                self._nearby_blocks.remove(b)
                # Spawn dropped item entity (like real MC) — use collect() to pick up
                self._nearby_entities.append({
                    "name": b["name"], "type": "item",
                    "x": x + 0.5, "y": y, "z": z + 0.5,
                    "distance": b.get("distance", 1.0), "health": 0,
                })
                return BridgeResponse("success", f"Broke {b['name']} at {x}, {y}, {z}")
        return BridgeResponse("error", f"No block at {x}, {y}, {z}")

    async def collect(self, radius: float = 3) -> BridgeResponse:
        # Find all item entities within radius of the player and pick them up
        px = self._position["x"]
        py = self._position["y"]
        pz = self._position["z"]
        to_collect = []
        for e in self._nearby_entities:
            if e["type"] != "item":
                continue
            dist = math.sqrt((e["x"] - px) ** 2 + (e["y"] - py) ** 2 + (e["z"] - pz) ** 2)
            if dist <= radius:
                to_collect.append(e)
        for e in to_collect:
            self._nearby_entities.remove(e)
            self._add_to_inventory(e["name"], 1)
            # Simulate walking to the item
            self._position = {"x": e["x"], "y": e["y"], "z": e["z"]}
        count = len(to_collect)
        msg = f"Collected {count} item(s)" if count else "No items to collect"
        return BridgeResponse("success", msg, {"collected": count})

    async def attack(self, entity_id: str) -> BridgeResponse:
        # Loop swings until the target is dead, despawns, or `attack_stop`
        # is called — mirrors the real bridge's looping /attack so
        # MOCK_BRIDGE=1 exercises the same agent contract.
        self._attack_cancelled = False
        swings = 0
        max_swings = 100  # safety bound so a stuck mock can't spin forever
        while swings < max_swings:
            await asyncio.sleep(0)
            if self._attack_cancelled:
                return BridgeResponse(
                    "success", f"Attack cancelled after {swings} swings",
                    {"attacked": swings > 0, "swings": swings, "reason": "cancelled", "method": "simulated"},
                )
            target = next(
                (e for e in self._nearby_entities if e["name"] == entity_id or str(e.get("id", "")) == entity_id),
                None,
            )
            if target is None:
                if swings == 0:
                    return BridgeResponse(
                        "error", f"Entity {entity_id} not found",
                        {"attacked": False, "swings": 0, "reason": "not_found", "method": "simulated"},
                    )
                return BridgeResponse(
                    "error", f"Target {entity_id} despawned after {swings} swings",
                    {"attacked": True, "swings": swings, "reason": "despawned", "method": "simulated"},
                )
            target["health"] = max(0, target.get("health", 5) - 5)
            swings += 1
            if target["health"] <= 0:
                self._nearby_entities.remove(target)
                return BridgeResponse(
                    "success", f"Killed {entity_id} in {swings} swings",
                    {"attacked": True, "swings": swings, "reason": "killed", "method": "simulated"},
                )
        return BridgeResponse(
            "error", f"Attack timed out after {swings} swings",
            {"attacked": True, "swings": swings, "reason": "timeout", "method": "simulated"},
        )

    async def attack_stop(self) -> BridgeResponse:
        was_running = not getattr(self, "_attack_cancelled", True)
        self._attack_cancelled = True
        return BridgeResponse(
            "success",
            "Attack cancelled" if was_running else "No attack in progress",
            {"cancelled": was_running},
        )

    async def craft(self, item: str, count: int = 1) -> BridgeResponse:
        from mineclaude.recipes import (
            format_required_ingredients,
            get_recipe,
            get_required_ingredients,
            resolve_ingredients,
        )

        item = item.replace("minecraft:", "")
        recipe = get_recipe(item)
        if recipe is None:
            return BridgeResponse("error", f"Unknown recipe: {item}. Cannot craft without a known recipe.", {"crafted": 0, "method": "simulated"})

        if recipe.needs_table:
            # Match real bridge scan radius (see bridge/minescript_api.py _craft_via_table).
            has_table = any(
                b["name"] == "crafting_table" and b["distance"] <= 16
                for b in self._nearby_blocks
            )
            if not has_table:
                return BridgeResponse("error", f"Cannot craft {item}: no crafting table nearby. Place one first.", {"crafted": 0, "method": "simulated"})

        required = get_required_ingredients(item, count)
        if required is None:
            return BridgeResponse("error", f"Cannot calculate ingredients for {item}", {"crafted": 0, "method": "simulated"})

        # Check inventory (with variant matching)
        have: dict[str, int] = {}
        for entry in self._inventory:
            have[entry["name"]] = have.get(entry["name"], 0) + entry["count"]

        resolved = resolve_ingredients(required, have)
        if resolved is None:
            need_str = format_required_ingredients(required)
            have_str = ", ".join(f"{v}x {k}" for k, v in have.items()) if have else "nothing"
            msg = f"Cannot craft {item}: missing ingredients. Need: {need_str}. Have: {have_str}."
            return BridgeResponse("error", msg, {"crafted": 0, "method": "simulated"})

        # Consume resolved actual items
        for actual_item, needed in resolved.items():
            self._remove_from_inventory(actual_item, needed)

        # Produce output
        crafts_needed = math.ceil(count / recipe.output_count)
        total_output = crafts_needed * recipe.output_count
        self._add_to_inventory(item, total_output)

        return BridgeResponse("success", f"Crafted {total_output} {item}", {"crafted": total_output, "method": "simulated"})

    def _find_furnace(self, x: int | None, y: int | None, z: int | None) -> dict | None:
        if x is not None and y is not None and z is not None:
            for b in self._nearby_blocks:
                if b["name"] in ("furnace", "lit_furnace") and b["x"] == x and b["y"] == y and b["z"] == z:
                    return b
            return None
        for b in self._nearby_blocks:
            if b["name"] in ("furnace", "lit_furnace") and b["distance"] <= 16:
                return b
        return None

    def _furnace_state(self, b: dict) -> dict:
        """Lazily attach mutable slot state to a mock furnace block."""
        return b.setdefault("_state", {
            "input": {"item": None, "count": 0},
            "fuel": {"item": None, "count": 0},
            "output": {"item": None, "count": 0},
        })

    async def furnace_load(
        self,
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse:
        b = self._find_furnace(x, y, z)
        if b is None:
            return BridgeResponse("error", "No furnace nearby. Place one first.")
        # Pull the stated amounts from inventory; fail if either is short.
        if not self._remove_from_inventory(input_item, input_count):
            return BridgeResponse(
                "error",
                f"Not enough {input_item} in inventory (need {input_count})",
            )
        if not self._remove_from_inventory(fuel_item, fuel_count):
            # Refund the input we already pulled so the call is atomic.
            self._add_to_inventory(input_item, input_count)
            return BridgeResponse(
                "error",
                f"Not enough {fuel_item} in inventory (need {fuel_count})",
            )
        # Simulate smelting: produce output equal to min(input_count, fuel_count * fuel_value).
        # Mock fuel value is hardcoded to 1.5 for parity with planks/log; good enough
        # for tests that exercise the round-trip.
        from mineclaude.recipes import get_smelting_by_input
        recipe = get_smelting_by_input(input_item)
        state = self._furnace_state(b)
        if recipe is not None:
            produced = min(input_count, int(fuel_count * 1.5))
            state["output"] = {"item": recipe.output, "count": produced}
            state["input"] = {"item": input_item, "count": max(0, input_count - produced)}
            state["fuel"] = {"item": fuel_item, "count": 0}
        else:
            # No known recipe — treat as nothing smelts. Inputs sit in slots.
            state["input"] = {"item": input_item, "count": input_count}
            state["fuel"] = {"item": fuel_item, "count": fuel_count}
        b["name"] = "lit_furnace" if recipe is not None else "furnace"
        return BridgeResponse(
            "success",
            f"Loaded {input_count} {input_item} and {fuel_count} {fuel_item} into furnace",
            {
                "loaded_input": input_count,
                "loaded_fuel": fuel_count,
                "position": {"x": b["x"], "y": b["y"], "z": b["z"]},
                "method": "simulated",
            },
        )

    async def furnace_inspect(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse:
        b = self._find_furnace(x, y, z)
        if b is None:
            return BridgeResponse("error", "No furnace nearby.")
        state = self._furnace_state(b)
        return BridgeResponse(
            "success",
            "Furnace inspected",
            {
                "position": {"x": b["x"], "y": b["y"], "z": b["z"]},
                "lit": b["name"] == "lit_furnace",
                "cook_progress": 0.0,
                "fuel_remaining_ticks": 0,
                "input": dict(state["input"]),
                "fuel": dict(state["fuel"]),
                "output": dict(state["output"]),
                "method": "simulated",
            },
        )

    async def furnace_extract(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> BridgeResponse:
        b = self._find_furnace(x, y, z)
        if b is None:
            return BridgeResponse("error", "No furnace nearby.")
        state = self._furnace_state(b)
        out = dict(state["output"])
        in_left = dict(state["input"])
        fuel_left = dict(state["fuel"])
        # Move everything back to inventory.
        for slot in (out, in_left, fuel_left):
            if slot["item"] and slot["count"] > 0:
                self._add_to_inventory(slot["item"], slot["count"])
        # Reset slot state.
        state["input"] = {"item": None, "count": 0}
        state["fuel"] = {"item": None, "count": 0}
        state["output"] = {"item": None, "count": 0}
        b["name"] = "furnace"
        return BridgeResponse(
            "success",
            f"Extracted {out['count']} {out['item'] or 'nothing'} from furnace",
            {
                "position": {"x": b["x"], "y": b["y"], "z": b["z"]},
                "output": out,
                "input_left": in_left,
                "fuel_left": fuel_left,
                "method": "simulated",
            },
        )

    def _resolve_chest(self, x: int, y: int, z: int) -> tuple[bool, str]:
        """(ok, msg). True if (x,y,z) points at a chest block. Mock treats any
        nearby_blocks entry named 'chest' or 'trapped_chest' at those coords
        as a valid chest and lazily allocates an empty contents list."""
        for b in self._nearby_blocks:
            if b["x"] == x and b["y"] == y and b["z"] == z:
                if b["name"] in ("chest", "trapped_chest"):
                    self._chests.setdefault((x, y, z), [])
                    return True, ""
                return False, f"Block at ({x}, {y}, {z}) is '{b['name']}', not a chest."
        return False, f"No block at ({x}, {y}, {z})."

    async def chest_store(
        self,
        x: int,
        y: int,
        z: int,
        items: list[dict[str, Any]],
    ) -> BridgeResponse:
        ok, msg = self._resolve_chest(x, y, z)
        if not ok:
            return BridgeResponse("error", msg)
        contents = self._chests[(x, y, z)]
        stored: list[dict] = []
        skipped: list[dict] = []
        for entry in items:
            name = entry["name"]
            spec = entry.get("count", "all")
            have = sum(e["count"] for e in self._inventory if e["name"] == name)
            target = have if spec == "all" else min(int(spec), have)
            if target <= 0:
                skipped.append({"item": name, "reason": "not in inventory", "requested": spec})
                continue
            self._remove_from_inventory(name, target)
            existing = next((c for c in contents if c["name"] == name), None)
            if existing is None:
                contents.append({"name": name, "count": target})
            else:
                existing["count"] += target
            stored.append({"item": name, "count": target})
        return BridgeResponse(
            "success",
            f"Stored {sum(s['count'] for s in stored)} item(s)",
            {
                "position": {"x": x, "y": y, "z": z},
                "stored": stored,
                "skipped": skipped,
                "method": "simulated",
            },
        )

    async def chest_take(
        self,
        x: int,
        y: int,
        z: int,
        items: list[dict[str, Any]],
    ) -> BridgeResponse:
        ok, msg = self._resolve_chest(x, y, z)
        if not ok:
            return BridgeResponse("error", msg)
        contents = self._chests[(x, y, z)]
        taken: list[dict] = []
        skipped: list[dict] = []
        for entry in items:
            name = entry["name"]
            spec = entry.get("count", "all")
            existing = next((c for c in contents if c["name"] == name), None)
            have = existing["count"] if existing else 0
            target = have if spec == "all" else min(int(spec), have)
            if target <= 0:
                skipped.append({"item": name, "reason": "not in chest", "requested": spec})
                continue
            existing["count"] -= target
            if existing["count"] == 0:
                contents.remove(existing)
            self._add_to_inventory(name, target)
            taken.append({"item": name, "count": target})
        return BridgeResponse(
            "success",
            f"Took {sum(t['count'] for t in taken)} item(s)",
            {
                "position": {"x": x, "y": y, "z": z},
                "taken": taken,
                "skipped": skipped,
                "method": "simulated",
            },
        )

    async def chest_inspect(self, x: int, y: int, z: int) -> BridgeResponse:
        ok, msg = self._resolve_chest(x, y, z)
        if not ok:
            return BridgeResponse("error", msg)
        contents = self._chests[(x, y, z)]
        slots = [{"slot": i, "item": c["name"], "count": c["count"]} for i, c in enumerate(contents)]
        totals = {c["name"]: c["count"] for c in contents}
        return BridgeResponse(
            "success",
            f"Chest at ({x}, {y}, {z}) — 27 slots, {len(totals)} item types",
            {
                "position": {"x": x, "y": y, "z": z},
                "size": 27,
                "slots": slots,
                "totals": totals,
                "method": "simulated",
            },
        )

    async def equip(self, item: str, slot: str = "hand") -> BridgeResponse:
        for entry in self._inventory:
            if entry["name"] == item:
                return BridgeResponse("success", f"Equipped {item} to {slot}")
        return BridgeResponse("error", f"No {item} in inventory")

    async def discard(self, slot: int, count: int = 1) -> BridgeResponse:
        if slot not in range(0, 36):
            return BridgeResponse("error", f"slot {slot} out of range (0..35)")
        if count <= 0:
            return BridgeResponse("error", "count must be >= 1")
        entry = next((e for e in self._inventory if e.get("slot") == slot), None)
        if entry is None:
            return BridgeResponse("error", f"Slot {slot} is empty")
        item = entry["name"]
        dropped = min(entry["count"], count)
        entry["count"] -= dropped
        if entry["count"] == 0:
            self._inventory.remove(entry)
        return BridgeResponse(
            "success", f"Discarded {dropped} {item}",
            {"discarded": dropped, "item": item, "method": "simulated"},
        )

    async def chat(self, message: str) -> BridgeResponse:
        self._chat_log.append(message)
        return BridgeResponse("success", f"Sent: {message}")

    async def surface(self, timeout: float = 2.0) -> BridgeResponse:
        return BridgeResponse("success", "Surfaced", {"surfaced": True, "ticks": 0})

    async def use_item(self, item: str, hold_ms: int | None = None) -> BridgeResponse:
        # Mock: require the item to be in inventory (mirror equip's check).
        # Food items consume one stack and bump hunger toward full so a
        # NO_CLAUDE eat-loop test settles. Other items just succeed.
        item = item.replace("minecraft:", "")
        if not any(e["name"] == item for e in self._inventory):
            return BridgeResponse("error", f"No {item} in inventory")
        FOODS = {
            "bread": 5, "cooked_beef": 8, "apple": 4, "carrot": 3,
            "cooked_chicken": 6, "cooked_porkchop": 8, "cookie": 2,
            "golden_apple": 4, "honey_bottle": 6, "dried_kelp": 1,
        }
        if item in FOODS:
            self._remove_from_inventory(item, 1)
            self._hunger = min(20, self._hunger + FOODS[item])
        return BridgeResponse(
            "success", f"Used {item}",
            {"used": True, "item": item, "hold_ms": hold_ms, "method": "simulated"},
        )

    async def interact(self, x: int, y: int, z: int) -> BridgeResponse:
        for b in self._nearby_blocks:
            if b["x"] == x and b["y"] == y and b["z"] == z:
                return BridgeResponse(
                    "success", f"Interacted with {b['name']} at ({x}, {y}, {z})",
                    {"interacted": True, "target": b["name"], "method": "simulated"},
                )
        return BridgeResponse(
            "error", f"Nothing to interact with at ({x}, {y}, {z}) — block is air",
        )

    async def heightmap(
        self,
        x0: int,
        z0: int,
        w: int,
        h: int,
        near_y: int | None = None,
    ) -> BridgeResponse:
        # Mock: pretend the floor is at y=63 everywhere → standable y=64.
        ny = near_y if near_y is not None else int(self._position["y"])
        ys = [[64 for _ in range(w)] for _ in range(h)]
        floor = [["grass_block" for _ in range(w)] for _ in range(h)]
        return BridgeResponse(
            "success",
            f"Scanned {w * h} cells, {w * h} standable",
            {"x0": x0, "z0": z0, "w": w, "h": h, "near_y": ny, "ys": ys, "floor": floor},
        )

    async def get_block(self, x: int, y: int, z: int) -> BridgeResponse:
        # Mock: any cell present in _nearby_blocks reports its name; otherwise
        # air (replaceable).
        for b in self._nearby_blocks:
            if b["x"] == x and b["y"] == y and b["z"] == z:
                return BridgeResponse(
                    "success",
                    f"{b['name']} at ({x}, {y}, {z})",
                    {"block": b["name"], "replaceable": False},
                )
        return BridgeResponse(
            "success",
            f"air at ({x}, {y}, {z})",
            {"block": "air", "replaceable": True},
        )

    async def screenshot(
        self,
        yaw: float | None = None,
        pitch: float | None = None,
        look_at: tuple[float, float, float] | None = None,
    ) -> BridgeResponse:
        import base64
        # 1x1 red pixel JPEG
        dummy = base64.b64encode(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9').decode()
        return BridgeResponse("success", "Screenshot captured", {
            "image": dummy, "format": "jpeg", "width": 854, "height": 480, "size_bytes": 23,
        })

    async def events(self, callback) -> None:
        """Process events from the mock event queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                await callback(event)
            except asyncio.TimeoutError:
                continue

    async def record_roll(self, name: str | None = None) -> BridgeResponse:
        # No recorder in mock mode; report idle so callers treat it as a no-op.
        return BridgeResponse("success", "not recording", {"recording": False})

    async def close(self) -> None:
        self._running = False

    # --- Test helpers ---

    def inject_chat(self, username: str, message: str) -> None:
        """Push a chat event into the event queue for testing."""
        self._event_queue.put_nowait({
            "type": "chat",
            "data": {"username": username, "message": message},
        })

    def inject_event(self, event_type: str, data: dict) -> None:
        self._event_queue.put_nowait({"type": event_type, "data": data})

    def _add_to_inventory(self, item: str, count: int) -> None:
        for entry in self._inventory:
            if entry["name"] == item:
                entry["count"] += count
                return
        self._inventory.append({"name": item, "count": count, "slot": len(self._inventory)})

    def _remove_from_inventory(self, item: str, count: int) -> bool:
        # Check total available first
        total = sum(e["count"] for e in self._inventory if e["name"] == item)
        if total < count:
            return False
        remaining = count
        to_remove = []
        for entry in self._inventory:
            if entry["name"] == item and remaining > 0:
                take = min(entry["count"], remaining)
                entry["count"] -= take
                remaining -= take
                if entry["count"] == 0:
                    to_remove.append(entry)
        for entry in to_remove:
            self._inventory.remove(entry)
        return True


def create_bridge(
    mock: bool = False,
    base_url: str = "http://localhost:8081",
    ws_url: str = "ws://localhost:8082/events",
) -> BridgeClient:
    if mock:
        return MockBridgeClient()
    return RealBridgeClient(base_url, ws_url=ws_url)
