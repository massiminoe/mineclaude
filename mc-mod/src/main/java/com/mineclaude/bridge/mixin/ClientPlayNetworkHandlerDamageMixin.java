package com.mineclaude.bridge.mixin;

import com.mineclaude.bridge.EventBus;
import kotlin.Triple;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayNetworkHandler;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.entity.Entity;
import net.minecraft.network.packet.s2c.play.EntityDamageS2CPacket;
import net.minecraft.registry.Registries;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

// Captures damage source attribution off the network thread and stashes it on
// EventBus.pendingDamage. The tick callback combines it with the next observed
// HP drop. Java rather than Kotlin so Loom's Mixin AP picks it up and emits a
// refmap (yarn -> intermediary remapping at runtime).
@Mixin(ClientPlayNetworkHandler.class)
public class ClientPlayNetworkHandlerDamageMixin {

    // onEntityDamage is the packet that carries the DamageType + attacker
    // identity (onDamageTilt only carries the screen-shake yaw, not who hit
    // us). Both fire for the same hit, but onEntityDamage has the data we
    // need for source attribution.
    @Inject(method = "onEntityDamage", at = @At("HEAD"))
    private void mineclaude_onEntityDamage(EntityDamageS2CPacket packet, CallbackInfo ci) {
        try {
            MinecraftClient mc = MinecraftClient.getInstance();
            if (mc.player == null) return;
            if (packet.entityId() != mc.player.getId()) return;

            ClientWorld world = mc.world;
            if (world == null) return;

            String source = packet.sourceType().getKey()
                .map(k -> k.getValue().getPath())
                .orElse("unknown");

            // sourceCauseId() is already the raw entity id: the wire format
            // is `id + 1` with 0 = "no attacker", but readOffsetVarInt subtracts
            // 1 on deserialization, so this field is the entity id directly
            // (or -1 for none). Don't subtract again — that lookup lands on a
            // neighbor in the entity table and silently misattributes the hit.
            int causeId = packet.sourceCauseId();
            Entity attacker = causeId >= 0 ? world.getEntityById(causeId) : null;

            String attackerKind = attacker != null
                ? Registries.ENTITY_TYPE.getId(attacker.getType()).getPath()
                : null;
            Integer attackerId = attacker != null ? attacker.getId() : null;

            Triple<Double, Double, Double> attackerPos = packet.sourcePosition()
                .map(p -> new Triple<>(p.x, p.y, p.z))
                .orElseGet(() -> attacker != null
                    ? new Triple<>(attacker.getX(), attacker.getY(), attacker.getZ())
                    : null);

            EventBus.setPendingDamage(new EventBus.PendingDamage(
                source, attackerKind, attackerId, attackerPos
            ));
        } catch (Throwable t) {
            // Mixin runs on the network thread; an uncaught throw would
            // disconnect us. Better to lose attribution on an edge case.
            EventBus.setPendingDamage(null);
        }
    }
}
