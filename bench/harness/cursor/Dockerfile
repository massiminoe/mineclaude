# The Cursor harness: drives one bench run over MCP with @cursor/sdk.
#
# Build context is bench/harness/ so prompt.md + common.sh are shared with the
# other harness images; the env/mount contract is identical (see common.sh).
# Auth: CURSOR_API_KEY (Cursor dashboard -> API Keys; usage draws on the
# account's plan). Pin BENCH_HARNESS_VERSION for reproducible entries.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl jq procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The SDK needs node >= 22.13 and pulls its platform binary from an optional
# dependency (@cursor/sdk-linux-x64 / -arm64), so the image must be built for
# the architecture it runs on — true for both the amd64 bench VMs and an arm64
# dev machine.
ARG BENCH_HARNESS_VERSION=latest
RUN npm install --no-audit --no-fund --prefix /opt/bench "@cursor/sdk@${BENCH_HARNESS_VERSION}"

COPY prompt.md /opt/bench/prompt.md
COPY common.sh /opt/bench/common.sh
COPY cursor/driver.mjs /opt/bench/driver.mjs
COPY cursor/entrypoint.sh /opt/bench/entrypoint.sh
RUN chmod +x /opt/bench/entrypoint.sh && chown -R node:node /opt/bench

# uid 1000 lines up with the default admin uid on the bench VMs, so
# bind-mounted artifact dirs stay writable.
USER node
WORKDIR /workspace

ENV HOME=/home/node

ENTRYPOINT ["/opt/bench/entrypoint.sh"]
