"""Explicit NoneBot driver and supervised reverse WebSocket service lifecycle."""

import asyncio
from socket import socket

import uvicorn
from nonebot.adapters.onebot.v11 import Bot
from nonebot.config import Config, Env
from nonebot.drivers.fastapi import Driver
from nonebot.matcher import Matcher

from ginko.adapters.onebot import OneBotAdapter, register_ingress
from ginko.runtime import Runtime


class ExplicitConfig(Config):
    @staticmethod
    def _settings_build_values(
        settings_cls, init_kwargs, env_file, env_file_encoding, env_nested_delimiter
    ):
        # NoneBot 2.5's public init reads .env even with _env_file=None, and a normal
        # Config reads process environment before applying kwargs. Keep only our input.
        return init_kwargs


class Gateway:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        config = runtime.config
        settings = config.settings.onebot
        self.driver = Driver(
            Env(_env_file=None, environment="ginko"),
            ExplicitConfig(
                _env_file=None,
                driver="~fastapi",
                host=settings.host,
                port=settings.port,
                api_timeout=settings.api_timeout_seconds,
                superusers=set(),
                nickname=set(),
                command_start=set(),
                command_sep=set(),
            ),
        )
        self.matcher: type[Matcher] | None = None
        # Shutdown hooks run in reverse: stop tasks/admission, drain SDK handlers,
        # then close SQLite and release the process lock.
        self.driver.on_shutdown(runtime.close)
        self.adapter = OneBotAdapter(
            self.driver, settings=config.settings, access_token=config.onebot_access_token
        )
        self.driver.on_startup(self._start)
        self.driver.on_shutdown(self._stop)
        self.driver.on_bot_connect(self._connected)
        self.driver.on_bot_disconnect(self._disconnected)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.driver.server_app,
                host=settings.host,
                port=settings.port,
                workers=1,
                reload=False,
                access_log=False,
                log_level="warning",
                ws="websockets-sansio",
                ws_max_size=128 * 1024,
                timeout_graceful_shutdown=5,
            )
        )

    async def _start(self) -> None:
        await self.runtime.start()
        self.matcher = register_ingress(
            self.runtime.ingress, adapter=self.adapter, on_failure=self.runtime.fail
        )

    async def _stop(self) -> None:
        await self.runtime.stop()
        if self.matcher is not None:
            self.matcher.destroy()
            self.matcher = None

    async def _connected(self, bot: Bot) -> None:
        if bot.adapter is self.adapter:
            self.runtime.set_connected(bot.self_id in self.adapter.connections)

    async def _disconnected(self, bot: Bot) -> None:
        if bot.adapter is self.adapter:
            self.runtime.set_connected(bot.self_id in self.adapter.connections)

    async def serve(self, *, sockets: list[socket] | None = None) -> None:
        server = asyncio.create_task(self.server.serve(sockets=sockets), name="ginko-gateway")
        failed = asyncio.create_task(self.runtime.wait_failed(), name="ginko-supervisor")
        try:
            finished, _ = await asyncio.wait({server, failed}, return_when=asyncio.FIRST_COMPLETED)
            if failed in finished:
                self.server.should_exit = True
                await server
                await failed
            await server
            if not self.server.started:
                raise RuntimeError("gateway startup failed")
        finally:
            self.server.should_exit = True
            failed.cancel()
            await asyncio.gather(server, failed, return_exceptions=True)
            await self.runtime.close()
