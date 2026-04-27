package com.mineclaude.bridge

import java.util.concurrent.CopyOnWriteArrayList

/**
 * Registry of HTTP endpoints the native mod claims to own.
 *
 * Surfaced via `/health` and `/probe` so the agent can verify which bridge
 * is serving which path during the migration. Endpoints add themselves at
 * registration time — see e.g. [PlayerStatusRoutes].
 */
object PortedEndpoints {
    private val endpoints = CopyOnWriteArrayList<String>()

    fun register(path: String) {
        if (path !in endpoints) endpoints.add(path)
    }

    fun list(): List<String> = endpoints.toList().sorted()
}
