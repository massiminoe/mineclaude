package com.mineclaude.bridge.mixin;

import net.minecraft.entity.projectile.FishingBobberEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

// FishingBobberEntity.caughtFish (backed by the synced CAUGHT_FISH TrackedData)
// is private with no getter — it's the only client-visible signal that a fish
// actually bit (vs. the bobber just floating). An @Accessor mixin exposes it
// without reflection; Java (not Kotlin) so Loom's Mixin AP emits a refmap,
// matching the other mixins in this package.
@Mixin(FishingBobberEntity.class)
public interface FishingBobberAccessor {
    @Accessor("caughtFish")
    boolean mineclaude_getCaughtFish();
}
