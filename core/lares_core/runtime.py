"""Starts and stops the room containers (Podman or Docker), each with its own token and data folder."""

import asyncio
import logging
import platform
from pathlib import Path

log = logging.getLogger("lares.runtime")
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "aarch64", "arm64": "aarch64",
        "armv7l": "armv7"}.get(platform.machine().lower(), "amd64")


class ContainerError(RuntimeError):
    pass


class NoRuntime:
    """For development and tests: rooms are started by hand (lares room dev) and only get a token."""

    name = "keine"

    async def start(self, room, token: str) -> None:
        return None

    async def stop(self, room_id: str) -> None:
        return None

    async def remove(self, room_id: str) -> None:
        return None

    async def status(self, room_id: str) -> str:
        return "manuell"

    async def logs(self, room_id: str, lines: int = 200) -> str:
        return ""


class ContainerRuntime:
    """Uses the docker or podman command; the rooms share the network "lares" with the core."""

    def __init__(self, command: str, core_url: str, host_rooms_dir: Path, network: str = "lares"):
        self.name, self.command, self.core_url = command, command, core_url
        self.host_rooms_dir, self.network = host_rooms_dir, network

    @staticmethod
    def container(room_id: str) -> str:
        return f"lares-room-{room_id}"

    async def _run(self, *args: str, check: bool = True, stdin: str | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.command, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate(stdin.encode() if stdin is not None else None)
        text = out.decode(errors="replace").strip()
        if check and proc.returncode:
            raise ContainerError(f"{self.command} {args[0]}: {text[-400:]}")
        return text

    async def start(self, room, token: str) -> None:
        manifest = room.manifest
        if not manifest.image:
            raise ContainerError(f"{manifest.name}: room.yaml nennt kein image")
        if manifest.arch and ARCH not in manifest.arch:
            raise ContainerError(f"{manifest.name} gibt es nicht für {ARCH}")
        image = manifest.image.replace("{arch}", ARCH)
        if ":" not in image.rsplit("/", 1)[-1]:
            image += f":{manifest.version}"
        await self.remove(manifest.id)
        await self._run("network", "create", self.network, check=False)
        args = ["run", "-d", "--name", self.container(manifest.id), "--network", self.network,
                "--restart", "unless-stopped", "--label", f"lares.room={manifest.id}",
                "--env-file", "/dev/stdin", "-v", f"{self.host_rooms_dir / manifest.id}:/data"]
        if manifest.ram:
            args += ["--memory", manifest.ram]
        # The token goes in through stdin, so it never shows up in the process list.
        env = f"LARES_URL={self.core_url}\nLARES_TOKEN={token}\nLARES_ROOM={manifest.id}\n"
        await self._run(*args, image, stdin=env)
        log.info("%s gestartet (%s)", manifest.name, image)

    async def stop(self, room_id: str) -> None:
        await self._run("stop", self.container(room_id), check=False)

    async def remove(self, room_id: str) -> None:
        await self._run("rm", "-f", self.container(room_id), check=False)

    async def status(self, room_id: str) -> str:
        out = await self._run("inspect", "-f", "{{.State.Status}}", self.container(room_id), check=False)
        return out if out and " " not in out else "fehlt"

    async def logs(self, room_id: str, lines: int = 200) -> str:
        return await self._run("logs", "--tail", str(lines), self.container(room_id), check=False)
