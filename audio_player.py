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

    def __init__(self, device: str | int | None = None, sample_rate: int = 22050, start_stream: bool = True):
        self.device = device
        self.sample_rate = sample_rate
        self._stream = None
        self._lock = threading.Lock()
        self._interrupt_flag = False
        if start_stream:
            self.start(sample_rate)

    def interrupt(self) -> None:
        """Signals any active play() call to terminate immediately."""
        self._interrupt_flag = True

    def start(self, sample_rate: Optional[int] = None) -> None:
        """Opens and starts a persistent OutputStream."""
        if sample_rate:
            self.sample_rate = sample_rate
        try:
            import sounddevice as sd
            with self._lock:
                if self._stream is not None:
                    try:
                        self._stream.stop()
                        self._stream.close()
                    except Exception:
                        pass
                self._stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.device,
                )
                self._stream.start()
                log.info("Audio output opened (%d Hz mono float32, device=%s)", self.sample_rate, self.device)
        except Exception as e:
            log.warning("Could not open audio output stream: %s", e)

    def play(self, audio: np.ndarray, sample_rate: int, is_interrupted: Optional[Callable[[], bool]] = None) -> bool:
        """Plays audio in short blocks.

        Returns:
            True if playback finished completely.
            False if playback was interrupted by PTT, external interrupt, or error.
        """
        if audio is None or len(audio) == 0:
            return True

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        if self._stream is None or self.sample_rate != sample_rate:
            self.start(sample_rate)

        self._interrupt_flag = False
        interrupt_fn = is_interrupted or (lambda: False)

        # Fallback for headless environments or when audio driver is unavailable
        if self._stream is None:
            duration = len(audio) / sample_rate
            steps = max(1, int(duration / 0.04))
            for _ in range(steps):
                if interrupt_fn() or self._interrupt_flag:
                    log.info("Playback interrupted")
                    return False
                time.sleep(0.04)
            return True

        # Write in ~40 ms blocks so PTT or spotter interrupts within 40 ms
        block_size = max(1, int(sample_rate * 0.04))
        for i in range(0, len(audio), block_size):
            if interrupt_fn() or self._interrupt_flag:
                log.info("Playback interrupted")
                return False
            block = audio[i : i + block_size]
            try:
                self._stream.write(block)
            except Exception as e:
                log.debug("Stream write error: %s", e)
                return False

        return True

    def play_direct(self, audio: np.ndarray, sample_rate: int) -> bool:
        """Immediately interrupts any current playback and plays audio to completion without PTT gating."""
        self.interrupt()
        time.sleep(0.02)  # brief pause for active block to exit
        return self.play(audio, sample_rate, is_interrupted=None)

    def stop(self) -> None:
        """Stops and closes the OutputStream."""
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
