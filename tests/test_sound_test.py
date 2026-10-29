"""Unit tests for offline app sound test features (Feature B)."""
import asyncio
import numpy as np
import pytest

from audio_devices import resolve_device
from config import AppConfig, VolumesConfig
from sound_test import SoundTestManager, SOUND_TEST_SPEAKER_PHRASE, STARTUP_PHRASE


class DummyTTS:
    def __init__(self):
        self.last_text = None

    async def synth(self, text: str):
        self.last_text = text
        sr = 16000
        samples = int(sr * 0.1)
        audio = (0.5 * np.sin(np.linspace(0, 2 * np.pi * 440, samples))).astype(np.float32)
        return audio, sr


class DummySTT:
    def __init__(self, transcript="testing microphone one two"):
        self.transcript = transcript

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcript


class DummyAudioPlayer:
    def __init__(self, volume=1.0):
        self.device = None
        self.volume = volume
        self.played = []

    def set_volume(self, vol: float):
        self.volume = vol

    def set_device(self, dev):
        self.device = dev
        return True, None

    def play_direct(self, audio: np.ndarray, sr: int = 16000):
        self.played.append((audio, sr))
        return True


class DummyCapture:
    def __init__(self, device=None):
        self.device = device
        self.sample_rate = 16000
        self.ptt = False

    def on_ptt(self, down: bool):
        self.ptt = down

    def reset_clipping(self):
        pass

    def get_live_level(self):
        return {"peak": 0.4, "rms": 0.15, "peak_db": -8.0, "clipping": False}

    def set_device(self, dev):
        self.device = dev
        return True, None


def test_sound_test_blocked_during_live_session():
    """Sound test is disabled while a session is live and displays proper message."""
    async def _run():
        cfg = AppConfig()
        tts = DummyTTS()
        player = DummyAudioPlayer()

        # Session is live
        mgr = SoundTestManager(
            tts=tts,
            player=player,
            config=cfg,
            is_session_live_fn=lambda: True,
        )

        # 1. Speaker test blocked
        spk_res = await mgr.play_speaker_test()
        assert spk_res["status"] == "blocked"
        assert spk_res["message"] == "Use radio check in game instead"

        # 2. Mic test blocked
        mic_res = mgr.start_mic_test()
        assert mic_res["status"] == "blocked"
        assert mic_res["message"] == "Use radio check in game instead"

        # 3. Startup sound blocked
        start_res = await mgr.play_startup_sound()
        assert start_res["status"] == "blocked"

    asyncio.run(_run())


def test_speaker_test_playback_and_radio_fx_toggle():
    """Speaker test plays local TTS test phrase with/without radio effect."""
    async def _run():
        cfg = AppConfig()
        tts = DummyTTS()
        player = DummyAudioPlayer()

        mgr = SoundTestManager(
            tts=tts,
            player=player,
            config=cfg,
            is_session_live_fn=lambda: False,
        )

        # 1. With radio FX
        res_fx = await mgr.play_speaker_test(with_radio_fx=True)
        assert res_fx["status"] == "ok"
        assert res_fx["with_radio_fx"] is True
        assert tts.last_text == SOUND_TEST_SPEAKER_PHRASE
        assert len(player.played) == 1

        # 2. Without radio FX
        res_no_fx = await mgr.play_speaker_test(with_radio_fx=False)
        assert res_no_fx["status"] == "ok"
        assert res_no_fx["with_radio_fx"] is False
        assert len(player.played) == 2

    asyncio.run(_run())


def test_mic_test_evaluation_and_clipping():
    """Mic test measures peak, RMS, clipping warning, and returns STT transcript."""
    async def _run():
        cfg = AppConfig()
        stt = DummySTT("mic test loud and clear")
        player = DummyAudioPlayer()
        capture = DummyCapture()

        mgr = SoundTestManager(
            stt=stt,
            player=player,
            capture=capture,
            config=cfg,
            is_session_live_fn=lambda: False,
        )

        # Normal speech level (peak 0.6)
        audio_normal = (0.6 * np.ones(16000, dtype=np.float32))
        eval_normal = await mgr.stop_mic_test_and_evaluate(manual_audio=audio_normal, play_back=False)

        assert eval_normal["status"] == "ok"
        assert eval_normal["audio_received"] is True
        assert eval_normal["clipping"] is False
        assert eval_normal["clipping_warning"] is None
        assert eval_normal["transcript"] == "mic test loud and clear"
        assert eval_normal["peak"] == pytest.approx(0.6, abs=0.01)

        # Clipped speech level (peak >= 0.98)
        audio_clipped = (0.99 * np.ones(16000, dtype=np.float32))
        eval_clipped = await mgr.stop_mic_test_and_evaluate(manual_audio=audio_clipped, play_back=False)

        assert eval_clipped["clipping"] is True
        assert eval_clipped["clipping_warning"] is not None
        assert "Warning: Microphone input clipped!" in eval_clipped["clipping_warning"]

    asyncio.run(_run())


def test_device_fallback_on_invalid_device():
    """Device resolver falls back to None (Windows default) when device ID is unavailable."""
    resolved, warn = resolve_device(999999, kind="input")
    assert resolved is None
    assert warn is not None
    assert "falling back to system default" in warn.lower()


def test_volume_settings_applied(tmp_path):
    """Volume settings apply separate levels for engineer voice, spotter, and menu beeps."""
    cfg = AppConfig()
    player = DummyAudioPlayer()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("volumes:\n  engineer: 1.0\n", encoding="utf-8")
    mgr = SoundTestManager(player=player, config=cfg, config_path=str(cfg_file))

    res = mgr.update_volumes(engineer=0.75, spotter=0.60, menu_beeps=0.40)
    assert res["status"] == "ok"
    assert cfg.volumes.engineer == 0.75
    assert cfg.volumes.spotter == 0.60
    assert cfg.volumes.menu_beeps == 0.40
    assert player.volume == 0.75

