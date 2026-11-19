FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN useradd -r app && chown -R app /srv
USER app
ENV PATH="/srv/.venv/bin:$PATH"
EXPOSE 8000
# API: default. Worker: `python -m app.worker`. Migrations (one-off task): `alembic upgrade head`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
