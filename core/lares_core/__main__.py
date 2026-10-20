"""python -m lares_core – starts Lares Core.

LARES_DATA       data partition (default /data)
LARES_PORT       port of the API (default 8080)
LARES_RUNTIME    docker, podman or keine (rooms are started by hand – for development)
LARES_CORE_URL   how the rooms reach the core (default http://lares-core:<port>/api/v1)
LARES_HOST_DATA  path of the data partition on the host, if the core itself runs in a container
"""

import logging
import os
from pathlib import Path

from aiohttp import web

from .api import make_app, make_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    data = Path(os.environ.get("LARES_DATA", "/data"))
    port = int(os.environ.get("LARES_PORT", "8080"))
    core_url = os.environ.get("LARES_CORE_URL", f"http://lares-core:{port}/api/v1")
    host_data = Path(os.environ.get("LARES_HOST_DATA", str(data)))
    runtime = make_runtime(os.environ.get("LARES_RUNTIME", "keine"), core_url, host_data / "rooms")
    web.run_app(make_app(data, runtime), port=port, access_log=None)


if __name__ == "__main__":
    main()
