import array
import asyncio
import ipaddress
import logging
import os
import signal
import sys
import time
import urllib.parse

import pyatv
from pyatv.const import Protocol
from pyatv.conf import AppleTV, ManualService
from pyatv.protocols.raop.audio_source import AudioSource
import pyatv.protocols.raop as raop
import pyatv.protocols.raop.audio_source as audio_source
from pyatv.interface import MediaMetadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
_LOGGER = logging.getLogger("bridge")

IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "30.0"))
MAX_BUFFER_BYTES = int(os.environ.get("MAX_BUFFER_BYTES", "264600"))  # ~1.5s of 44.1kHz 16-bit stereo
PREBUFFER_BYTES = int(os.environ.get("PREBUFFER_BYTES", "70560"))    # ~400ms

# Full AirPlay 2 RAOP TXT properties required by pyatv so that:
# 1. get_protocol_version() selects AirPlayMajorVersion.AirPlayV2 (via ft bits 38 & 48)
# 2. extract_credentials() selects TRANSIENT_CREDENTIALS (HAP Pair-Verify + ChaCha20)
RAOP_AIRPLAY2_PROPS = {
    "cn": "0,1,2,3",
    "da": "true",
    "et": "0,3,5",
    "ft": "0x4A7FCA00,0x3C354BD0",
    "sf": "0xb8404",
    "md": "0,1,2",
    "am": "AudioAccessory5,1",
    "tp": "UDP",
    "vn": "65537",
    "vs": "980.77.2",
    "ov": "27.0",
    "vv": "1",
}

# Monkeypatch open_source to accept custom AudioSource
orig_open = raop.open_source
async def my_open(source, sample_rate, channels, sample_size):
    if isinstance(source, AudioSource):
        return source
    return await orig_open(source, sample_rate, channels, sample_size)
raop.open_source = my_open
audio_source.open_source = my_open


def _fast_swap_s16le_to_be(chunk: bytes) -> bytes:
    if len(chunk) % 2 != 0:
        chunk = chunk[:-1]
    arr = array.array("h", chunk)
    arr.byteswap()
    return arr.tobytes()


class RingBufferAudioSource(AudioSource):
    """
    On-demand Non-blocking Jitter Buffer for Squeezelite -> AirPlay 2.
    - Consumes from bridge.audio_buffer at 1.0x real-time speed.
    - When idle (no audio for > IDLE_TIMEOUT seconds), signals EOF (NO_FRAMES)
      to tear down AirPlay 2 session cleanly so HomePods enter sleep.
    - Prebuffers ~400ms before starting playback for glitch-free streaming.
    """
    def __init__(self, bridge, max_buffer_bytes=MAX_BUFFER_BYTES, prebuffer_bytes=PREBUFFER_BYTES):
        self.bridge = bridge
        self.max_buffer = max_buffer_bytes
        self.prebuffer = prebuffer_bytes
        self.buffer = bridge.audio_buffer
        self.running = True
        self.buffering = len(self.buffer) < self.prebuffer
        if not self.buffering:
            _LOGGER.info("Pre-buffer already filled (%d bytes) for %s, streaming audio",
                         len(self.buffer), self.bridge.player_name)

    async def readframes(self, nframes: int) -> bytes:
        if not self.running or self.bridge.is_idle:
            return AudioSource.NO_FRAMES

        needed = nframes * 4  # 16-bit stereo = 4 bytes per frame (352 * 4 = 1408)

        if self.buffering:
            if len(self.buffer) >= self.prebuffer:
                self.buffering = False
                _LOGGER.info("Pre-buffer filled (%d bytes) for %s, streaming audio",
                             len(self.buffer), self.bridge.player_name)
            else:
                return b"\x00" * needed

        if len(self.buffer) < needed:
            return b"\x00" * needed

        chunk = bytes(self.buffer[:needed])
        del self.buffer[:needed]
        return chunk

    async def close(self):
        self.running = False

    async def get_metadata(self) -> MediaMetadata:
        return MediaMetadata()

    @property
    def sample_rate(self) -> int:
        return 44100

    @property
    def channels(self) -> int:
        return 2

    @property
    def sample_size(self) -> int:
        return 2

    @property
    def duration(self) -> int:
        return 86400 * 365


class SqueezeliteAirplayBridge:
    def __init__(self):
        self.lms_host = os.environ.get("LMS_HOST", "127.0.0.1")
        self.lms_port = os.environ.get("LMS_PORT", "3483")
        self.lms_cli_port = int(os.environ.get("LMS_CLI_PORT", "9090"))
        self.player_name = os.environ.get("PLAYER_NAME", "HomePod")
        self.player_mac = os.environ.get("PLAYER_MAC", "aa:aa:00:00:00:01").lower()
        self.airplay_id = os.environ.get("AIRPLAY_ID")
        self.airplay_ip = os.environ.get("AIRPLAY_IP")

        self.proc = None
        self.atv = None
        self.running = True
        self.is_streaming = False
        self.current_volume = 25.0
        self.audio_buffer = bytearray()
        self.is_idle = True
        self.last_audio_time = 0.0
        self.wake_event = asyncio.Event()

    def trigger_wake(self):
        self.last_audio_time = time.monotonic()
        if self.is_idle:
            self.is_idle = False
            self.wake_event.set()
            _LOGGER.info("Audio activity detected for %s, waking AirPlay 2 session...", self.player_name)

    async def start_squeezelite(self):
        _LOGGER.info("Starting squeezelite for %s (MAC: %s, LMS: %s:%s)...",
                     self.player_name, self.player_mac, self.lms_host, self.lms_port)
        self.proc = await asyncio.create_subprocess_exec(
            "squeezelite",
            "-s", f"{self.lms_host}:{self.lms_port}",
            "-m", self.player_mac,
            "-n", self.player_name,
            "-o", "-",
            "-a", "16",
            "-r", "44100",
            "-b", "2048:4096",
            "-M", "SqueezeLite",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )

    async def drain_squeezelite_stdout(self):
        zeros_4096 = b"\x00" * 4096
        while self.running:
            try:
                # Apply backpressure whenever we are awake (including while AirPlay 2
                # is completing its handshake) so squeezelite blocks on stdout instead
                # of draining the track at unthrottled CPU speed.
                while self.running and not self.is_idle and len(self.audio_buffer) >= MAX_BUFFER_BYTES:
                    if not self.is_streaming and (time.monotonic() - self.last_audio_time > IDLE_TIMEOUT):
                        _LOGGER.info("AirPlay not consuming for %.0fs on %s, resetting to idle",
                                     IDLE_TIMEOUT, self.player_name)
                        self.is_idle = True
                        self.wake_event.clear()
                        self.audio_buffer.clear()
                        break
                    await asyncio.sleep(0.01)

                try:
                    chunk = await asyncio.wait_for(self.proc.stdout.read(4096), timeout=1.0)
                except asyncio.TimeoutError:
                    # Custom squeezelite output_stdout.c writes 0 bytes when silence=true
                    # (paused/stopped/off). Check idle timeout every 1s!
                    if not self.is_idle and (time.monotonic() - self.last_audio_time > IDLE_TIMEOUT):
                        _LOGGER.info("No audio for %.0fs on %s, closing AirPlay 2 session (idle sleep)",
                                     IDLE_TIMEOUT, self.player_name)
                        self.is_idle = True
                        self.wake_event.clear()
                        self.audio_buffer.clear()
                    continue

                if not chunk:
                    await asyncio.sleep(0.05)
                    continue

                is_silent = (chunk == zeros_4096) if len(chunk) == 4096 else (chunk == b"\x00" * len(chunk))

                if not is_silent:
                    self.trigger_wake()
                    swapped = _fast_swap_s16le_to_be(chunk)
                    self.audio_buffer.extend(swapped)
                else:
                    if not self.is_idle:
                        if time.monotonic() - self.last_audio_time > IDLE_TIMEOUT:
                            _LOGGER.info("Digital silence for %.0fs on %s, closing AirPlay 2 session (idle sleep)",
                                         IDLE_TIMEOUT, self.player_name)
                            self.is_idle = True
                            self.wake_event.clear()
                            self.audio_buffer.clear()
                        else:
                            self.audio_buffer.extend(chunk)
                    else:
                        await asyncio.sleep(0.02)
            except Exception as e:
                _LOGGER.debug("Stdout reader exception: %s", e)
                await asyncio.sleep(0.05)

    async def sync_volume_lms(self):
        mac_encoded = urllib.parse.quote(self.player_mac).lower()
        while self.running:
            try:
                reader, writer = await asyncio.open_connection(self.lms_host, self.lms_cli_port)

                # Lock digital volume to 100% in LMS (dvc=0) so squeezelite outputs
                # bit-perfect 16-bit PCM and the HomePod handles hardware attenuation.
                # writer.write(f"{self.player_mac} playerpref digitalVolumeControl 0\n".encode())
                writer.write(f"{self.player_mac} power 1\n".encode())
                writer.write(f"{self.player_mac} mixer volume ?\n".encode())
                writer.write(b"listen 1\n")
                await writer.drain()
                _LOGGER.info("Connected to LMS CLI at %s:%s",
                             self.lms_host, self.lms_cli_port)

                while self.running:
                    line = await reader.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").strip()
                    parts = decoded.split()
                    if not parts or parts[0].lower() != mac_encoded:
                        continue

                    if len(parts) >= 4 and parts[1] == "mixer" and parts[2] == "volume":
                        vol_raw = urllib.parse.unquote(parts[3])
                        try:
                            if vol_raw.startswith("+") or vol_raw.startswith("-"):
                                new_vol = self.current_volume + float(vol_raw)
                            else:
                                new_vol = float(vol_raw)
                            self.current_volume = max(0.0, min(100.0, new_vol))
                            if self.is_streaming and self.atv and self.atv.audio:
                                _LOGGER.info("Applying volume to HomePod %s: %.1f%%", self.player_name, self.current_volume)
                                await self.atv.audio.set_volume(self.current_volume)
                        except Exception as e:
                            _LOGGER.warning("Error parsing volume %s: %s", vol_raw, e)

                writer.close()
                await writer.wait_closed()
            except Exception as e:
                _LOGGER.warning("LMS CLI error: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)

    async def _build_config(self, loop, force_scan=False):
        if self.airplay_ip and self.airplay_id and not force_scan:
            conf = AppleTV(ipaddress.IPv4Address(self.airplay_ip), self.player_name)
            conf.add_service(
                ManualService(self.airplay_id, Protocol.RAOP, 7000, dict(RAOP_AIRPLAY2_PROPS))
            )
            return conf

        _LOGGER.info("Scanning mDNS for AirPlay target %s (ID: %s)...", self.player_name, self.airplay_id)
        results = await pyatv.scan(loop, identifier=self.airplay_id) if self.airplay_id else []
        if not results:
            results = [c for c in await pyatv.scan(loop) if c.name == self.player_name]
        if not results:
            return None
        conf = results[0]
        raop_service = conf.get_service(Protocol.RAOP)
        if not raop_service:
            return None
        conf._services = {Protocol.RAOP: raop_service}
        self.airplay_ip = str(conf.address)
        return conf

    async def run_airplay(self):
        loop = asyncio.get_event_loop()
        force_scan = False
        while self.running:
            await self.wake_event.wait()
            if not self.running:
                break

            source = None
            try:
                conf = await self._build_config(loop, force_scan=force_scan)
                if not conf:
                    _LOGGER.warning("AirPlay target %s not found, retrying in 3s...", self.player_name)
                    await asyncio.sleep(3)
                    continue

                _LOGGER.info("Connecting to AirPlay 2 target %s (IP: %s, ID: %s)...",
                             self.player_name, conf.address, self.airplay_id)
                self.atv = await pyatv.connect(conf, loop)

                try:
                    await self.atv.audio.set_volume(self.current_volume)
                    _LOGGER.info("Initial volume set to %.1f%% on %s", self.current_volume, self.player_name)
                except Exception as e:
                    _LOGGER.warning("Could not set initial volume on %s: %s", self.player_name, e)

                source = RingBufferAudioSource(self)
                self.is_streaming = True
                force_scan = False
                _LOGGER.info("AirPlay 2 streaming active for %s", self.player_name)

                async def _enforce_homepod_volume():
                    for delay in (1.5, 4.0):
                        await asyncio.sleep(delay)
                        if self.is_streaming and self.atv and self.atv.audio:
                            try:
                                await self.atv.audio.set_volume(self.current_volume)
                                _LOGGER.info("Post-start volume confirmed at %.1f%% on %s", self.current_volume, self.player_name)
                            except Exception as ve:
                                _LOGGER.debug("Post-start volume error on %s: %s", self.player_name, ve)

                vol_task = asyncio.create_task(_enforce_homepod_volume())
                try:
                    await self.atv.stream.stream_file(source)
                finally:
                    vol_task.cancel()
                _LOGGER.info("AirPlay 2 stream closed for %s (idle=%s)", self.player_name, self.is_idle)
            except Exception as e:
                _LOGGER.error("AirPlay 2 streaming exception for %s: %s. Reconnecting in 3s...", self.player_name, e)
                force_scan = True
                await asyncio.sleep(3)
            finally:
                self.is_streaming = False
                if source:
                    await source.close()
                if self.atv:
                    self.atv.close()
                    self.atv = None

    async def run(self):
        await self.start_squeezelite()
        try:
            await asyncio.gather(
                self.drain_squeezelite_stdout(),
                self.run_airplay(),
                self.sync_volume_lms()
            )
        finally:
            self.running = False
            self.is_streaming = False
            if self.proc:
                try:
                    self.proc.terminate()
                    await self.proc.wait()
                except Exception:
                    pass
            if self.atv:
                self.atv.close()


def handle_shutdown(bridge, loop):
    _LOGGER.info("Shutting down bridge...")
    bridge.running = False
    if bridge.proc:
        try:
            bridge.proc.kill()
        except Exception:
            pass
    bridge.wake_event.set()
    for task in asyncio.all_tasks(loop):
        task.cancel()


async def main():
    bridge = SqueezeliteAirplayBridge()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: handle_shutdown(bridge, loop))
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
