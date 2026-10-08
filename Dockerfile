# One container for the whole app: FastAPI serves /api/chat and the built React frontend.
# Built by Cloud Build (gcloud run deploy --source .), so Docker isn't needed on your machine.

# ---------- 1. Build the React frontend ----------
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- 2. The Python backend ----------
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first, so they're cached between deploys when only the code changes
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The code, the data and the prompts
COPY backend/ ./
RUN uv sync --frozen --no-dev

# Download the embedding model now, not on every cold start.
# (Only needed if the knowledge base uses Chroma's default embedding function.)
RUN .venv/bin/python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warm-up'])" || true

# The built frontend, served by FastAPI (see main.py)
COPY --from=frontend /frontend/dist ./static

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8080
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]