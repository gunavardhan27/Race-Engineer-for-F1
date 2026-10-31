"""Audio output playback using sounddevice.OutputStream with instant PTT interruption."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("engineer.player")


class AudioPlayer:
    """Manages playback through sounddevice.OutputStream and allows immediate PTT interruption."""

    def __init__(
        self,
        device: str | int | None = None,
        sample_rate: int = 24000,
        start_stream: bool = True,
        volume: float = 1.0,
    ):
        self.device = device
        self.sample_rate = sample_rate
        self.volume = max(0.0, min(1.0, float(volume)))
        self._stream = None
        self._stream_lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._interrupt_flag = False
        if start_stream:
            self.start(sample_rate)

    def set_volume(self, volume: float) -> None:
        """Sets output playback volume (0.0 to 1.0)."""
        self.volume = max(0.0, min(1.0, float(volume)))
        log.info("AudioPlayer volume set to %.2f", self.volume)

    def set_device(self, device: str | int | None) -> tuple[bool, Optional[str]]:
        """Changes playback output device immediately. Falls back to default if unavailable."""
        from audio_devices import resolve_device
        resolved, warn = resolve_device(device, kind="output")
        self.device = resolved
        self.start(self.sample_rate)
        return (resolved is not None or device is None), warn

    def interrupt(self) -> None:
        """Signals any active play() call to terminate immediately."""
        self._interrupt_flag = True

    def start(self, sample_rate: Optional[int] = None) -> None:
        """Opens and starts a persistent OutputStream."""
        if sample_rate:
            self.sample_rate = sample_rate
        try:
            import sounddevice as sd
            with self._stream_lock:
                if self._stream is not None:
                    try:
                        self._stream.stop()
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                self._stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.device,
                )
                self._stream.start()
                log.info("Audio output opened (%d Hz mono float32, device=%s)", self.sample_rate, self.device)
        except Exception as e:
            log.warning("Could not open audio output stream on device %s: %s; trying default", self.device, e)
            if self.device is not None:
                self.device = None
                self.start(sample_rate)

    def play(self, audio: np.ndarray, sample_rate: int, is_interrupted: Optional[Callable[[], bool]] = None) -> bool:
        """Plays audio in short blocks.

        Returns:
            True if playback finished completely.
            False if playback was interrupted by PTT, external interrupt, or error.
        """
        # Acquire playback lock to ensure only one thread writes to audio hardware at a time
        if not self._play_lock.acquire(timeout=1.0):
            log.warning("Audio playback lock acquisition timed out")
            return False
        try:
            return self._play_internal(audio, sample_rate, is_interrupted=is_interrupted)
        finally:
            self._play_lock.release()

    def play_direct(self, audio: np.ndarray, sample_rate: int) -> bool:
        """Immediately interrupts any current playback and plays audio to completion without PTT gating."""
        self.interrupt()
        # Wait up to 300ms for current block to exit on interrupt
        if not self._play_lock.acquire(timeout=0.3):
            log.warning("play_direct lock acquisition timed out")
            return False
        try:
            self._interrupt_flag = False
            return self._play_internal(audio, sample_rate, is_interrupted=None)
        finally:
            self._play_lock.release()

    def _play_internal(self, audio: np.ndarray, sample_rate: int, is_interrupted: Optional[Callable[[], bool]] = None) -> bool:
        if audio is None or len(audio) == 0:
            return True

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        if self.volume != 1.0:
            audio = audio * self.volume

        with self._stream_lock:
            stream = self._stream

        if stream is None:
            self.start(sample_rate)
            with self._stream_lock:
                stream = self._stream

        # Resample in memory if sample rate differs, avoiding closing/reopening the PortAudio hardware stream
        if self.sample_rate != sample_rate and len(audio) > 0:
            num_samples = int(len(audio) * self.sample_rate / sample_rate)
            if num_samples > 0:
                audio = np.interp(
                    np.linspace(0, len(audio), num_samples, endpoint=False),
                    np.arange(len(audio)),
                    audio,
                ).astype(np.float32)

        self._interrupt_flag = False
        interrupt_fn = is_interrupted or (lambda: False)

        # Fallback for headless environments or when audio driver is unavailable
        if stream is None:
            duration = len(audio) / self.sample_rate
            steps = max(1, int(duration / 0.04))
            for _ in range(steps):
                if interrupt_fn() or self._interrupt_flag:
                    log.info("Playback interrupted")
                    return False
                time.sleep(0.04)
            return True

        # Write in ~40 ms blocks so PTT or spotter interrupts within 40 ms
        block_size = max(1, int(self.sample_rate * 0.04))
        for i in range(0, len(audio), block_size):
            if interrupt_fn() or self._interrupt_flag:
                log.info("Playback interrupted")
                return False
            block = audio[i : i + block_size]
            try:
                stream.write(block)
            except Exception as e:
                log.debug("Stream write error: %s", e)
                return False

        return True

    def stop(self) -> None:
        """Stops and closes the OutputStream."""
        self.interrupt()
        acquired = self._play_lock.acquire(timeout=0.2)
        try:
            with self._stream_lock:
                if self._stream is not None:
                    try:
                        self._stream.stop()
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
        finally:
            if acquired:
                self._play_lock.release()
