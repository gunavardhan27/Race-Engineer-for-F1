"""Unit tests for radio sound effect: band-pass filtering, compression, static, and peak limiter."""
import numpy as np
import pytest

from radio_fx import apply_radio_fx, generate_click, generate_squelch


def test_fx_output_shape_and_dtype():
    """Verify that apply_radio_fx returns float32 array with added click/squelch bursts."""
    sample_rate = 22050
    input_audio = np.sin(2 * np.pi * 440 * np.linspace(0, 1.0, sample_rate, endpoint=False)).astype(np.float32)

    output = apply_radio_fx(input_audio, sample_rate, enable_clicks=True)

    assert isinstance(output, np.ndarray)
    assert output.dtype == np.float32
    assert output.ndim == 1
    # Output must be longer than input due to click + squelch bursts
    assert len(output) > len(input_audio)


def test_fx_no_clipping_above_1():
    """Output values must never exceed 1.0 or fall below -1.0 even with extreme input peaks."""
    sample_rate = 22050
    # Overdriven hot signal with extreme amplitudes
    extreme_audio = np.random.uniform(-50.0, 50.0, sample_rate).astype(np.float32)

    output = apply_radio_fx(extreme_audio, sample_rate, enable_clicks=True, compression_drive=2.0)

    assert np.max(output) <= 1.0
    assert np.min(output) >= -1.0
    assert np.max(np.abs(output)) <= 1.0


def test_fx_without_clicks():
    """When enable_clicks=False, output length must equal input length."""
    sample_rate = 16000
    input_audio = np.zeros(8000, dtype=np.float32)

    output = apply_radio_fx(input_audio, sample_rate, enable_clicks=False)
    assert len(output) == len(input_audio)
    assert output.dtype == np.float32


def test_click_and_squelch_generators():
    """Click and squelch generators must produce float32 signals within [-1.0, 1.0]."""
    for sr in (16000, 22050, 48000):
        click = generate_click(sr)
        squelch = generate_squelch(sr)

        assert click.dtype == np.float32
        assert squelch.dtype == np.float32
        assert np.max(np.abs(click)) <= 1.0
        assert np.max(np.abs(squelch)) <= 1.0
        assert len(click) > 0
        assert len(squelch) > 0
