# 1. The mobile web app (Next.js static export -> /web/out).
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
ARG NEXT_PUBLIC_CONVEX_URL=""
ENV NEXT_PUBLIC_CONVEX_URL=$NEXT_PUBLIC_CONVEX_URL
RUN npm run build

# 2. The backend, which serves the web app at / and the API.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
# `azure` extra: Key Vault secrets + Application Insights (azure-monitor-opentelemetry).
RUN uv sync --locked --no-dev --extra azure --no-install-project
COPY backend ./backend
COPY fakeline ./fakeline
# The console (backup dashboard + phone-mode rep page) and eval results for /metrics.
COPY dashboard ./dashboard
COPY --from=web /web/out ./web/out
COPY evals ./evals
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
# Long-lived WebSockets (calls can last an hour): run on Container Apps, never serverless.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-ping-interval", "20", "--proxy-headers", "--forwarded-allow-ips", "*"]
