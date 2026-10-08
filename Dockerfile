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
# Same Python version as the project's .python-version (3.14), so uv uses this one instead of downloading another
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so they're cached between deploys when only the code changes
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The code, the data and the prompts. The project itself isn't installed as a package:
# Python finds it through PYTHONPATH (below), so no packaging step (and no README) is needed.
COPY backend/ ./

# Download the embedding model now, not on every cold start.
# (Only needed if the knowledge base uses Chroma's default embedding function.)
RUN .venv/bin/python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warm-up'])" || true

# The built frontend, served by FastAPI (see main.py)
COPY --from=frontend /frontend/dist ./static

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PORT=8080
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]