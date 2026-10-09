# syntax=docker/dockerfile:1
# ClearCast: one container, three processes started by deploy/launcher.py.
#   Gradio UI            0.0.0.0:7860   (the only public port; Hugging Face app_port)
#   Fastify gateway      127.0.0.1:8787 (Node.js + TypeScript, internal)
#   FastAPI orchestrator 127.0.0.1:8001 (LangGraph + MCP stdio subprocess, internal)

# ---- Stage 1: build the TypeScript gateway with locked npm dependencies ----
FROM node:22-bookworm-slim AS gateway
WORKDIR /build/gateway
COPY gateway/package.json gateway/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY gateway/tsconfig.json gateway/tsconfig.build.json ./
COPY gateway/src ./src
RUN npm run build && npm prune --omit=dev --no-audit --no-fund

# ---- Stage 2: Python runtime plus the Node.js binary ----
FROM python:3.13-slim-bookworm
COPY --from=gateway /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 user \
    && node --version

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    GRADIO_ANALYTICS_ENABLED=False

USER user
WORKDIR /home/user/app

COPY --chown=user requirements.txt ./
RUN pip install --user -r requirements.txt && pip check

COPY --chown=user agent ./agent
COPY --chown=user api ./api
COPY --chown=user contracts ./contracts
COPY --chown=user deploy ./deploy
COPY --chown=user frontend ./frontend
COPY --chown=user mcp_server ./mcp_server
COPY --chown=user app.py ./
COPY --chown=user gateway/package.json ./gateway/package.json
COPY --chown=user --from=gateway /build/gateway/dist ./gateway/dist
COPY --chown=user --from=gateway /build/gateway/node_modules ./gateway/node_modules

EXPOSE 7860
CMD ["python", "-m", "deploy.launcher"]
