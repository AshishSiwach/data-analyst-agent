# S28: a single image reproducing the whole environment - app, agent, and
# eval harness - per _docs/technology_stack.md's Docker recommendation.
FROM python:3.11-slim

# Installs uv itself (not a project dependency) via the official
# distroless image, per uv's documented Docker pattern - avoids adding a
# pip-installed uv into the project's own dependency graph.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first, so this layer is cached across code-only changes.
# --no-install-project defers installing this package itself until its
# source is copied below; pyproject.toml's metadata is enough for uv to
# resolve and install everything it depends on.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

# .git isn't shipped in the image (see .dockerignore), so eval/harness.py's
# commit-hash stamp can't shell out to `git rev-parse` at runtime - baked
# in at build time instead, via `--build-arg GIT_COMMIT=$(git rev-parse HEAD)`.
ARG GIT_COMMIT=unknown

ENV PATH="/app/.venv/bin:$PATH" \
    DUCKDB_PATH=/app/data_analyst_agent.duckdb \
    STREAMLIT_SERVER_HEADLESS=true \
    GIT_COMMIT=$GIT_COMMIT

EXPOSE 8501

RUN chmod +x docker/entrypoint.sh
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["streamlit", "run", "data_analyst_agent/app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
