"""Background world state cache for the bridge.

Maintains block/entity/status data in memory, updated by a daemon scanner
thread that paces its RPC calls to avoid monopolizing Minescript's stdin/
stdout pipe. HTTP GET handlers read from this cache instead of triggering
on-demand scans, eliminating the 10+ second executor-blocking scans that
used to starve command handlers and corrupt the RPC channel.

Accuracy model:
- Scanner re-scans blocks every ~10s OR immediately on player movement >8 blocks
- Bot's own break/place operations are write-through (instant cache update)
- Commands (break/place/attack) still do live getblock verification before acting
- Status and entities poll every 2s (cheap, single RPC each)
- _ms_lock is released between scanner chunks → command threads interleave

Thread safety: all cache reads and writes go through _lock. The scanner
thread holds _ms_lock briefly per RPC call and releases between calls, so
the executor thread running commands can grab _ms_lock in the gaps.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

from bridge import minescript_api

logger = logging.getLogger("bridge")

# Full re-scan cadence (seconds) when the player is stationary. Bumped
# from the original 10s → 20s to roughly halve background pipe traffic:
# the Java-side stdout-writer race fires under sustained RPC pressure,
# and the scanner is by far the largest producer of that pressure.
BLOCK_SCAN_INTERVAL = 20.0

# Movement threshold — re-scan immediately if player has moved this far
# from the last scan center. Bumped 8 → 12 for the same reason.
MOVEMENT_RESCAN_THRESHOLD = 12.0

# Status/entity poll cadence (seconds) — these are single cheap RPCs
STATUS_POLL_INTERVAL = 2.0
ENTITY_POLL_INTERVAL = 2.0

# Scanner loop tick (seconds) — how often the loop wakes up to check
# whether any update is due
LOOP_TICK = 0.5

# Scan radius used for the background block cache. Agent queries at
# smaller radii are served by filtering this cache.
DEFAULT_SCAN_RADIUS = 32


def _executor_busy_count() -> int:
    """Read bridge.server._executor_busy via lazy import.

    Lazy because `bridge.server` imports `WorldCache` from this module at
    load time, so a top-level `from bridge import server` would be
    circular. Lookup is a plain getattr with a default of 0, so it's
    safe to call even before server.py has finished loading.
    """
    try:
        from bridge import server as _server  # local import to break cycle
    except ImportError:
        return 0
    return getattr(_server, "_executor_busy", 0)


@dataclass
class CachedBlock:
    name: str
    x: int
    y: int
    z: int


class WorldCache:
    """Thread-safe cache of nearby blocks, entities, and player status."""

    def __init__(self, scan_radius: int = DEFAULT_SCAN_RADIUS) -> None:
        self._blocks: dict[tuple[int, int, int], CachedBlock] = {}
        self._entities: list[dict] = []
        self._status: dict = {}
        self._lock = threading.Lock()

        self._scan_radius = scan_radius
        self._scan_center: tuple[int, int, int] | None = None
        self._last_block_scan: float = 0.0
        self._last_status_update: float = 0.0
        self._last_entity_update: float = 0.0
        self._blocks_populated = False

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background scanner thread (daemon, auto-exit on shutdown)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._scan_loop, name="world-cache-scanner", daemon=True
        )
        self._thread.start()
        logger.info("WorldCache scanner thread started")

    def stop(self) -> None:
        """Signal the scanner thread to exit. Daemon thread will also die with process."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Scanner loop
    # ------------------------------------------------------------------

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.monotonic()

                # Refresh player status first (cheap) so we know where to scan
                if now - self._last_status_update >= STATUS_POLL_INTERVAL:
                    self._update_status()
                    self._last_status_update = time.monotonic()

                # Refresh entities on its own cadence
                if now - self._last_entity_update >= ENTITY_POLL_INTERVAL:
                    self._update_entities()
                    self._last_entity_update = time.monotonic()

                # Decide whether to re-scan blocks
                px, py, pz = self._current_int_position()
                needs_rescan = False
                if not self._blocks_populated:
                    needs_rescan = True
                elif self._scan_center is None:
                    needs_rescan = True
                else:
                    cx, cy, cz = self._scan_center
                    dx, dy, dz = px - cx, py - cy, pz - cz
                    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if dist > MOVEMENT_RESCAN_THRESHOLD:
                        logger.info(
                            f"WorldCache: player moved {dist:.1f} blocks "
                            f"from scan center, triggering re-scan"
                        )
                        needs_rescan = True
                    elif now - self._last_block_scan >= BLOCK_SCAN_INTERVAL:
                        needs_rescan = True

                if needs_rescan:
                    # Don't drum the Minescript pipe with a 64-chunk scan
                    # while a foreground command is holding the executor
                    # thread — that's the write pattern that most reliably
                    # triggers the Java-side stdout-writer race. Skip this
                    # iteration; _last_block_scan stays pinned so we'll
                    # re-check on the next LOOP_TICK and scan as soon as
                    # the executor is idle.
                    if _executor_busy_count() > 0:
                        logger.debug(
                            "WorldCache: skipping rescan — executor busy"
                        )
                    else:
                        self._rescan_blocks(px, py, pz)
                        self._last_block_scan = time.monotonic()

            except Exception as e:
                logger.error(f"WorldCache scan loop error: {type(e).__name__}: {e}")

            # Sleep in small increments so stop() is responsive
            self._stop.wait(LOOP_TICK)

    def _current_int_position(self) -> tuple[int, int, int]:
        """Read player position from the latest cached status (or default)."""
        with self._lock:
            pos = self._status.get("position", {}) if self._status else {}
        try:
            return int(pos.get("x", 0)), int(pos.get("y", 0)), int(pos.get("z", 0))
        except (TypeError, ValueError):
            return 0, 0, 0

    def _update_status(self) -> None:
        try:
            data = minescript_api.get_player_status()
        except Exception as e:
            logger.warning(f"WorldCache: status update failed: {e}")
            return
        with self._lock:
            self._status = data

    def _update_entities(self) -> None:
        try:
            ents = minescript_api.get_nearby_entities(radius=32)
        except Exception as e:
            logger.warning(f"WorldCache: entity update failed: {e}")
            return
        with self._lock:
            self._entities = ents

    def _rescan_blocks(self, px: int, py: int, pz: int) -> None:
        """Full chunked scan around (px, py, pz). Atomic swap into cache."""
        try:
            scanned = minescript_api.scan_blocks_chunked(
                px, py, pz, self._scan_radius, block_types=None
            )
        except Exception as e:
            logger.warning(f"WorldCache: block re-scan failed: {e}")
            return

        new_blocks: dict[tuple[int, int, int], CachedBlock] = {}
        for b in scanned:
            key = (b["x"], b["y"], b["z"])
            new_blocks[key] = CachedBlock(
                name=b["name"], x=b["x"], y=b["y"], z=b["z"]
            )

        with self._lock:
            self._blocks = new_blocks
            self._scan_center = (px, py, pz)
            self._blocks_populated = True
        logger.info(
            f"WorldCache: re-scanned {len(new_blocks)} blocks at ({px},{py},{pz})"
        )

    # ------------------------------------------------------------------
    # Query API (HTTP handlers call these)
    # ------------------------------------------------------------------

    def query_blocks(
        self, radius: int, block_types: list[str] | None = None
    ) -> list[dict] | None:
        """Return blocks within radius of the current player position.

        Returns None if the cache has not been populated yet (signals caller
        to fall back to a live scan).
        """
        with self._lock:
            if not self._blocks_populated:
                return None
            pos = self._status.get("position", {}) if self._status else {}
            try:
                px = float(pos.get("x", 0))
                py = float(pos.get("y", 0))
                pz = float(pos.get("z", 0))
            except (TypeError, ValueError):
                px = py = pz = 0.0

            radius = min(radius, self._scan_radius)
            radius_sq = radius * radius
            type_set = set(block_types) if block_types else None

            results: list[dict] = []
            for (bx, by, bz), entry in self._blocks.items():
                if type_set and entry.name not in type_set:
                    continue
                dx, dy, dz = bx - px, by - py, bz - pz
                dist_sq = dx * dx + dy * dy + dz * dz
                if dist_sq > radius_sq:
                    continue
                results.append({
                    "name": entry.name,
                    "x": bx,
                    "y": by,
                    "z": bz,
                    "distance": round(math.sqrt(dist_sq), 1),
                })

        results.sort(key=lambda b: b["distance"])
        return results

    def query_entities(self, radius: int) -> list[dict] | None:
        """Return cached entities within radius of the current player position.

        Returns None if the cache has not been populated yet.
        """
        with self._lock:
            if not self._entities and self._last_entity_update == 0.0:
                return None
            # Entities already include their own distance from scan time, but
            # the player may have moved slightly. Recompute relative to cached
            # position for consistency with query_blocks.
            pos = self._status.get("position", {}) if self._status else {}
            try:
                px = float(pos.get("x", 0))
                py = float(pos.get("y", 0))
                pz = float(pos.get("z", 0))
            except (TypeError, ValueError):
                px = py = pz = 0.0

            results: list[dict] = []
            for ent in self._entities:
                try:
                    ex, ey, ez = float(ent["x"]), float(ent["y"]), float(ent["z"])
                except (KeyError, TypeError, ValueError):
                    continue
                dist = math.sqrt((ex - px) ** 2 + (ey - py) ** 2 + (ez - pz) ** 2)
                if dist > radius:
                    continue
                e = dict(ent)
                e["distance"] = round(dist, 1)
                results.append(e)
        results.sort(key=lambda e: e["distance"])
        return results

    def query_status(self) -> dict | None:
        """Return the cached player status dict.

        Returns None if the cache has not been populated yet.
        """
        with self._lock:
            if not self._status:
                return None
            return dict(self._status)

    def get_health(self) -> float | None:
        """Return cached player health (None if not yet populated)."""
        with self._lock:
            if not self._status:
                return None
            val = self._status.get("health")
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

    # ------------------------------------------------------------------
    # Write-through (called by POST handlers after successful commands)
    # ------------------------------------------------------------------

    def on_block_broken(self, x: int, y: int, z: int) -> None:
        """Remove a broken block from the cache immediately."""
        with self._lock:
            self._blocks.pop((int(x), int(y), int(z)), None)

    def on_block_placed(self, name: str, x: int, y: int, z: int) -> None:
        """Add a placed block to the cache immediately."""
        clean = name.replace("minecraft:", "").split("[")[0]
        with self._lock:
            key = (int(x), int(y), int(z))
            self._blocks[key] = CachedBlock(name=clean, x=key[0], y=key[1], z=key[2])

    # ------------------------------------------------------------------
    # Invalidation (called by POST handlers after mutations the cache
    # can't predict directly — e.g. crafting modifies inventory, Baritone
    # commands move the player asynchronously)
    # ------------------------------------------------------------------

    def invalidate_status(self) -> None:
        """Force the scanner to re-poll player status on its next tick."""
        with self._lock:
            self._last_status_update = 0.0

    def invalidate_entities(self) -> None:
        """Force the scanner to re-poll entities on its next tick."""
        with self._lock:
            self._last_entity_update = 0.0

    def invalidate_blocks(self) -> None:
        """Force a full block re-scan on the scanner's next tick."""
        with self._lock:
            self._blocks_populated = False
            self._scan_center = None
            self._last_block_scan = 0.0

    def force_refresh_status(self) -> None:
        """Synchronously re-poll player status right now.

        Used after mutations that change inventory/position so the very next
        query sees fresh data without waiting for the 2s scanner tick. Must
        be called through the bridge's _run() executor — this function
        issues a Minescript RPC internally.
        """
        try:
            data = minescript_api.get_player_status()
        except Exception as e:
            logger.warning(f"WorldCache: force refresh status failed: {e}")
            return
        with self._lock:
            self._status = data
            self._last_status_update = time.monotonic()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def scan_age(self) -> float:
        """Seconds since the last block scan completed (infinity if never)."""
        if self._last_block_scan == 0.0:
            return float("inf")
        return time.monotonic() - self._last_block_scan

    def stats(self) -> dict:
        """Diagnostic snapshot of cache state."""
        with self._lock:
            return {
                "blocks": len(self._blocks),
                "entities": len(self._entities),
                "scan_center": self._scan_center,
                "scan_age_s": round(self.scan_age(), 1) if self._last_block_scan else None,
                "status_populated": bool(self._status),
                "blocks_populated": self._blocks_populated,
            }
