"""Starts and stops containers (Podman or Docker): the rooms, each with its own token and data folder, and the
system services (Tailscale, Cloudflare Tunnel)."""

import asyncio
import logging
import platform
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("lares.runtime")
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "aarch64", "arm64": "aarch64",
        "armv7l": "armv7"}.get(platform.machine().lower(), "amd64")


class ContainerError(RuntimeError):
    pass


def room_container(room_id: str) -> str:
    return f"lares-room-{room_id}"


def service_container(name: str) -> str:
    return f"lares-{name}"


@dataclass
class Service:
    """A system container. env goes in through stdin (it may hold keys); its data folder is mounted at mount."""
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    mount: str = ""
    files: dict[str, str] = field(default_factory=dict)     # written into the data folder before the start


class NoRuntime:
    """For development and tests: rooms are started by hand (lares room dev) and only get a token; their surface
    is expected on this machine at the port from room.yaml."""

    name = "keine"

    def address(self, room_id: str, port: int) -> str:
        return f"127.0.0.1:{port}"

    async def start(self, room, token: str) -> None:
        return None

    async def start_service(self, service: Service) -> None:
        raise ContainerError("Ohne Container-Laufzeit (LARES_RUNTIME) laufen keine Systemdienste")

    async def stop(self, container: str) -> None:
        return None

    async def remove(self, container: str) -> None:
        return None

    async def status(self, container: str) -> str:
        return "manuell"

    async def logs(self, container: str, lines: int = 200) -> str:
        return ""


class ContainerRuntime:
    """Uses the docker or podman command; rooms and services share the network "lares" with the core."""

    def __init__(self, command: str, core_url: str, host_data_dir: Path, local_data_dir: Path | None = None,
                 network: str = "lares"):
        self.name, self.command, self.core_url, self.network = command, command, core_url, network
        self.host_data_dir = host_data_dir                         # the data partition as the host sees it
        self.local_data_dir = local_data_dir or host_data_dir      # … and as the core sees it

    def address(self, room_id: str, port: int) -> str:
        return f"{room_container(room_id)}:{port}"

    async def _run(self, *args: str, check: bool = True, stdin: str | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.command, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate(stdin.encode() if stdin is not None else None)
        text = out.decode(errors="replace").strip()
        if check and proc.returncode:
            raise ContainerError(f"{self.command} {args[0]}: {text[-400:]}")
        return text

    async def _start(self, name: str, image: str, env: dict[str, str], extra: list[str], command: list[str]) -> None:
        if any("\n" in k or "\n" in v for k, v in env.items()):
            raise ContainerError("Zeilenumbruch in einer Umgebungsvariable")
        await self.remove(name)
        await self._run("network", "create", self.network, check=False)
        args = ["run", "-d", "--name", name, "--network", self.network, "--restart", "unless-stopped",
                "--env-file", "/dev/stdin", *extra, image, *command]
        # Tokens go in through stdin, so they never show up in the process list.
        await self._run(*args, stdin="".join(f"{k}={v}\n" for k, v in env.items()))

    async def start(self, room, token: str) -> None:
        manifest = room.manifest
        if not manifest.image:
            raise ContainerError(f"{manifest.name}: room.yaml nennt kein image")
        if manifest.arch and ARCH not in manifest.arch:
            raise ContainerError(f"{manifest.name} gibt es nicht für {ARCH}")
        image = manifest.image.replace("{arch}", ARCH)
        if ":" not in image.rsplit("/", 1)[-1]:
            image += f":{manifest.version}"
        extra = ["--label", f"lares.room={manifest.id}", "-v", f"{self.host_data_dir / 'rooms' / manifest.id}:/data"]
        if manifest.ram:
            extra += ["--memory", manifest.ram]
        await self._start(room_container(manifest.id), image,
                          {"LARES_URL": self.core_url, "LARES_TOKEN": token, "LARES_ROOM": manifest.id}, extra, [])
        log.info("%s gestartet (%s)", manifest.name, image)

    async def start_service(self, service: Service) -> None:
        extra = ["--label", f"lares.dienst={service.name}"]
        if service.mount:
            local = self.local_data_dir / "system" / service.name
            local.mkdir(parents=True, exist_ok=True)
            for filename, content in service.files.items():
                (local / filename).write_text(content)
            extra += ["-v", f"{self.host_data_dir / 'system' / service.name}:{service.mount}"]
        await self._start(service_container(service.name), service.image, service.env, extra, service.command)
        log.info("Dienst %s gestartet (%s)", service.name, service.image)

    async def stop(self, container: str) -> None:
        await self._run("stop", container, check=False)

    async def remove(self, container: str) -> None:
        await self._run("rm", "-f", container, check=False)

    async def status(self, container: str) -> str:
        out = await self._run("inspect", "-f", "{{.State.Status}}", container, check=False)
        return out if out and " " not in out else "fehlt"

    async def logs(self, container: str, lines: int = 200) -> str:
        return await self._run("logs", "--tail", str(lines), container, check=False)
