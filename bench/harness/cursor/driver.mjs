// Cursor harness driver: run one bench session against the mineclaude MCP
// server until the wall-clock budget expires, then write the transcript and the
// billed usage ledger.
//
// Why the SDK instead of `cursor-agent -p`: the CLI's stream-json result event
// carries only `duration_ms` — no tokens, no cost — which would leave a Cursor
// entry with no usage.json at all. `@cursor/sdk` runs the same local agent and
// exposes `agent.getUsage()` (billed tokens + dollar cost), so a Cursor run is
// as accountable as a Claude Code one. The CLI stays the documented fallback.
//
// Env (the harness contract, see bench/harness/common.sh):
//   MCP_URL, BENCH_MODEL, BENCH_RUN_SECONDS, ARTIFACTS_DIR, WORKSPACE,
//   CURSOR_API_KEY, PROMPT_FILE
import { appendFileSync, writeFileSync, readFileSync } from "node:fs";
import { Agent, Cursor } from "@cursor/sdk";

const MCP_URL = process.env.MCP_URL ?? "http://mineclaude:5556/mcp";
const MODEL = process.env.BENCH_MODEL;
const RUN_SECONDS = Number(process.env.BENCH_RUN_SECONDS ?? 3600);
const ART = process.env.ARTIFACTS_DIR ?? "/artifacts";
const WORKSPACE = process.env.WORKSPACE ?? "/workspace";
const PROMPT_FILE = process.env.PROMPT_FILE ?? "/opt/bench/prompt.md";

if (!MODEL) {
    console.error("BENCH_MODEL must be set");
    process.exit(2);
}

// Same reasoning as the try/catch around Agent.create: a stray rejection must
// not bury the run log in minified SDK source.
process.on("unhandledRejection", (err) => {
    console.error(`[harness] FATAL unhandled rejection: ${err?.message ?? err}`);
    process.exit(1);
});

const log = (msg) => {
    const line = `[harness ${new Date().toISOString().slice(11, 19)}] ${msg}`;
    console.log(line);
    appendFileSync(`${ART}/harness.log`, line + "\n");
};

const deadline = Date.now() + RUN_SECONDS * 1000;
const secondsLeft = () => Math.floor((deadline - Date.now()) / 1000);

const prompt =
    readFileSync(PROMPT_FILE, "utf8") +
    `\n\nYour time budget for this session is ${RUN_SECONDS} seconds of wall-clock time, starting now.\n`;
// Identical wording to common.sh's continue_prompt() — a model must never be
// advantaged by a differently-phrased nudge.
const continuePrompt = (left) =>
    `The session is still live — roughly ${left} seconds remain. Keep playing and earning advancements; do not stop to summarize until time is up.`;

// Model catalogue: proof the id we were handed is real, and the record of what
// was on offer at run time. Best-effort — a catalogue outage must not sink a run.
try {
    const models = await Cursor.models.list();
    writeFileSync(`${ART}/cursor-models.json`, JSON.stringify(models, null, 2) + "\n");
    const ids = models.map((m) => m.id);
    if (!ids.includes(MODEL)) {
        log(`WARN: model "${MODEL}" is not in the account's catalogue (${ids.join(", ")}) — sending anyway`);
    }
} catch (err) {
    log(`WARN: could not list models: ${err?.message ?? err}`);
}

let agent;
try {
    agent = await Agent.create({
        model: { id: MODEL },
        apiKey: process.env.CURSOR_API_KEY,
        // settingSources:["project"] loads the workspace's own layer, which is how
        // .cursor/skills/mineclaude gets discovered. MCP is passed inline instead
        // (inline takes precedence) so the server can never be silently missing.
        local: { cwd: WORKSPACE, settingSources: ["project"] },
        mcpServers: { mineclaude: { type: "http", url: MCP_URL } },
    });
} catch (err) {
    // Bad key, unknown model, unreachable backend: fail with one readable line.
    // Left unhandled, the SDK's bundled stack trace is 60KB of minified source
    // that buries the actual reason in the run log.
    log(`FATAL: could not create the agent: ${err?.message ?? err}`);
    process.exit(1);
}
log(`model=${MODEL} budget=${RUN_SECONDS}s mcp=${MCP_URL} agent=${agent.agentId}`);

// Per-turn usage from the stream, kept as a cross-check against the billed
// totals — the same discipline as the Claude Code parser's "only the result
// event is authoritative" rule.
const streamed = { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, reasoningTokens: 0 };
const invocations = [];
let fails = 0;
let i = 0;

while (secondsLeft() > 15) {
    i += 1;
    const left = secondsLeft();
    const transcript = `${ART}/cursor-${i}.jsonl`;
    log(`invocation ${i} starts (${left}s left)`);
    const startedAt = Date.now();
    let events = 0;
    let toolCalls = 0;
    let error = null;

    try {
        const run = await agent.send(i === 1 ? prompt : continuePrompt(left));
        // The budget, not the model, ends the session: cancel the in-flight run
        // the moment the clock runs out rather than waiting for a natural stop.
        const timer = setTimeout(() => {
            run.cancel().catch(() => {});
        }, Math.max(1000, deadline - Date.now()));
        try {
            for await (const event of run.stream()) {
                events += 1;
                if (event.type === "tool_call" || event.type === "tool_use") toolCalls += 1;
                if (event.type === "usage" && event.usage) {
                    for (const k of Object.keys(streamed)) streamed[k] += event.usage[k] ?? 0;
                }
                appendFileSync(transcript, JSON.stringify(event) + "\n");
            }
        } finally {
            clearTimeout(timer);
        }
        if (run.error) error = run.error.message ?? String(run.error);
    } catch (err) {
        error = err?.message ?? String(err);
    }

    invocations.push({
        file: `cursor-${i}.jsonl`,
        agent_id: agent.agentId,
        events,
        tool_calls: toolCalls,
        duration_ms: Date.now() - startedAt,
        error,
    });
    log(`invocation ${i} ended events=${events} tools=${toolCalls}${error ? ` error=${error}` : ""}`);

    // Same failure policy as the bash harnesses: back off, and abort rather than
    // burn the whole budget in a silent retry loop on a systemic failure.
    if (error) {
        fails += 1;
        if (fails >= 5) {
            log(`FATAL: ${fails} consecutive failed invocations — aborting run`);
            break;
        }
        await new Promise((r) => setTimeout(r, 10_000));
    } else {
        fails = 0;
    }
}

// Billed usage. Cost is server-derived and eventually consistent — it can lag a
// few seconds behind the last turn — so give it a chance to land before giving up.
let usage = null;
let usageError = null;
for (let attempt = 0; attempt < 6; attempt++) {
    try {
        usage = await agent.getUsage();
        usageError = null;
        if (usage?.cost) break;
    } catch (err) {
        usageError = err?.message ?? String(err);
        log(`WARN: getUsage attempt ${attempt + 1} failed: ${usageError}`);
        // Entitlement, not a transient hiccup: getUsage is not available on
        // every plan (an individual Pro account gets `feature_unavailable`).
        // Retrying six times cannot fix that, so stop and record the reason —
        // the artifact should say WHY the ledger is missing.
        if (/feature_unavailable|not available for your account/i.test(usageError)) break;
    }
    await new Promise((r) => setTimeout(r, 5_000));
}

writeFileSync(
    `${ART}/cursor-usage.json`,
    JSON.stringify(
        { agent_id: agent.agentId, model: MODEL, billed: usage, usage_error: usageError, streamed, invocations },
        null,
        2,
    ) + "\n",
);
log(`budget exhausted after ${i} invocation(s); usage ${usage ? "captured" : "UNAVAILABLE"}`);

agent.close();
process.exit(0);
