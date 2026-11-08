"""Entry point: python -m am_party."""

from __future__ import annotations

import logging
import os

from aiohttp import web

from .app import create_app
from .config import load_config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AM_PARTY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    config = load_config()
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        access_log=None,
        # handlers still running this long after SIGTERM are cancelled; stays well
        # under Docker's 10 s stop grace period so cleanup always gets to run
        shutdown_timeout=3,
    )


if __name__ == "__main__":
    main()
