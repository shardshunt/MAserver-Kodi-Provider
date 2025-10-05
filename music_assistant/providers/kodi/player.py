from __future__ import annotations
import aiohttp
import asyncio
import time
from typing import TYPE_CHECKING, Any
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import KodiPlayerProvider

class KodiPlayer(Player):
    """Kodi Player in Music Assistant."""

    def __init__(self, provider: KodiPlayerProvider, player_id: str, host: str, port: int, username: str, password: str) -> None:
        super().__init__(provider, player_id)
        self.host = host
        self.port = port
        self.username = username
        self.password = password

        #for poll count logic
        self._poll_ready = asyncio.Event()
        self._poll_count = 0
        
        self._attr_name = f"Kodi Player {host}:{port}"
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.ENQUEUE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.POWER,
        }
        self._set_attributes()

    async def _jsonrpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"http://{self.host}:{self.port}/jsonrpc"
        auth = aiohttp.BasicAuth(self.username, self.password) if self.username else None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            payload["params"] = params

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, auth=auth) as resp:
                    result: dict[str, Any] = await resp.json()
                    return result
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            self.logger.warning("Failed to reach Kodi at %s: %s", url, err)
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
            self.update_state()
            return {}

    @property
    def needs_poll(self) -> bool:
        return True

    @property
    def poll_interval(self) -> int:
        return 5 if self.playback_state == PlaybackState.PLAYING else 30

    async def play(self) -> None:
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            player_id = players[0]["playerid"]
            self.logger.debug("Active player found with ID %s, sending PlayPause command", player_id)
            await self._jsonrpc("Player.PlayPause", {"playerid": player_id, "play": True})
        elif self._attr_current_media:
            self.logger.debug("No active players, playing current media %s", self._attr_current_media.uri)
            await self.play_media(self._attr_current_media)
        else:
            self.provider.logger.warning("No media loaded to play")
            return

        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()
        asyncio.create_task(self.poll())

    async def stop(self) -> None:
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            await self._jsonrpc("Player.Stop", {"playerid": players[0]["playerid"]})
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        self.logger.debug("Opening media in Kodi with URL: %s", media.uri)

        resp = await self._jsonrpc("Player.Open", {"item": {"file": media.uri}})
        self.logger.debug("Kodi Player.Open response: %s", resp)

        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()
        asyncio.create_task(self.poll())

    async def volume_set(self, volume_level: int) -> None:
        await self._jsonrpc("Application.SetVolume", {"volume": volume_level})
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        await self._jsonrpc("Application.SetMute", {"mute": muted})
        self._attr_volume_muted = muted
        self.update_state()

    async def seek(self, position: int) -> None:
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            await self._jsonrpc("Player.Seek", {"playerid": players[0]["playerid"], "value": position})
        self.update_state()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        await self._poll_ready.wait()
        self.logger.debug("Attempting to enqueue media for Kodi: %s", media.uri)

        # Get current playlist items
        playlist_resp = await self._jsonrpc(
            "Playlist.GetItems",
            {"playlistid": 0, "properties": ["file"]}
        )
        playlist_items = playlist_resp.get("result", {}).get("items", []) or []

        # Skip if media already in playlist
        if any(item.get("file") == media.uri for item in playlist_items):
            self.logger.warning("Skipping enqueue: %s already in playlist", media.uri)
            return

        await self._jsonrpc("Playlist.Add", {
            "playlistid": 0,
            "item": {"file": media.uri}
        })
        self.logger.debug("Enqueued new media for Kodi: %s", media.uri)

    async def on_unload(self) -> None:
        self.logger.info("Kodi player %s unloaded", self.name)

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        logger = self.provider.logger.getChild(self.player_id)
        if powered:
            logger.info("Received POWER ON command on player %s", self._attr_name)
            self._attr_powered = True
        else:
            logger.info("Received POWER OFF command on player %s", self._attr_name)
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def _set_attributes(self) -> None:
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_volume_level = 50

    async def poll(self) -> None:
        """Poll Kodi for playback progress and update playback state, handling track changes and seeks."""
        try:
            now = time.time()

            # Get active players
            players_resp = await self._jsonrpc("Player.GetActivePlayers")
            players = players_resp.get("result", [])

            if not players:
                # Short recheck to avoid false idle
                await asyncio.sleep(1)
                try:
                    recheck_resp = await self._jsonrpc("Player.GetActivePlayers")
                    players = recheck_resp.get("result", [])
                except Exception:
                    players = []

                if not players:
                    self._attr_playback_state = PlaybackState.IDLE
                    self._attr_elapsed_time = 0
                    self._attr_elapsed_time_last_updated = now
                    self._attr_powered = False
                    self.update_state()
                    return
                now = time.time()

            player_id = players[0]["playerid"]

            # Get current playing item
            item_resp = await self._jsonrpc(
                "Player.GetItem",
                {"playerid": player_id, "properties": ["file"]},
            )
            item = item_resp.get("result", {}).get("item")
            current_media_uri = item.get("file") if item else None

            # Track change detection
            if self._attr_current_media is None or self._attr_current_media.uri != current_media_uri:
                self._attr_elapsed_time = 0
                self._attr_elapsed_time_last_updated = now
                if current_media_uri:
                    self._attr_current_media = PlayerMedia(uri=current_media_uri)

            # Get playback properties
            props = await self._jsonrpc(
                "Player.GetProperties",
                {"playerid": player_id, "properties": ["time", "speed"]},
            )
            result = props.get("result", {})
            time_obj = result.get("time", {})
            speed = result.get("speed", 0)

            # Get volume
            try:
                app_props = await self._jsonrpc("Application.GetProperties", {"properties": ["volume"]})
                volume = app_props.get("result", {}).get("volume")
            except Exception as e:
                self.logger.debug("Failed to get volume: %s", e)
                volume = None

            if volume is not None:
                self._attr_volume_level = volume

            # Convert time object to seconds
            current_seconds = (
                time_obj.get("hours", 0) * 3600
                + time_obj.get("minutes", 0) * 60
                + time_obj.get("seconds", 0)
            )

            last_update = self._attr_elapsed_time_last_updated or now
            last_known = self._attr_elapsed_time or 0
            elapsed_real = now - last_update

            # Handle backward seek
            if current_seconds < last_known - 1:
                self.logger.debug("Time: Handle backward seek: last_known=%.2f, current_seconds=%.2f", last_known, current_seconds)
                self._attr_elapsed_time = current_seconds
            # Handle stalled playback
            elif speed > 0 and current_seconds <= last_known:
                self.logger.debug("Time: Handle stalled playback: last_known=%.2f, current_seconds=%.2f", current_seconds, last_known)
                current_seconds = last_known + elapsed_real
                self._attr_elapsed_time = current_seconds
            else:
                self._attr_elapsed_time = current_seconds

            self._attr_elapsed_time_last_updated = now

            # Determine playback state
            if speed > 0:
                state = PlaybackState.PLAYING
            elif speed == 0 and self._attr_elapsed_time > 0:
                state = PlaybackState.PAUSED
            else:
                state = PlaybackState.IDLE

            self._attr_playback_state = state
            self._attr_powered = True

            # Logging on changes
            log_required = (
                getattr(self, "_last_speed", None) != speed
                or getattr(self, "_last_volume", None) != volume
                or getattr(self, "_last_media_uri", None) != current_media_uri
                or getattr(self, "_last_state", None) != state
                or getattr(self, "_last_powered", None) != self._attr_powered
            )

            if log_required:
                self.logger.debug(
                    "Kodi properties: speed=%s, volume=%s, current_media=%s, state=%s, powered=%s",
                    speed, volume, current_media_uri, state, self._attr_powered
                )
                self._last_speed = speed
                self._last_volume = volume
                self._last_media_uri = current_media_uri
                self._last_state = state
                self._last_powered = self._attr_powered
        
            self.update_state()
            self._poll_count += 1
            if self._poll_count >= 2:  # wait until 2 successful polls
                self._poll_ready.set()

        except Exception as err:
            self.logger.warning("Error polling Kodi player: %s; setting power OFF", err)
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
            self.update_state()
