"""App Sound Test System (Offline, no game needed, never mentions telemetry).

Features:
1. Speaker test:
   Plays radio click + "Sound test. One, two, three. Radio's working." with local TTS
   through selected output device. Toggle with/without radio effect to compare.
2. Mic test:
   Record speech, play back recording through team radio effect, return transcript,
   peak level (dB), live level meter, and clipping warning.
3. Device management with instant live switching and fallback on unplugged hardware.
4. Startup sound:
   Plays click + "Engineer Online, Radio check radio check." when window/app opens (config sound_test.on_app_start).
5. Live session gating:
   Disabled while an F1 session is live ("Use radio check in game instead").
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np
import yaml

from audio_devices import get_audio_devices, resolve_device
from audio_player import AudioPlayer
from config import AppConfig, VolumesConfig
from radio_fx import apply_radio_fx

log = logging.getLogger("engineer.sound_test")

SOUND_TEST_SPEAKER_PHRASE = "Sound test. One, two, three. Radio's working."
STARTUP_PHRASE = "Engineer Online, Radio check radio check."


class SoundTestManager:
    def __init__(
        self,
        tts: Any | None = None,
        player: AudioPlayer | None = None,
        capture: Any | None = None,
        stt: Any | None = None,
        config: AppConfig | None = None,
        is_session_live_fn: Optional[Callable[[], bool]] = None,
        config_path: str = "config.yaml",
    ):
        self.tts = tts
        self.player = player
        self.capture = capture
        self.stt = stt
        self.config = config or AppConfig()
        self.is_session_live_fn = is_session_live_fn or (lambda: False)
        self.config_path = config_path

        self._lock = threading.Lock()
        self._mic_recording = False
        self._mic_chunks: list[np.ndarray] = []
        self._mic_start_time: float = 0.0

    def is_session_live(self) -> bool:
        """Returns True if telemetry packets from an active session are arriving."""
        try:
            return bool(self.is_session_live_fn())
        except Exception as e:
            log.debug("Error checking live session state: %s", e)
            return False

    async def play_speaker_test(
        self,
        with_radio_fx: bool = True,
        volume: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Plays speaker test phrase: click + 'Sound test. One, two, three. Radio's working.'"""
        if self.is_session_live():
            return {
                "status": "blocked",
                "message": "Use radio check in game instead",
                "session_live": True,
            }

        if not self.tts or not self.player:
            return {"status": "error", "message": "TTS engine or audio player unavailable"}

        try:
            audio, sr = await self.tts.synth(SOUND_TEST_SPEAKER_PHRASE)
            if with_radio_fx:
                audio = apply_radio_fx(audio, sr, enable_clicks=True)

            eff_volume = self.config.volumes.engineer if volume is None else float(volume)
            eff_volume = max(0.0, min(1.0, eff_volume))
            if eff_volume != 1.0:
                audio = audio * eff_volume

            completed = await asyncio_to_thread(self.player.play_direct, audio, sr)
            return {
                "status": "ok",
                "text": SOUND_TEST_SPEAKER_PHRASE,
                "with_radio_fx": with_radio_fx,
                "volume": eff_volume,
                "completed": completed,
            }
        except Exception as e:
            log.warning("Speaker test playback failed: %s", e)
            return {"status": "error", "message": str(e)}

    async def play_startup_sound(self) -> Dict[str, Any]:
        """Plays click + 'Engineer Online, Radio check radio check.' on initial app launch if enabled."""
        if not getattr(self.config.sound_test, "on_app_start", True):
            return {"status": "skipped", "reason": "sound_test.on_app_start disabled"}

        if self.is_session_live():
            return {
                "status": "blocked",
                "message": "Use radio check in game instead",
                "session_live": True,
            }

        if not self.tts or not self.player:
            return {"status": "error", "message": "Audio system unavailable"}

        try:
            audio, sr = await self.tts.synth(STARTUP_PHRASE)
            audio = apply_radio_fx(audio, sr, enable_clicks=True)
            eff_volume = self.config.volumes.engineer
            if eff_volume != 1.0:
                audio = audio * eff_volume

            await asyncio_to_thread(self.player.play_direct, audio, sr)
            log.info("Startup sound played: %r", STARTUP_PHRASE)
            return {"status": "ok", "phrase": STARTUP_PHRASE}
        except Exception as e:
            log.warning("Startup sound playback failed: %s", e)
            return {"status": "error", "message": str(e)}

    def start_mic_test(self) -> Dict[str, Any]:
        """Begins recording driver speech for mic test."""
        if self.is_session_live():
            return {
                "status": "blocked",
                "message": "Use radio check in game instead",
                "session_live": True,
            }

        with self._lock:
            self._mic_recording = True
            self._mic_chunks = []
            self._mic_start_time = time.monotonic()
            if self.capture:
                self.capture.reset_clipping()
                if hasattr(self.capture, "on_ptt"):
                    self.capture.on_ptt(True)

        log.info("Mic test recording started")
        return {"status": "recording"}

    def feed_mic_chunk(self, chunk: np.ndarray) -> None:
        """Appends chunk if mic test is recording."""
        with self._lock:
            if self._mic_recording:
                self._mic_chunks.append(chunk)

    async def stop_mic_test_and_evaluate(
        self,
        manual_audio: Optional[np.ndarray] = None,
        play_back: bool = True,
    ) -> Dict[str, Any]:
        """Stops mic test, calculates levels, checks clipping, transcribes, and plays back with radio effect."""
        if self.is_session_live():
            return {
                "status": "blocked",
                "message": "Use radio check in game instead",
                "session_live": True,
            }

        audio = manual_audio
        with self._lock:
            self._mic_recording = False
            if audio is None and self._mic_chunks:
                audio = np.concatenate(self._mic_chunks)
                self._mic_chunks = []
            if self.capture and hasattr(self.capture, "on_ptt"):
                self.capture.on_ptt(False)

        if audio is None or len(audio) == 0:
            return {
                "status": "ok",
                "audio_received": False,
                "peak": 0.0,
                "peak_db": -100.0,
                "rms": 0.0,
                "clipping": False,
                "transcript": "",
                "message": "No audio recorded",
            }

        audio = audio.astype(np.float32)
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio**2)))
        peak_db = 20.0 * np.log10(peak) if peak > 1e-5 else -100.0
        clipping = (peak >= 0.98)

        # Transcribe with local STT
        transcript = ""
        if self.stt:
            try:
                transcript = await asyncio_to_thread(self.stt.transcribe, audio)
                transcript = (transcript or "").strip()
            except Exception as e:
                log.warning("Mic test STT failed: %s", e)

        # Play back driver's own voice through team radio acoustics
        if play_back and self.player and len(audio) > 0:
            try:
                sr = getattr(self.capture, "sample_rate", 16000) if self.capture else 16000
                fx_audio = apply_radio_fx(audio, sr, enable_clicks=True)
                eff_volume = self.config.volumes.engineer
                if eff_volume != 1.0:
                    fx_audio = fx_audio * eff_volume
                await asyncio_to_thread(self.player.play_direct, fx_audio, sr)
            except Exception as e:
                log.warning("Mic test playback failed: %s", e)

        duration_s = round(len(audio) / 16000.0, 2)
        return {
            "status": "ok",
            "audio_received": True,
            "duration_s": duration_s,
            "peak": round(peak, 4),
            "peak_db": round(peak_db, 1),
            "rms": round(rms, 4),
            "clipping": clipping,
            "transcript": transcript,
            "clipping_warning": "Warning: Microphone input clipped! Reduce mic gain in Windows sound settings." if clipping else None,
        }

    def get_devices(self) -> Dict[str, Any]:
        """Returns input and output audio device lists with active selections."""
        data = get_audio_devices()
        data["current_input"] = self.capture.device if self.capture else self.config.audio.input_device
        data["current_output"] = self.player.device if self.player else self.config.audio.output_device
        return data

    def change_devices(
        self,
        input_device: str | int | None = None,
        output_device: str | int | None = None,
    ) -> Dict[str, Any]:
        """Switches audio devices immediately and falls back gracefully if disconnected."""
        warnings = []
        input_ok, output_ok = True, True

        if input_device is not None and self.capture:
            ok, warn = self.capture.set_device(input_device)
            if warn:
                warnings.append(warn)
            input_ok = ok
            self.config.audio.input_device = self.capture.device

        if output_device is not None and self.player:
            ok, warn = self.player.set_device(output_device)
            if warn:
                warnings.append(warn)
            output_ok = ok
            self.config.audio.output_device = self.player.device

        self._save_config_updates()
        return {
            "status": "ok",
            "input_ok": input_ok,
            "output_ok": output_ok,
            "active_input": self.capture.device if self.capture else self.config.audio.input_device,
            "active_output": self.player.device if self.player else self.config.audio.output_device,
            "warnings": warnings,
        }

    def update_volumes(
        self,
        engineer: Optional[float] = None,
        spotter: Optional[float] = None,
        menu_beeps: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Updates individual volume settings and writes to config.yaml."""
        if engineer is not None:
            self.config.volumes.engineer = max(0.0, min(1.0, float(engineer)))
            if self.player:
                self.player.set_volume(self.config.volumes.engineer)
        if spotter is not None:
            self.config.volumes.spotter = max(0.0, min(1.0, float(spotter)))
        if menu_beeps is not None:
            self.config.volumes.menu_beeps = max(0.0, min(1.0, float(menu_beeps)))

        self._save_config_updates()
        return {
            "status": "ok",
            "engineer": self.config.volumes.engineer,
            "spotter": self.config.volumes.spotter,
            "menu_beeps": self.config.volumes.menu_beeps,
        }

    def _save_config_updates(self) -> None:
        """Persists current device and volume settings to config.yaml."""
        p = Path(self.config_path)
        if not p.is_file():
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            # Update audio section
            audio_sec = data.setdefault("audio", {})
            audio_sec["input_device"] = self.config.audio.input_device
            audio_sec["output_device"] = self.config.audio.output_device

            # Update volumes section
            vol_sec = data.setdefault("volumes", {})
            vol_sec["engineer"] = self.config.volumes.engineer
            vol_sec["spotter"] = self.config.volumes.spotter
            vol_sec["menu_beeps"] = self.config.volumes.menu_beeps

            with open(p, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            log.debug("Saved audio and volume configuration to %s", self.config_path)
        except Exception as e:
            log.warning("Failed to save audio configuration to %s: %s", self.config_path, e)


async def asyncio_to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Asyncio runner helper for synchronous functions."""
    import asyncio
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except AttributeError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
