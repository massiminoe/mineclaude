# Container image for the mineclaude Python runtime (MCP server + monitor +
# bridge event loop). On the local dev workflow `mineclaude` runs on the host;
# in the bench stack it is the fourth container, sitting between the mc-client
# bridge and the harness.
#
# Build context is the repo root:
#   docker build -f bench/mineclaude.Dockerfile .
FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mineclaude/ ./mineclaude/
RUN pip install --no-cache-dir .

# Compose-network defaults; MCP_HOST must be 0.0.0.0 so the harness container
# can reach the MCP server (the host-process default is 127.0.0.1).
ENV BRIDGE_URL=http://mc-client:8081 \
    BRIDGE_WS_URL=ws://mc-client:8082/events \
    MCP_HOST=0.0.0.0

# Session logs land in state/sessions relative to CWD (/app) — the bench
# compose bind-mounts the per-run artifacts dir there.
EXPOSE 5555 5556

HEALTHCHECK --interval=5s --timeout=3s --retries=24 --start-period=10s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 5556), 2)"

CMD ["mineclaude"]
