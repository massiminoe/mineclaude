package com.mineclaude.bridge

/**
 * Cooperative ownership of the player's use-key — the offhand shield hold and
 * the bow draw both drive it, and vanilla only lets one item-use run at a time.
 *
 * Ownership is otherwise implicit across the codebase: [AttackRoute]'s melee
 * block-rhythm and its bow draw, and standalone [ShieldRoute], each set
 * `useKey` directly around their own windows. [AutoShield] is the lowest-
 * priority consumer and must yield to all of them so it can never clobber a
 * deliberate block or steal the hand mid-draw. This object centralizes the one
 * read the auto-shield gates on, so that "is someone already on the use-key?"
 * is an explicit call rather than the reflex reaching into two other singletons.
 */
object UseKeyArbiter {
    /**
     * True when a deliberate consumer already owns the use-key: an in-flight
     * combat session (melee rhythm or bow draw — both share the one combat
     * slot) or a standalone `/block`. [AutoShield] stands down while this holds,
     * without ever touching the key itself (the owner asserts its own state).
     */
    fun claimedByOther(): Boolean =
        AttackRoute.hasActiveSession() || ShieldRoute.isBlocking()
}
