# The Claude Code harness: a container that drives one bench run over MCP.
#
# Contract (any future harness image must honor the same one):
#   - env MCP_URL           — the mineclaude MCP endpoint to drive
#   - env BENCH_RUN_SECONDS — wall-clock budget; the container exits by itself
#   - env BENCH_MODEL       — model id for this entry in the eval matrix
#   - auth via CLAUDE_CODE_OAUTH_TOKEN (subscription) or ANTHROPIC_API_KEY
#   - /skills  (ro)  — the mineclaude skill directory from the repo
#   - /scoring (ro)  — the gamerscore table
#   - /artifacts     — where transcripts/metadata get written
#
# Pin CLAUDE_CODE_VERSION at build time for reproducible benchmark entries.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl jq procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

COPY entrypoint.sh /opt/bench/entrypoint.sh
COPY prompt.md /opt/bench/prompt.md
RUN chmod +x /opt/bench/entrypoint.sh

# Claude Code refuses --dangerously-skip-permissions as root; the stock `node`
# user (uid 1000) also lines up with the default admin uid on the Ubuntu VMs
# the bench runs on, so bind-mounted artifact dirs stay writable.
USER node
WORKDIR /workspace

ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

ENTRYPOINT ["/opt/bench/entrypoint.sh"]
