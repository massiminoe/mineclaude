FROM node:22-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl jq procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG BENCH_HARNESS_VERSION=0.153.4
RUN npm install -g @openai/codex@${BENCH_HARNESS_VERSION}
COPY prompt.md common.sh /opt/bench/
COPY codex/entrypoint.sh /opt/bench/entrypoint.sh
RUN chmod +x /opt/bench/entrypoint.sh \
    && mkdir -p /workspace && chown node:node /workspace
USER node
WORKDIR /workspace
ENTRYPOINT ["/opt/bench/entrypoint.sh"]
