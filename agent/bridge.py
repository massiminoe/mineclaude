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
    async def get_status(self) -> BridgeResponse: ...
    async def get_nearby_blocks(self, radius: int = 16, block_types: list[str] | None = None) -> BridgeResponse: ...
    async def get_nearby_entities(self, radius: int = 32) -> BridgeResponse: ...
    async def goto(self, x: float, y: float, z: float) -> BridgeResponse: ...
    async def mine(self, block: str, count: int = 1) -> BridgeResponse: ...
    async def follow(self, player: str, distance: int = 3) -> BridgeResponse: ...
    async def explore(self) -> BridgeResponse: ...
    async def stop(self) -> BridgeResponse: ...
    async def place(self, block: str, x: int, y: int, z: int, face: str = "top") -> BridgeResponse: ...
    async def break_block(self, x: int, y: int, z: int) -> BridgeResponse: ...
    async def attack(self, entity_id: str) -> BridgeResponse: ...
    async def craft(self, item: str, count: int = 1) -> BridgeResponse: ...
    async def equip(self, item: str, slot: str = "hand") -> BridgeResponse: ...
    async def discard(self, item: str, count: int = 1) -> BridgeResponse: ...
    async def chat(self, message: str) -> BridgeResponse: ...
    async def events(self, callback) -> None: ...
    async def close(self) -> None: ...


class RealBridgeClient:
    """HTTP/WS client for the real bridge server."""

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url.rstrip("/")
        self.ws_url = self.base_url.replace("http", "ws") + "/events"
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self._ws = None
        self._ws_task = None

    def _parse(self, resp: httpx.Response) -> BridgeResponse:
        data = resp.json()
        return BridgeResponse(
            status=data.get("status", "error"),
            message=data.get("message", ""),
            data=data.get("data", {}),
        )

    async def get_status(self) -> BridgeResponse:
        return self._parse(await self._http.get("/status"))

    async def get_nearby_blocks(self, radius: int = 16, block_types: list[str] | None = None) -> BridgeResponse:
        params: dict = {"r": radius}
        if block_types:
            params["types"] = ",".join(block_types)
        return self._parse(await self._http.get("/nearby/blocks", params=params))

    async def get_nearby_entities(self, radius: int = 32) -> BridgeResponse:
        return self._parse(await self._http.get("/nearby/entities", params={"r": radius}))

    async def goto(self, x: float, y: float, z: float) -> BridgeResponse:
        return self._parse(await self._http.post("/goto", json={"x": x, "y": y, "z": z}))

    async def mine(self, block: str, count: int = 1) -> BridgeResponse:
        return self._parse(await self._http.post("/mine", json={"block": block, "count": count}))

    async def follow(self, player: str, distance: int = 3) -> BridgeResponse:
        return self._parse(await self._http.post("/follow", json={"player": player, "distance": distance}))

    async def explore(self) -> BridgeResponse:
        return self._parse(await self._http.post("/explore"))

    async def stop(self) -> BridgeResponse:
        return self._parse(await self._http.post("/stop"))

    async def place(self, block: str, x: int, y: int, z: int, face: str = "top") -> BridgeResponse:
        return self._parse(await self._http.post("/place", json={"block": block, "x": x, "y": y, "z": z, "face": face}))

    async def break_block(self, x: int, y: int, z: int) -> BridgeResponse:
        return self._parse(await self._http.post("/break", json={"x": x, "y": y, "z": z}))

    async def attack(self, entity_id: str) -> BridgeResponse:
        return self._parse(await self._http.post("/attack", json={"entity_id": entity_id}))

    async def craft(self, item: str, count: int = 1) -> BridgeResponse:
        return self._parse(await self._http.post("/craft", json={"item": item, "count": count}))

    async def equip(self, item: str, slot: str = "hand") -> BridgeResponse:
        return self._parse(await self._http.post("/equip", json={"item": item, "slot": slot}))

    async def discard(self, item: str, count: int = 1) -> BridgeResponse:
        return self._parse(await self._http.post("/discard", json={"item": item, "count": count}))

    async def chat(self, message: str) -> BridgeResponse:
        return self._parse(await self._http.post("/chat", json={"message": message}))

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

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
        await self._http.aclose()


class MockBridgeClient:
    """Simulates bridge for local testing without Minecraft."""

    def __init__(self):
        self._position = {"x": 0.0, "y": 64.0, "z": 0.0}
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

    async def get_status(self) -> BridgeResponse:
        return BridgeResponse("success", "Status retrieved", {
            "position": self._position.copy(),
            "health": self._health,
            "hunger": self._hunger,
            "inventory": list(self._inventory),
            "biome": "plains",
            "time": 6000,
        })

    async def get_nearby_blocks(self, radius: int = 16, block_types: list[str] | None = None) -> BridgeResponse:
        blocks = [b for b in self._nearby_blocks if b["distance"] <= radius]
        if block_types:
            type_set = set(block_types)
            blocks = [b for b in blocks if b["name"] in type_set]
        return BridgeResponse("success", f"Found {len(blocks)} blocks", {"blocks": blocks})

    async def get_nearby_entities(self, radius: int = 32) -> BridgeResponse:
        entities = [e for e in self._nearby_entities if e["distance"] <= radius]
        return BridgeResponse("success", f"Found {len(entities)} entities", {"entities": entities})

    async def goto(self, x: float, y: float, z: float) -> BridgeResponse:
        self._position = {"x": x, "y": y, "z": z}
        return BridgeResponse("success", f"Moved to {x}, {y}, {z}")

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

    async def place(self, block: str, x: int, y: int, z: int, face: str = "top") -> BridgeResponse:
        removed = self._remove_from_inventory(block, 1)
        if not removed:
            return BridgeResponse("error", f"No {block} in inventory")
        self._nearby_blocks.append({"name": block, "x": x, "y": y, "z": z, "distance": 1.0})
        return BridgeResponse("success", f"Placed {block} at {x}, {y}, {z}")

    async def break_block(self, x: int, y: int, z: int) -> BridgeResponse:
        for b in self._nearby_blocks:
            if b["x"] == x and b["y"] == y and b["z"] == z:
                self._nearby_blocks.remove(b)
                self._add_to_inventory(b["name"], 1)
                return BridgeResponse("success", f"Broke {b['name']} at {x}, {y}, {z}")
        return BridgeResponse("error", f"No block at {x}, {y}, {z}")

    async def attack(self, entity_id: str) -> BridgeResponse:
        for e in self._nearby_entities:
            if e["name"] == entity_id:
                e["health"] = max(0, e["health"] - 5)
                return BridgeResponse("success", f"Attacked {entity_id}")
        return BridgeResponse("error", f"Entity {entity_id} not found")

    async def craft(self, item: str, count: int = 1) -> BridgeResponse:
        from agent.recipes import get_recipe, get_required_ingredients, resolve_ingredients

        item = item.replace("minecraft:", "")
        recipe = get_recipe(item)
        if recipe is None:
            return BridgeResponse("error", f"Unknown recipe: {item}. Cannot craft without a known recipe.", {"crafted": 0, "method": "simulated"})

        required = get_required_ingredients(item, count)
        if required is None:
            return BridgeResponse("error", f"Cannot calculate ingredients for {item}", {"crafted": 0, "method": "simulated"})

        # Check inventory (with variant matching)
        have: dict[str, int] = {}
        for entry in self._inventory:
            have[entry["name"]] = have.get(entry["name"], 0) + entry["count"]

        resolved = resolve_ingredients(required, have)
        if resolved is None:
            need_str = ", ".join(f"{v}x {k}" for k, v in required.items())
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

    async def equip(self, item: str, slot: str = "hand") -> BridgeResponse:
        for entry in self._inventory:
            if entry["name"] == item:
                return BridgeResponse("success", f"Equipped {item} to {slot}")
        return BridgeResponse("error", f"No {item} in inventory")

    async def discard(self, item: str, count: int = 1) -> BridgeResponse:
        removed = self._remove_from_inventory(item, count)
        if removed:
            return BridgeResponse("success", f"Discarded {count} {item}")
        return BridgeResponse("error", f"No {item} in inventory")

    async def chat(self, message: str) -> BridgeResponse:
        self._chat_log.append(message)
        return BridgeResponse("success", f"Sent: {message}")

    async def events(self, callback) -> None:
        """Process events from the mock event queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                await callback(event)
            except asyncio.TimeoutError:
                continue

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


def create_bridge(mock: bool = False, base_url: str = "http://localhost:8080") -> BridgeClient:
    if mock:
        return MockBridgeClient()
    return RealBridgeClient(base_url)
