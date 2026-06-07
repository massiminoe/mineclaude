---
name: mineclaude
description: Drive the Mineclaude headless Minecraft bot over MCP. Covers the execute/get_state/interrupt/screenshot/handler tools, the primitive vocabulary you run inside execute, the event + reflex model, and proven patterns for mining, crafting, building, and combat. Use whenever you are operating the bot.
---

# Mineclaude

You are driving a real Minecraft bot — a headless client in a live world —
through an MCP server. You are the agent now; the bot has no LLM of its own. You
observe with `get_state` / `screenshot`, act by sending Python to `execute`, and
talk by calling `say(...)` inside that Python.

The world is real and persists: blocks you break stay broken, mobs hurt you, the
sun sets and hostiles spawn, tools wear out. Play with curiosity and patience —
short trips, frequent check-ins. The journey is part of it.

## The loop

1. **Observe** — `get_state()` returns a structured snapshot (player, inventory,
   equipped, current action, recent reflex fires, buffered events). Call it
   before deciding; it's cheap. `screenshot(...)` when you need to *see*.
2. **Act** — `execute(code=...)` runs Python with the primitive namespace and
   **blocks** until the action finishes, returning `{status, result, error, ...}`.
   One action at a time (single-flight): a second `execute` while one is running
   comes back `busy`. If an action runs longer than the inline-wait budget
   (`wait`, default ~50s), `execute` returns `status:"running"` with an
   `action_id` *while the action keeps going in the background* — it still holds
   the slot. Don't treat that as an error: poll `get_state()` (its
   `action.result` fills in when the action completes) or `interrupt()` to
   abort. Pass a larger `wait` for an action you expect to be long but want to
   block on; pass `wait=0` to fire-and-watch.
3. **React** — between actions, `wait_for_event(...)` or poll `get_state` for
   chat, death, and world events. Install standing reactions with `set_handler`.

```
state = get_state()              # observe
execute(code="...python...")     # act (blocks)
ev = wait_for_event(["chat"], timeout=5)   # react
```

## Tools (7)

`execute`, `interrupt`, `get_state`, `screenshot`, `get_handler`, `set_handler`,
`wait_for_event`. Full schemas in **[tools.md](tools.md)**. `say(message)` is a
**primitive inside `execute`**, not a tool — talking is something the bot *does*.

## Writing `execute` code

- All primitives are async — `await` every call (except `log`). Full list with
  signatures in **[primitives.md](primitives.md)**.
- `return` a short string describing what happened; it lands in `result`.
- `say("...")` to speak to players in-game (240-char auto-split).
- `import` is blocked; `math` is preloaded; no dunder access.
- Keep each `execute` bounded — a long mutating loop is blind (you can't observe
  mid-run). Break work into legs and re-observe between them.

```python
# example execute body
logs = await findBlocks("oak_log", 32, 5)
if not logs:
    return "no oak nearby"
await say("chopping oak")
for b in logs:
    await breakBlockAt(b["x"], b["y"], b["z"])
await collectItems(radius=10)
return f"chopped {len(logs)} logs"
```

## Concurrency

`execute` blocks, but `interrupt()` is out-of-band — always allowed, even mid-run.
For a watcher/worker split, run a loop that polls `get_state` / `wait_for_event`
and calls `interrupt()` to preempt a worker's blocking `execute` when something
urgent happens. `interrupt()` purges the action slot and halts the bridge
(Baritone + any attack loop).

## Events & reflexes

Hazards (damage, lava, drowning, tool break) react instantly via a built-in
reflex layer — you see them after the fact in `get_state().reflexes_recent`.
Everything else (chat, death, respawn, world mutations) lands in the flushable
`events` buffer. You can author your own reaction to any event type with
`set_handler`. See **[events.md](events.md)** and **[handlers.md](handlers.md)**.

## Reference

- **[primitives.md](primitives.md)** — every primitive (generated from code)
- **[events.md](events.md)** — event types + default reaction policy (generated)
- **[tools.md](tools.md)** — MCP tool schemas (generated)
- **[mental-model.md](mental-model.md)** — how to play well: preparation, day/night,
  mining/building discipline, vision, hunger
- **[snippets.md](snippets.md)** — proven `execute` patterns (trees, ore, shelter)
- **[handlers.md](handlers.md)** — the reaction-handler contract + examples

The three generated files come from `scripts/gen_skill_docs.py` — re-run it after
changing primitives, reflex handlers, or MCP tools so the reference can't drift.

## Activating this skill

Committed at `skills/mineclaude/` in the repo. To have Claude Code auto-discover
it, symlink or copy it into a skills path, e.g.
`ln -s "$(pwd)/skills/mineclaude" .claude/skills/mineclaude`. Until then it's
readable as plain docs.
