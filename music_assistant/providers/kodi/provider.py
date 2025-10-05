"""Kodi Player Provider implementation."""

from __future__ import annotations

import asyncio
import aiohttp
from music_assistant.models.player_provider import PlayerProvider
from .player import KodiPlayer


class KodiPlayerProvider(PlayerProvider):
    """Kodi Player provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.kodi_host = self.config.get_value("host", "127.0.0.1")
        self.kodi_port = self.config.get_value("port", 8080)
        self.kodi_user = self.config.get_value("username", "")
        self.kodi_pass = self.config.get_value("password", "")

    async def loaded_in_mass(self) -> None:
        """Called after the provider has been fully loaded into Music Assistant."""
        self.logger.info("KodiPlayerProvider loaded")
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def discover_players(self) -> None:
        """Discover Kodi players for this provider, retrying indefinitely if Kodi is not online yet."""
        url = f"http://{self.kodi_host}:{self.kodi_port}/jsonrpc"
        payload = {"jsonrpc": "2.0", "method": "JSONRPC.Ping", "id": 1}
        interval = 5  # seconds between retries

        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        auth=aiohttp.BasicAuth(self.kodi_user, self.kodi_pass)
                        if self.kodi_user
                        else None,
                        timeout=5,
                    ) as resp:
                        data = await resp.json()
                        if data.get("result") == "pong":
                            break
            except Exception:
                await asyncio.sleep(interval)

        player = KodiPlayer(
            provider=self,
            player_id=f"kodi_{self.kodi_host}_{self.kodi_port}",
            host=self.kodi_host,
            port=self.kodi_port,
            username=self.kodi_user,
            password=self.kodi_pass,
        )
        await self.mass.players.register(player)
        self.logger.info("Registered Kodi player at %s:%s", self.kodi_host, self.kodi_port)
