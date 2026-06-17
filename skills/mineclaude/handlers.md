# Handlers — authoring reactions to events

A handler is a standing reaction body you install for an event type with
`set_handler`. It fires every time that event arrives, without you being in the
loop. The runtime owns the machinery around it — cooldown, latest-wins
cancellation, deadline budgets; you write only the reaction.

`get_handler(event_type)` reads the current policy:
`{event_type, source: default|authored, preempts, cooldown_s, code}`. The default
policies are in [events.md](events.md). `set_handler` overrides them.

## What a handler body gets

The body runs in the same sandbox as `execute`, with:

- the full **primitive namespace** (movement, mining, `say`, ...),
- `data` — the event payload (e.g. for `chat`: `{"username", "message"}`),
- `interrupt` — acquire the action slot (await it).

The code is AST-validated when you install it (no `import`, no dunders), same as
`execute`.

## The one rule: acquire before you mutate

> `interrupt()` acquires the action slot. **Reads and `say()` are always free;
> anything that mutates the world needs the slot first.** `preempts=True` acquires
> it for you before the body runs (and freezes the world before your reads).
> Otherwise call `interrupt()` yourself when the payload warrants it. The footgun
> is acting *before* acquiring — two `goto`s racing. Safe shape:
> **read → decide → `interrupt()` → act.**

`interrupt()` cancels whatever `execute`/action is in flight, but **not** the
handler task that called it — so a handler can interrupt the world and then act in
the same body.

- **`preempts=True`** — the runtime calls `interrupt()` *before* your body runs.
  Use when the event always means "drop everything now" (lava, drowning, fire, death).
  Your body starts with the slot held and the world frozen.
- **`preempts=False`** — your body runs alongside whatever's happening. Use for
  events that only *sometimes* warrant interrupting (a chat message that might be
  "stop", might be small talk). Read the payload, decide, and call `interrupt()`
  yourself before you mutate.

## Install / inspect

```
set_handler(
  event_type="chat",
  code="<python reaction body>",
  preempts=False,      # default
  cooldown_s=0.0,      # min seconds between fires
)
get_handler("chat")    # -> {source: "authored", code: "...", ...}
```

## Examples

### A chat "stop" command (conditional interrupt, preempts=False)

```python
# set_handler("chat", code=<this>, preempts=False)
msg = (data.get("message") or "").lower()
if "stop" in msg:
    await interrupt()            # acquire the slot — halts current action + bridge
    await say("stopping")
# otherwise: record-only, let whatever's running continue
```

### Low-HP flee override (preempts=True)

```python
# set_handler("damage_taken", code=<this>, preempts=True, cooldown_s=5)
# preempts=True: the slot is already ours and the world is frozen for our reads.
hp = data.get("hp_before", 20) - data.get("amount", 0)
ap = data.get("attacker_pos") or {}
if hp <= 6 and ap.get("x") is not None:
    me = (await getStats())["position"]
    dx, dz = me["x"] - ap["x"], me["z"] - ap["z"]      # away from the attacker
    mag = max(0.1, math.sqrt(dx * dx + dz * dz))
    await goToPosition(me["x"] + dx / mag * 10, me["z"] + dz / mag * 10)
    await say("disengaging")
```

### A standing chat command that builds (read → interrupt → act)

```python
# set_handler("chat", code=<this>, preempts=False)
msg = (data.get("message") or "").lower()
if msg.startswith("come"):
    user = data.get("username")
    await interrupt()            # drop current work before pathing
    await goToPlayer(user)
    await say(f"coming, {user}")
```

## Cooldown & latest-wins

- `cooldown_s` gates re-fires of the *same* event type — a swarm of
  `hostile_nearby` won't flood you.
- Handlers run as their own tasks; a newer reflex cancels an in-flight prior
  handler (the reaction to the latest event wins). Hold the world only as long as
  the reaction needs it.

## When NOT to use a handler

Handlers are for *standing* reactions — things that should happen every time
without you deciding. For one-off responses, just `wait_for_event(...)` in your
own loop and act on the result with `execute`. Reads (`get_state`) and `say` never
need a handler or the slot.
