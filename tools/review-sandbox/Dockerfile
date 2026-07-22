# Ephemeral review-sandbox image for the Code Review agent.
#
# app/integrations/review_sandbox.py starts one container per review:
#   docker run -d --rm --name review-<id> sdlc-review-sandbox:latest sleep <timeout>
# then `docker exec`s into it to: git clone the repo into /work/repo, run ruff, run eslint, and
# run sonar-scanner. So this image must provide, on PATH / at the exact paths the agent calls:
#   * git                                   (clone)
#   * ruff                                   (`ruff check ...`  — Python lint)
#   * /opt/eslint/node_modules/.bin/eslint   (`eslint --config /opt/eslint/eslint.config.mjs ...`)
#   * sonar-scanner  (+ a JRE)               (`sonar-scanner -Dsonar...` — upload to SonarQube)
#
# Build (from services/implementation/):
#   docker build -t sdlc-review-sandbox:latest tools/review-sandbox

FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV SONAR_SCANNER_VERSION=5.0.1.3006

# OS deps: git (clone), a JRE (sonar-scanner is a Java app), python3+pip (ruff), curl/unzip (fetch
# the scanner). procps gives a well-behaved `sleep`/process table for the idle container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl unzip procps \
        python3 python3-pip \
        default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# ruff on PATH (the agent calls a bare `ruff`). --break-system-packages: bookworm's pip is
# PEP-668 "externally managed"; this is a single-purpose container, so a global install is fine.
RUN pip3 install --no-cache-dir --break-system-packages ruff

# sonar-scanner CLI -> /opt/sonar-scanner, symlinked onto PATH.
RUN curl -sSLo /tmp/sonar-scanner.zip \
        "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}-linux.zip" \
    && unzip -q /tmp/sonar-scanner.zip -d /opt \
    && mv "/opt/sonar-scanner-${SONAR_SCANNER_VERSION}-linux" /opt/sonar-scanner \
    && ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner \
    && rm /tmp/sonar-scanner.zip

# ESLint (v9 flat config) preinstalled at /opt/eslint, with a TS/JSX-aware config the agent points
# at explicitly (it never trusts the reviewed repo's own eslint config).
WORKDIR /opt/eslint
COPY eslint.config.mjs /opt/eslint/eslint.config.mjs
# Pin the whole ESLint 9 set to the same major so npm's peer resolution stays consistent
# (an unpinned @eslint/js pulls v10 and conflicts with eslint@9). typescript-eslint 8 targets
# ESLint 9; globals 15 is the flat-config-era release.
RUN npm init -y >/dev/null 2>&1 \
    && npm install --no-audit --no-fund \
        eslint@^9.0.0 @eslint/js@^9.0.0 typescript-eslint@^8.0.0 globals@^15.0.0 \
    && npm cache clean --force

# Where the agent clones + runs everything.
RUN mkdir -p /work/repo
WORKDIR /work

# The agent supplies the run command (`sleep <timeout>`); keep an idle default for manual use.
CMD ["sleep", "infinity"]
