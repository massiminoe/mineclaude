# The opencode harness: drives one bench run over MCP with `opencode run`.
#
# Build context is bench/harness/ so prompt.md + common.sh are shared with the
# other harness images; the env/mount contract is identical (see common.sh).
# Auth: OPENCODE_API_KEY (opencode Zen / Go subscription key) — the model id
# carries the provider, e.g. BENCH_MODEL=opencode-go/qwen3.8-flash.
# Pin BENCH_HARNESS_VERSION for reproducible entries.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl jq procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG BENCH_HARNESS_VERSION=latest
RUN npm install -g "opencode-ai@${BENCH_HARNESS_VERSION}"

COPY prompt.md /opt/bench/prompt.md
COPY common.sh /opt/bench/common.sh
COPY opencode/entrypoint.sh /opt/bench/entrypoint.sh
RUN chmod +x /opt/bench/entrypoint.sh

# uid 1000 lines up with the default admin uid on the bench VMs, so
# bind-mounted artifact dirs stay writable.
USER node
WORKDIR /workspace

# opencode writes config/state under HOME + XDG dirs; both must be writable or
# it dies before it ever reaches the model.
ENV HOME=/home/node \
    XDG_CONFIG_HOME=/home/node/.config \
    XDG_DATA_HOME=/home/node/.local/share \
    XDG_CACHE_HOME=/home/node/.cache

ENTRYPOINT ["/opt/bench/entrypoint.sh"]
