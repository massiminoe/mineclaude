#!/usr/bin/env bash
# Sourced after argument parsing by local, launch, and sweep entry points.
# Keep validation before credential reads or cloud mutations.
if [[ "$HARNESS" == "codex" ]]; then
    case "$MODEL" in
        gpt-6-astra|gpt-5.6-luna|gpt-5.6-terra|gpt-5.6-sol) ;;
        *) echo "Codex bench requires --model gpt-6-astra, gpt-5.6-luna, gpt-5.6-terra, or gpt-5.6-sol" >&2; exit 2 ;;
    esac
    case "$REASONING_EFFORT" in
        ''|minimal|low|medium|high|xhigh) ;;
        *) echo "Unsupported --reasoning-effort: $REASONING_EFFORT" >&2; exit 2 ;;
    esac
elif [[ -n "$REASONING_EFFORT" ]]; then
    echo "--reasoning-effort is only supported by the codex harness" >&2
    exit 2
fi
