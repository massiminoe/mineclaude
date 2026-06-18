package com.mineclaude.bridge.mixin;

import com.mineclaude.bridge.EventBus;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayNetworkHandler;
import net.minecraft.network.packet.s2c.play.DeathMessageS2CPacket;
import net.minecraft.network.packet.s2c.play.HealthUpdateS2CPacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

// Latches a death the instant a death packet lands, off the tick cadence. The
// per-tick `health <= 0` poll in EventBus misses ~40% of deaths when
// `doImmediateRespawn` is on: the server kills + respawns in one server tick, so
// the respawn packet swaps in a fresh full-HP player before END_CLIENT_TICK ever
// samples health <= 0, and the whole alive->dead->alive transition goes
// unobserved. Latching at packet time makes detection immediate-respawn-proof.
//
// Primary hook is onDeathMessage: the server sends DeathMessageS2CPacket
// synchronously inside onDeath, *before* the immediate respawn, so it reliably
// reaches the client. onHealthUpdate is a redundant backstop — the lethal
// health=0 update is NOT reliably sent under immediate respawn (the player
// entity is replaced before the server syncs it), so it can't be the sole
// signal. Java (not Kotlin) so Loom's Mixin AP picks it up and emits a refmap.
@Mixin(ClientPlayNetworkHandler.class)
public class ClientPlayNetworkHandlerHealthMixin {

    @Inject(method = "onDeathMessage", at = @At("HEAD"))
    private void mineclaude_onDeathMessage(DeathMessageS2CPacket packet, CallbackInfo ci) {
        try {
            MinecraftClient mc = MinecraftClient.getInstance();
            if (mc.player == null || packet.playerId() != mc.player.getId()) return;
            EventBus.onDeathPacket();
        } catch (Throwable t) {
            // Never propagate — a throw on the network thread disconnects us.
        }
    }

    @Inject(method = "onHealthUpdate", at = @At("HEAD"))
    private void mineclaude_onHealthUpdate(HealthUpdateS2CPacket packet, CallbackInfo ci) {
        try {
            EventBus.onHealthPacket(packet.getHealth());
        } catch (Throwable t) {
            // Never propagate.
        }
    }
}
