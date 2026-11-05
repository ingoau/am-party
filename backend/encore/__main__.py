"""Entry point: python -m encore."""

from __future__ import annotations

import logging
import os

from aiohttp import web

from .app import create_app
from .config import load_config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("ENCORE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    config = load_config()
    web.run_app(create_app(config), host=config.host, port=config.port, access_log=None)


if __name__ == "__main__":
    main()
