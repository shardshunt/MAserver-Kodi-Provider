from __future__ import annotations
import aiohttp
import asyncio
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant_models.media_items import AudioFormat
import time
from typing import TYPE_CHECKING
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType, ContentType
from music_assistant.models.player import Player, PlayerMedia
from contextlib import suppress

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
        self._attr_name = f"Kodi Player {host}:{port}"
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.ENQUEUE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
        }
        self._set_attributes()

    async def _jsonrpc(self, method: str, params: dict | None = None) -> dict:
        uri = f"http://{self.host}:{self.port}/jsonrpc"
        auth = aiohttp.BasicAuth(self.username, self.password) if self.username else None
        payload = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            payload["params"] = params
            # self.logger.debug("Sending JSON-RPC request to Kodi: %s with params: %s", method, params)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(uri, json=payload, auth=auth, timeout=10) as resp:
                    resp_json = await resp.json()
                    # self.logger.debug("Received JSON-RPC response from Kodi: %s", resp_json)
                    return resp_json
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            self.logger.warning("Failed to reach Kodi at %s: %s", uri, err)
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
            self.update_state()
            return {}
        
    async def _get_kodi_library_song(self, media: PlayerMedia) -> int | None:
        """
        Check if Kodi has this track in its library by title + artist.
        Returns the Kodi songid if found, otherwise None.
        """
        title = getattr(media, "title", None) or getattr(media, "name", None)
        artist = getattr(media, "artist", None)
        if not title or not artist:
            return None

        self.logger.debug("Searching Kodi library for track: %s by artist: %s", title, artist)

        # Query Kodi library for songs matching the title
        resp = await self._jsonrpc("AudioLibrary.GetSongs", {
            "filter": {"field": "title", "operator": "is", "value": title}, "properties": ["artist"]
        })

        songs = resp.get("result", {}).get("songs", [])
        for song in songs:
            song_artists = song.get("artist", [])
            if artist in song_artists:
                self.logger.debug("Found track in Kodi library: %s by %s (songid=%s)", song.get("label"), song_artists, song.get("songid"))
                return song.get("songid")

        return None
    
    async def play_dummy_url(self, url: str):
        """ Plays a dummy stream to make MA think the song is being played and therefore allow progress updates"""
        input_format = AudioFormat(
            content_type=ContentType.UNKNOWN,
            channels=2,
            sample_rate=44100,
            bit_depth=16,
        )
        output_format = AudioFormat(
            content_type=ContentType.NUT,
            channels=2,
            sample_rate=44100,
            bit_depth=16,
        )
        async with FFMpeg(
            audio_input=url,
            input_format=input_format,
            output_format=output_format,
            audio_output="NULL"
        ) as ffmpeg_proc:
            await ffmpeg_proc.wait()

    @property
    def needs_poll(self) -> bool:
        return True

    @property
    def poll_interval(self) -> int:
        return 5 if self.playback_state == PlaybackState.PLAYING else 30

    async def play(self) -> None:
        """Handle Play command on kodi."""
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            await self._jsonrpc("Player.PlayPause", {"playerid": players[0]["playerid"], "play": True})
            self._attr_playback_state = PlaybackState.PLAYING
            self.logger.debug("Playback state set to PLAYING")
            self.update_state()
    
    async def pause(self) -> None:
        """Handle pause command on kodi."""
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            await self._jsonrpc("Player.PlayPause", {"playerid": players[0]["playerid"], "play": False})
            self._attr_playback_state = PlaybackState.PAUSED
            self.logger.debug("Playback state set to PAUSED")
            self.update_state()

    async def stop(self) -> None:
        """Handle Stop command on kodi."""
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            await self._jsonrpc("Player.Stop", {"playerid": players[0]["playerid"]})
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
            self.logger.debug("Playback state set to IDLE")
            self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        self.logger.debug("Play media command received: %s", media.uri)
        # set current media so poll/dummy logic knows the stream URI
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()

        kodi_songid = await self._get_kodi_library_song(media)
        if kodi_songid is not None:
            resp = await self._jsonrpc("Player.Open", {"item": {"songid": kodi_songid}})
            self.logger.debug("Playing Kodi library songid=%s, resp=%s", kodi_songid, resp)
            # start dummy so MA can track using the stream URI
            if getattr(self, "_dummy_task", None) and not self._dummy_task.done():
                self._dummy_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._dummy_task
            self._dummy_task = asyncio.create_task(self.play_dummy_url(media.uri))
        else:
            resp = await self._jsonrpc("Player.Open", {"item": {"file": media.uri}})
            self.logger.debug("Playing stream URL on Kodi fallback, resp=%s", resp)

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
        self.logger.debug("Seeking to: %s", position)
        if players := (await self._jsonrpc("Player.GetActivePlayers")).get("result"):
            await self._jsonrpc("Player.Seek", {"playerid": players[0]["playerid"], "value": position})
        self.update_state()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        # store next media so poll can promote it when Kodi advances
        self._next_media = media

        songid = await self._get_kodi_library_song(media)
        if songid is not None:
            await self._jsonrpc("Playlist.Add", {"playlistid": 0, "item": {"songid": songid}})
            self.logger.debug("Media enqueued from Kodi library: %s (songid=%s)", getattr(media, "title", None), songid)
        else:
            await self._jsonrpc("Playlist.Add", {"playlistid": 0, "item": {"file": media.uri}})
            self.logger.debug("Media enqueued via stream: %s", getattr(media, "uri", None))


    async def on_unload(self) -> None:
        self.logger.info("Kodi player %s unloaded", self.name)

    def _set_attributes(self) -> None:
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_volume_level = 50
        self._attr_current_track = None

    async def poll(self) -> None:
        """Poll Kodi for playback progress and update playback state, handling track changes and seeks."""
        try:
            # Get active players
            players = (await self._jsonrpc("Player.GetActivePlayers")).get("result", [])
            if not players:
                self._attr_playback_state = PlaybackState.IDLE
                self.logger.debug("No active players, playback state set to IDLE")
                self.update_state()
                return

            player_id = players[0]["playerid"]

            # Get playback properties
            props_resp = await self._jsonrpc(
                "Player.GetProperties",
                {"playerid": player_id, "properties": ["time", "totaltime", "speed", "percentage"]}
            )
            props = props_resp.get("result", {})

            # Get current item
            item_resp = await self._jsonrpc("Player.GetItem", {"playerid": player_id})
            item = item_resp.get("result", {}).get("item", {})

            # Update playback state
            speed = props.get("speed", 0)
            self._attr_playback_state = PlaybackState.PLAYING if speed == 1 else PlaybackState.PAUSED

            # Track change detection by Kodi label/file
            track_id = item.get("label") or item.get("file") or getattr(self._attr_current_media, "uri", None)
            prev_id = getattr(self._attr_current_track, "songid", None)

            if not self._attr_current_track or prev_id != track_id:
                # Update current_track (Kodi info)
                self._attr_current_track = PlayerMedia(
                    uri=item.get("file") or getattr(self._attr_current_media, "uri", None),
                    title=item.get("label"),
                    artist=(item.get("artist")[0] if item.get("artist") else None),
                )
                self._attr_current_track.songid = track_id
                self._attr_elapsed_time = 0
                self._attr_elapsed_time_last_updated = time.time()
                self.logger.debug("Track changed, new track: %s", item.get("label"))

                # If we have a pre-stored next media, promote it to current_media first
                if hasattr(self, "_next_media") and self._next_media is not None:
                    self._attr_current_media = self._next_media
                    self._next_media = None
                    self.logger.debug("Promoted queued media to current_media: %s", getattr(self._attr_current_media, "uri", None))

                # Ensure only one dummy task runs for the current track
                if getattr(self, "_dummy_task", None) and not self._dummy_task.done():
                    self.logger.debug("Cancelling existing dummy task")
                    self._dummy_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._dummy_task

                # Start dummy playback only if there is a valid stream URI to play
                stream_uri = getattr(self._attr_current_media, "uri", None)
                if stream_uri:
                    self._dummy_task = asyncio.create_task(self.play_dummy_url(stream_uri))
                    self.logger.debug("Started dummy playback for: %s", stream_uri)

            # Update elapsed time
            time_props = props.get("time", {})
            self._attr_elapsed_time = (
                time_props.get("hours", 0) * 3600 +
                time_props.get("minutes", 0) * 60 +
                time_props.get("seconds", 0)
            )
            self._attr_elapsed_time_last_updated = time.time()

            # Update total time
            totaltime = props.get("totaltime", {})
            total_seconds = (
                totaltime.get("hours", 0) * 3600 +
                totaltime.get("minutes", 0) * 60 +
                totaltime.get("seconds", 0)
            )

            self._attr_total_time = total_seconds

            # Volume detection
            vol_resp = await self._jsonrpc("Application.GetProperties", {"properties": ["volume", "muted"]})
            vol_result = vol_resp.get("result", {})
            self._attr_volume_level = vol_result.get("volume", self._attr_volume_level)
            self._attr_volume_muted = vol_result.get("muted", self._attr_volume_muted)
            
            self.logger.debug(
                "Playback state: %s | Track: %s | track_id: %s | Elapsed: %s s | Total: %s s | Volume: %s | Muted: %s",
                self._attr_playback_state,
                self._attr_current_track.title or None,
                track_id,
                self._attr_elapsed_time,
                self._attr_total_time,
                self._attr_volume_level,
                self._attr_volume_muted,
            )

            self.update_state(True)

        except Exception as err:
            self.logger.warning("Failed to kodi poll loop: %s", err)
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
            self.update_state()
