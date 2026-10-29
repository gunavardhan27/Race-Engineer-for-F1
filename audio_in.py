"""Push-to-talk audio capture with pre-roll buffering.

Maintains a persistent sounddevice.InputStream at 16 kHz mono float32.
Buffers ~300 ms of pre-roll to prevent clipping the first word.
Enforces a 250 ms minimum duration threshold and caps transmissions at 15 seconds.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("engineer.audio_in")


class AudioCapture:
    def __init__(
        self,
        sample_rate: int = 16000,
        device: str | int | None = None,
        pre_roll_ms: int = 300,
        min_duration_s: float = 0.25,
        max_duration_s: float = 15.0,
        on_audio_recorded: Optional[Callable[[np.ndarray], None]] = None,
        start_stream: bool = True,
    ):
        self.sample_rate = sample_rate
        self.device = device
        self.pre_roll_samples = int(sample_rate * (pre_roll_ms / 1000.0))
        self.min_samples = int(sample_rate * min_duration_s)
        self.max_samples = int(sample_rate * max_duration_s)
        self.on_audio_recorded = on_audio_recorded

        self._lock = threading.Lock()
        self._is_recording = False
        self._ring_buffer: collections.deque[np.ndarray] = collections.deque()
        self._ring_sample_count = 0
        self._recording_chunks: list[np.ndarray] = []
        self._recording_sample_count = 0

        self._stream = None
        if start_stream:
            self.start()

    def start(self) -> None:
        """Opens and starts the persistent sounddevice.InputStream."""
        try:
            import sounddevice as sd
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._audio_callback,
            )
            self._stream.start()
            log.info("Audio capture opened (16 kHz mono float32, device=%s)", self.device)
        except Exception as e:
            log.warning("Could not open audio input stream: %s", e)

    def stop(self) -> None:
        """Stops and closes the InputStream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.debug("Error stopping audio stream: %s", e)
            self._stream = None

    def _audio_callback(self, indata, frames, time_info, status):
        """sounddevice audio stream callback."""
        if status:
            log.debug("Audio input status: %s", status)
        chunk = indata[:, 0].copy()
        self.feed_audio(chunk)

    def feed_audio(self, chunk: np.ndarray) -> None:
        """Feeds an incoming audio chunk (1D float32 array) into the buffer."""
        with self._lock:
            # Maintain pre-roll ring buffer
            self._ring_buffer.append(chunk)
            self._ring_sample_count += len(chunk)
            while self._ring_buffer and (self._ring_sample_count - len(self._ring_buffer[0])) >= self.pre_roll_samples:
                popped = self._ring_buffer.popleft()
                self._ring_sample_count -= len(popped)

            # If actively recording, accumulate
            if self._is_recording:
                self._recording_chunks.append(chunk)
                self._recording_sample_count += len(chunk)

                # Cap transmission at max duration (15s)
                if self._recording_sample_count >= self.max_samples:
                    log.info("Transmission reached %d s cap; auto-stopping", int(self.max_samples / self.sample_rate))
                    self._finalize_recording_locked()

    def _get_pre_roll_locked(self) -> np.ndarray:
        """Extracts the last pre_roll_samples from the ring buffer."""
        if not self._ring_buffer:
            return np.array([], dtype=np.float32)
        combined = np.concatenate(self._ring_buffer)
        if len(combined) > self.pre_roll_samples:
            return combined[-self.pre_roll_samples:]
        return combined

    def on_ptt(self, down: bool) -> None:
        """Handles PTT state transitions (True = pressed, False = released)."""
        with self._lock:
            if down:
                if not self._is_recording:
                    self._is_recording = True
                    pre_roll = self._get_pre_roll_locked()
                    self._recording_chunks = [pre_roll] if len(pre_roll) > 0 else []
                    self._recording_sample_count = len(pre_roll)
                    log.debug("PTT down: recording started with %d pre-roll samples", len(pre_roll))
            else:
                if self._is_recording:
                    self._finalize_recording_locked()

    def _finalize_recording_locked(self) -> None:
        """Finalizes and dispatches recorded audio if it meets duration requirements."""
        self._is_recording = False
        if not self._recording_chunks:
            return

        audio = np.concatenate(self._recording_chunks)
        self._recording_chunks = []
        self._recording_sample_count = 0

        # Cap if over limit
        if len(audio) > self.max_samples:
            audio = audio[: self.max_samples]

        duration_s = len(audio) / self.sample_rate
        if len(audio) < self.min_samples:
            log.info("PTT press too short (%.2fs < %.2fs); ignored", duration_s, self.min_samples / self.sample_rate)
            return

        log.info("Captured transmission: %.2fs (%d samples)", duration_s, len(audio))
        if self.on_audio_recorded:
            # Dispatch outside lock or in thread
            try:
                self.on_audio_recorded(audio)
            except Exception:
                log.exception("Error in on_audio_recorded callback")


def start_joystick_listener(
    on_ptt: Callable[[bool], None],
    joystick_index: int = 0,
    button_index: int = 0,
) -> Optional[threading.Thread]:
    """Optional background listener for PTT via pygame joystick."""
    import os

    # Enable joystick events when game has window focus
    os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"

    try:
        import pygame
    except ImportError:
        log.warning("pygame is not installed; joystick PTT fallback unavailable")
        return None

    def _poll_joystick():
        pygame.init()
        pygame.joystick.init()
        count = pygame.joystick.get_count()
        if count <= joystick_index:
            log.warning("Joystick index %d not found (found %d)", joystick_index, count)
            return

        joy = pygame.joystick.Joystick(joystick_index)
        joy.init()
        log.info("Joystick PTT active: '%s', button %d", joy.get_name(), button_index)

        last_state = False
        try:
            while True:
                pygame.event.pump()
                current_state = bool(joy.get_button(button_index))
                if current_state != last_state:
                    last_state = current_state
                    on_ptt(current_state)
                time.sleep(0.01)
        except Exception:
            log.exception("Joystick polling loop terminated")
        finally:
            pygame.quit()

    t = threading.Thread(target=_poll_joystick, daemon=True, name="JoystickPTT")
    t.start()
    return t
