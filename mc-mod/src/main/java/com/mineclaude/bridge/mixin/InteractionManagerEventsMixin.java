package com.mineclaude.bridge.mixin;

import com.mineclaude.bridge.EventLog;
import com.mineclaude.bridge.PlaceSnapshot;
import net.minecraft.block.BlockState;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.network.ClientPlayerInteractionManager;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.entity.Entity;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.registry.Registries;
import net.minecraft.util.ActionResult;
import net.minecraft.util.Hand;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.math.BlockPos;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

import java.util.HashMap;
import java.util.Map;

/**
 * Captures world-mutation events from the local player's interaction
 * pipeline and pushes them to {@link EventLog} for the agent's per-iteration
 * gameState injection.
 *
 * Three hooks, all on {@code ClientPlayerInteractionManager}:
 *
 *   - {@code breakBlock(BlockPos)} — fires once per completed break
 *     (vanilla left-click + Baritone auto-mining both terminate here)
 *   - {@code interactBlock(player, hand, hitResult)} — fires for every
 *     right-click-on-block; we filter to "actually placed a block" by
 *     diffing the air-state at the placement position across the call
 *   - {@code attackEntity(player, target)} — fires once per swing on an
 *     entity (multi-hit attacks emit multiple events; that's correct,
 *     each swing is real)
 *
 * Source-agnostic by design: anything that calls these methods is
 * captured, so Baritone's autonomous mining/scaffolding shows up next
 * to Claude's explicit primitive calls. The agent does its own
 * de-duplication if needed.
 *
 * State-passing between HEAD and RETURN uses {@link ThreadLocal} rather
 * than a static volatile. These methods all run on the client tick
 * thread today, but ThreadLocal makes the pattern safe even if MC
 * starts dispatching some interactions off-thread in a future version,
 * and avoids cross-method clobbering if any of these recurse.
 *
 * Java rather than Kotlin so Loom's Mixin AP picks it up and emits a
 * refmap (yarn → intermediary remapping at runtime).
 */
@Mixin(ClientPlayerInteractionManager.class)
public class InteractionManagerEventsMixin {

    // Snapshotted at HEAD, consumed at RETURN. Per-thread so a recursive
    // or interleaved call on the same JVM thread doesn't see another
    // call's state. (Different threads would see independent values too,
    // which is the safe failure mode.)
    private static final ThreadLocal<String> BREAK_BLOCK_ID = new ThreadLocal<>();
    private static final ThreadLocal<PlaceSnapshot> PLACE_STATE = new ThreadLocal<>();

    // ---- breakBlock ---------------------------------------------------

    @Inject(method = "breakBlock", at = @At("HEAD"))
    private void mineclaude_onBreakBlockHead(BlockPos pos, CallbackInfoReturnable<Boolean> cir) {
        try {
            MinecraftClient mc = MinecraftClient.getInstance();
            ClientWorld world = mc.world;
            if (world == null) {
                BREAK_BLOCK_ID.remove();
                return;
            }
            BlockState state = world.getBlockState(pos);
            if (state.isAir()) {
                // Already air — the call will return false; nothing to log.
                BREAK_BLOCK_ID.remove();
                return;
            }
            String id = Registries.BLOCK.getId(state.getBlock()).getPath();
            BREAK_BLOCK_ID.set(id);
        } catch (Throwable t) {
            // Mixins must never crash the host; lose the event instead.
            BREAK_BLOCK_ID.remove();
        }
    }

    @Inject(method = "breakBlock", at = @At("RETURN"))
    private void mineclaude_onBreakBlockReturn(BlockPos pos, CallbackInfoReturnable<Boolean> cir) {
        try {
            String id = BREAK_BLOCK_ID.get();
            BREAK_BLOCK_ID.remove();
            if (id == null) return;
            // Only emit on actual removal — breakBlock returns false on
            // permission failures and similar no-ops.
            if (!Boolean.TRUE.equals(cir.getReturnValue())) return;

            Map<String, Object> data = new HashMap<>();
            data.put("block", id);
            data.put("pos", posMap(pos));
            EventLog.INSTANCE.push("block_broken", data);
        } catch (Throwable t) {
            // swallow
        }
    }

    // ---- interactBlock ------------------------------------------------

    @Inject(method = "interactBlock", at = @At("HEAD"))
    private void mineclaude_onInteractBlockHead(
        ClientPlayerEntity player,
        Hand hand,
        BlockHitResult hit,
        CallbackInfoReturnable<ActionResult> cir
    ) {
        try {
            MinecraftClient mc = MinecraftClient.getInstance();
            ClientWorld world = mc.world;
            if (world == null) {
                PLACE_STATE.remove();
                return;
            }
            // Placement position is the cell adjacent to the hit block on
            // the side that was clicked. interactBlock may also place
            // *into* the hit cell (e.g. tall grass → replaceable), so
            // record both candidates; we resolve at RETURN by checking
            // which one actually became non-air.
            BlockPos clickedPos = hit.getBlockPos();
            BlockPos adjacentPos = clickedPos.offset(hit.getSide());
            BlockState clickedState = world.getBlockState(clickedPos);
            PLACE_STATE.set(new PlaceSnapshot(
                clickedPos,
                adjacentPos,
                clickedState.isAir(),
                clickedState.isReplaceable(),
                world.getBlockState(adjacentPos).isAir()
            ));
        } catch (Throwable t) {
            PLACE_STATE.remove();
        }
    }

    @Inject(method = "interactBlock", at = @At("RETURN"))
    private void mineclaude_onInteractBlockReturn(
        ClientPlayerEntity player,
        Hand hand,
        BlockHitResult hit,
        CallbackInfoReturnable<ActionResult> cir
    ) {
        try {
            PlaceSnapshot st = PLACE_STATE.get();
            PLACE_STATE.remove();
            if (st == null) return;

            MinecraftClient mc = MinecraftClient.getInstance();
            ClientWorld world = mc.world;
            if (world == null) return;

            // Resolve which cell (if any) actually became a new block.
            // Either the click target itself (if it was air/replaceable
            // and now isn't) or its adjacent cell (the standard case).
            BlockPos placedAt = null;
            BlockState placedState = null;
            BlockState adjacentNow = world.getBlockState(st.adjacentPos);
            if (st.adjacentWasAir && !adjacentNow.isAir()) {
                placedAt = st.adjacentPos;
                placedState = adjacentNow;
            } else {
                BlockState clickedNow = world.getBlockState(st.clickedPos);
                if ((st.clickedWasAir || st.clickedWasReplaceable) && !clickedNow.isAir()) {
                    placedAt = st.clickedPos;
                    placedState = clickedNow;
                }
            }
            if (placedAt == null || placedState == null) return;

            String blockId = Registries.BLOCK.getId(placedState.getBlock()).getPath();
            Map<String, Object> data = new HashMap<>();
            data.put("block", blockId);
            data.put("pos", posMap(placedAt));
            EventLog.INSTANCE.push("block_placed", data);
        } catch (Throwable t) {
            // swallow
        }
    }

    // ---- attackEntity -------------------------------------------------

    @Inject(method = "attackEntity", at = @At("HEAD"))
    private void mineclaude_onAttackEntity(PlayerEntity player, Entity target, CallbackInfo ci) {
        try {
            if (target == null) return;
            String kind = Registries.ENTITY_TYPE.getId(target.getType()).getPath();
            Map<String, Object> data = new HashMap<>();
            data.put("kind", kind);
            data.put("entity_id", target.getId());
            Map<String, Object> pos = new HashMap<>();
            pos.put("x", target.getX());
            pos.put("y", target.getY());
            pos.put("z", target.getZ());
            data.put("pos", pos);
            EventLog.INSTANCE.push("entity_attacked", data);
        } catch (Throwable t) {
            // swallow
        }
    }

    // ---- helpers ------------------------------------------------------

    private static Map<String, Object> posMap(BlockPos pos) {
        Map<String, Object> m = new HashMap<>();
        m.put("x", pos.getX());
        m.put("y", pos.getY());
        m.put("z", pos.getZ());
        return m;
    }
}
