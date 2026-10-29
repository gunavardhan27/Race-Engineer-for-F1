"""Digital signal processing for the F1 team radio effect.

Features:
- 300–3400 Hz band-pass filter (speech band typical of team radios)
- Soft saturation / compression using tanh
- Low-level background static noise
- Lead-in mic click burst and trailing squelch cut-off burst
- Peak normalization guaranteeing dtype=float32 and amplitude <= 1.0
"""
from __future__ import annotations

import numpy as np
import scipy.signal as signal


def generate_click(sample_rate: int, duration_s: float = 0.04, amplitude: float = 0.12) -> np.ndarray:
    """Generates a short mic-key click burst before speech."""
    n = max(1, int(sample_rate * duration_s))
    t = np.linspace(0, duration_s, n, endpoint=False)
    noise = np.random.normal(0, 0.7, n)
    tone = np.sin(2 * np.pi * 1000 * t)
    sig = (0.7 * noise + 0.3 * tone) * np.hanning(n) * amplitude
    return sig.astype(np.float32)


def generate_squelch(sample_rate: int, duration_s: float = 0.06, amplitude: float = 0.07) -> np.ndarray:
    """Generates a trailing radio squelch cut-off burst after speech."""
    n = max(1, int(sample_rate * duration_s))
    noise = np.random.normal(0, 1.0, n)
    # Fast attack, slight plateau, fast decay
    env = np.ones(n, dtype=np.float32)
    attack = max(1, int(n * 0.15))
    decay = max(1, int(n * 0.35))
    env[:attack] = np.linspace(0, 1, attack)
    env[-decay:] = np.linspace(1, 0, decay)
    sig = noise * env * amplitude
    return sig.astype(np.float32)


def apply_radio_fx(
    audio: np.ndarray,
    sample_rate: int,
    enable_clicks: bool = True,
    static_level: float = 0.012,
    compression_drive: float = 1.4,
) -> np.ndarray:
    """Applies team radio acoustics: bandpass filter, compression, static, and clicks."""
    if audio is None or len(audio) == 0:
        return np.array([], dtype=np.float32)

    # Ensure 1D float32
    if audio.ndim > 1:
        audio = audio.flatten()
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    # 1. Band-pass 300–3400 Hz
    nyquist = sample_rate / 2.0
    low = max(50.0, min(300.0, nyquist * 0.05))
    high = min(3400.0, nyquist * 0.95)

    sos = signal.butter(4, [low, high], btype="bandpass", fs=sample_rate, output="sos")
    filtered = signal.sosfilt(sos, audio).astype(np.float32)

    # 2. Light compression / soft clipping via tanh saturation
    if compression_drive > 1.0:
        compressed = (np.tanh(filtered * compression_drive) / np.tanh(compression_drive)).astype(np.float32)
    else:
        compressed = filtered

    # 3. Low-level static noise
    if static_level > 0.0:
        static = np.random.normal(0, static_level, len(compressed)).astype(np.float32)
        static_filtered = signal.sosfilt(sos, static).astype(np.float32)
        mixed = compressed + static_filtered
    else:
        mixed = compressed

    # 4. Leading click and trailing squelch bursts
    if enable_clicks:
        click = generate_click(sample_rate)
        squelch = generate_squelch(sample_rate)
        out = np.concatenate([click, mixed, squelch])
    else:
        out = mixed

    # 5. Peak normalization & hard ceiling at 1.0 (no clipping)
    peak = np.max(np.abs(out))
    if peak > 0.95:
        out = out * (0.95 / peak)

    out = np.clip(out, -1.0, 1.0).astype(np.float32)
    return out
