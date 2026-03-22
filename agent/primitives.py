"""Sandbox primitives — async functions exposed to LLM-generated code."""

from __future__ import annotations

import asyncio
from typing import Any

from agent.bridge import BridgeClient

# Shared log buffer, cleared before each sandbox execution
_log_buffer: list[str] = []


def make_primitives(bridge: BridgeClient) -> dict[str, Any]:
    """Create a dict of name → async callable primitives, closed over bridge."""

    async def goToPosition(x: float, y: float, z: float) -> str:
        resp = await bridge.goto(x, y, z)
        return resp.message

    async def goToPlayer(player: str, distance: int = 3) -> str:
        resp = await bridge.follow(player, distance)
        return resp.message

    async def followPlayer(player: str, distance: int = 3) -> str:
        resp = await bridge.follow(player, distance)
        return resp.message

    async def stop() -> str:
        resp = await bridge.stop()
        return resp.message

    async def placeBlock(block_type: str, x: int, y: int, z: int, face: str = "top") -> str:
        resp = await bridge.place(block_type, x, y, z, face)
        return resp.message

    async def breakBlockAt(x: int, y: int, z: int) -> str:
        resp = await bridge.break_block(x, y, z)
        return resp.message

    async def attackNearest(mob_type: str) -> str:
        resp = await bridge.attack(mob_type)
        return resp.message

    async def defendSelf() -> str:
        resp = await bridge.attack("hostile")
        return resp.message

    async def craft(item: str, count: int = 1) -> str:
        resp = await bridge.craft(item, count)
        return resp.message

    async def equip(item: str, slot: str = "hand") -> str:
        resp = await bridge.equip(item, slot)
        return resp.message

    async def discard(item: str, count: int = 1) -> str:
        resp = await bridge.discard(item, count)
        return resp.message

    async def getStats() -> dict:
        resp = await bridge.get_status()
        return resp.data

    async def getInventory() -> list[dict]:
        resp = await bridge.get_status()
        return resp.data.get("inventory", [])

    async def getNearbyBlocks(range_: int = 16) -> list[dict]:
        resp = await bridge.get_nearby_blocks(range_)
        return resp.data.get("blocks", [])

    async def getNearbyEntities(range_: int = 32) -> list[dict]:
        resp = await bridge.get_nearby_entities(range_)
        return resp.data.get("entities", [])

    async def findBlocks(block_type: str, range_: int = 64, count: int = 10) -> list[dict]:
        resp = await bridge.get_nearby_blocks(range_)
        blocks = resp.data.get("blocks", [])
        return [b for b in blocks if b["name"] == block_type][:count]

    async def findEntities(entity_type: str, range_: int = 32) -> list[dict]:
        resp = await bridge.get_nearby_entities(range_)
        entities = resp.data.get("entities", [])
        return [e for e in entities if e["type"] == entity_type or e["name"] == entity_type]

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    def log(message: str) -> None:
        _log_buffer.append(str(message))

    return {
        "goToPosition": goToPosition,
        "goToPlayer": goToPlayer,
        "followPlayer": followPlayer,
        "stop": stop,
        "placeBlock": placeBlock,
        "breakBlockAt": breakBlockAt,
        "attackNearest": attackNearest,
        "defendSelf": defendSelf,
        "craft": craft,
        "equip": equip,
        "discard": discard,
        "getStats": getStats,
        "getInventory": getInventory,
        "getNearbyBlocks": getNearbyBlocks,
        "getNearbyEntities": getNearbyEntities,
        "findBlocks": findBlocks,
        "findEntities": findEntities,
        "sleep": sleep,
        "log": log,
    }
