"""python -m lares_core – starts Lares Core and its gateways.

LARES_DATA        data partition (default /data)
LARES_PORT        port of the API (default 8080)
LARES_HEIM_PORT   gateway for the home network and Tailscale (default 8000)
LARES_EXTERN_PORT gateway for cloudflared, only inside the network "lares" (default 8001)
LARES_RUNTIME     docker, podman or keine (rooms are started by hand – for development)
LARES_CORE_HOST   name of the core in the network "lares" (default lares-core)
LARES_CORE_URL    how the rooms reach the core (default http://<host>:<port>/api/v1)
LARES_HOST_DATA   path of the data partition on the host, if the core itself runs in a container
LARES_ROOMS_DIR   folder of the rooms' data (default <data>/rooms; on Lares OS set at boot: stick or disk)
LARES_OS          1 on Lares OS: the core runs on the host and manages where the rooms live
LARES_BIND, LARES_HEIM_BIND, LARES_EXTERN_BIND – addresses to listen on (comma-separated, default 0.0.0.0)
"""

import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from .api import make_app, make_runtime
from .gateway import Gateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lares")


async def serve() -> None:
    data = Path(os.environ.get("LARES_DATA", "/data"))
    port = int(os.environ.get("LARES_PORT", "8080"))
    home_port = int(os.environ.get("LARES_HEIM_PORT", "8000"))
    public_port = int(os.environ.get("LARES_EXTERN_PORT", "8001"))
    core_host = os.environ.get("LARES_CORE_HOST", "lares-core")
    core_url = os.environ.get("LARES_CORE_URL", f"http://{core_host}:{port}/api/v1")
    host_data = Path(os.environ.get("LARES_HOST_DATA", str(data)))
    rooms = Path(os.environ["LARES_ROOMS_DIR"]) if os.environ.get("LARES_ROOMS_DIR") else None
    lares_os = os.environ.get("LARES_OS") == "1"
    runtime = make_runtime(os.environ.get("LARES_RUNTIME", "keine"), core_url, host_data, data,
                           host_rooms_dir=Path(os.environ.get("LARES_HOST_ROOMS") or rooms) if rooms else None,
                           core_on_host=lares_os)
    core = make_app(data, runtime, core_host=core_host, home_port=home_port, public_port=public_port,
                    rooms_dir=rooms, lares_os=lares_os)
    runners = []
    for app, app_port, bind in ((core, port, "LARES_BIND"),
                                (Gateway(core, public=False).app(), home_port, "LARES_HEIM_BIND"),
                                (Gateway(core, public=True).app(), public_port, "LARES_EXTERN_BIND")):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        for host in os.environ.get(bind, "0.0.0.0").split(","):
            await web.TCPSite(runner, host.strip(), app_port).start()
        runners.append(runner)
    log.info("Lares Core läuft: API %s, Heim-Gateway %s, Internet-Gateway %s", port, home_port, public_port)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        for runner in reversed(runners):
            await runner.cleanup()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
