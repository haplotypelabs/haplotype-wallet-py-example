"""Run the example: `uv run --env-file .env wallet-example`."""

import os

import uvicorn

from wallet_example.app import create_app
from wallet_example.config import load_settings


def main() -> None:
    settings = load_settings()
    print(f"Haplotype Wallet example running at {settings.app_url}")
    # HOST defaults to localhost; the Docker image sets it to 0.0.0.0.
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(create_app(settings), host=host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
