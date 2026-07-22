# exec-sandbox — where the Debugging/Unit-Test loop's fixed-path compile/build/test and the
# repair-path tools (install_package/run_command/read_file/git_status/git_diff) actually execute.
# app/integrations/executor.py::MCPExecutor connects here over MCP (streamable-http) at
# SANDBOX_MCP_URL (default http://localhost:8080/mcp).
#
# This container never talks to git remotes/GitHub — publish/push is a host-side concern
# (scripts/local_executor.py); it needs no GitHub credentials and no network beyond the package
# registries allowed by the egress-proxy service in the root docker-compose.yml.
#
# Build (from repo root):
#   docker build -t sdlc-exec-sandbox:latest tools/exec-sandbox

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# git (compile/build/test + git_status/git_diff/git_commit), curl+gnupg (NodeSource setup),
# build-essential (some pip packages need a C toolchain), ca-certificates.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# TypeScript globally, so `npx tsc --noEmit` (the compile check's frontend half, executor.py) works
# even before a generated project's own devDependencies are installed.
RUN npm install -g typescript@^5.6.0

# Fail the build LOUDLY if either runtime is missing — an npm/python-less image would only surface
# as mysterious "command not found" at check time, deep inside a run. Both must be on PATH here.
RUN node --version && npm --version && python --version && git --version

# git identity for the fixed-path git_commit tool — no human operates this container directly.
RUN git config --global user.email "sandbox@sdlc.local" \
    && git config --global user.name "sdlc-exec-sandbox" \
    && git config --global --add safe.directory '*'

# MCP server's own deps.
COPY requirements.txt /opt/exec-sandbox/requirements.txt
RUN pip install --no-cache-dir -r /opt/exec-sandbox/requirements.txt

COPY server.py /opt/exec-sandbox/server.py

ENV WORKSPACE_ROOT=/workspace
RUN mkdir -p /workspace
WORKDIR /workspace

EXPOSE 8080
CMD ["python", "/opt/exec-sandbox/server.py"]
