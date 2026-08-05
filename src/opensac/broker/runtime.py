from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn

from opensac.broker.app import create_broker_app
from opensac.broker.service import BrokerService


class BrokerRuntime:
    def __init__(self, service: BrokerService, socket_path: Path) -> None:
        self.service = service
        self.socket_path = socket_path.resolve()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        config = uvicorn.Config(
            create_broker_app(self.service),
            uds=str(self.socket_path),
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self.socket_path.exists():
                return
            if self._task.done():
                await self._task
            await asyncio.sleep(0.02)
        raise RuntimeError("Capability broker did not create its Unix socket")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
        self.socket_path.unlink(missing_ok=True)
