FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --extra azure --no-install-project
COPY backend ./backend
COPY fakeline ./fakeline
EXPOSE 8000
# Long-lived WebSockets (calls can last an hour): run on Container Apps, never serverless.
CMD ["uv", "run", "--no-sync", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-ping-interval", "20"]
