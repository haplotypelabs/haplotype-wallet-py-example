FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked

ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8787
EXPOSE 8787
USER nobody
CMD ["wallet-example"]
