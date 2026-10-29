"""Unit tests for audio device listing using fakes/mocks."""
from unittest.mock import patch
import audio_devices


def test_list_audio_devices_mocked():
    """Verify list_audio_devices parses and formats device list correctly without physical audio hardware."""
    mock_devices = [
        {
            "name": "Mock Microphone",
            "hostapi": 0,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "name": "Mock Headset",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 44100.0,
        },
    ]
    mock_hostapis = [{"name": "Windows WASAPI"}]

    with patch("sounddevice.query_devices", return_value=mock_devices), \
         patch("sounddevice.query_hostapis", return_value=mock_hostapis), \
         patch("sounddevice.default.device", (0, 1)):
        output = audio_devices.list_audio_devices()

    assert "Windows WASAPI" in output
    assert "Mock Microphone" in output
    assert "Mock Headset" in output
    assert "DEFAULT" in output
    assert "INPUT DEVICES" in output
    assert "OUTPUT DEVICES" in output


def test_list_audio_devices_handles_import_or_driver_error():
    """Verify graceful error reporting when driver query fails."""
    with patch("sounddevice.query_devices", side_effect=RuntimeError("No host audio subsystem")):
        output = audio_devices.list_audio_devices()
        assert "Error querying audio devices: No host audio subsystem" in output
