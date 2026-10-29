"""Audio device discovery utility."""
from __future__ import annotations

import sys


def list_audio_devices() -> str:
    """Returns a formatted string listing available audio input and output devices."""
    try:
        import sounddevice as sd
    except ImportError:
        return "sounddevice module not installed. Install via: pip install sounddevice"
    except Exception as e:
        return f"Error loading audio driver: {e}"

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception as e:
        return f"Error querying audio devices: {e}"

    lines = [
        "============================= Audio Devices =============================",
    ]

    # Host APIs summary
    api_names = [api["name"] for api in hostapis]
    lines.append(f"Host APIs: {', '.join(api_names)}")
    lines.append("")

    # Input devices
    lines.append("INPUT DEVICES (Microphones):")
    lines.append("  ID | Name | Host API | In Ch | Default SR")
    lines.append("  ---+------------------------------------+------------------+-------+-----------")
    input_found = False
    for idx, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            input_found = True
            is_def = " (DEFAULT)" if idx == default_in else ""
            api_name = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else str(d["hostapi"])
            name = d["name"][:34]
            lines.append(f"  {idx:>2} | {name:<34} | {api_name:<16} | {d['max_input_channels']:>5} | {int(d['default_samplerate']):>6} Hz{is_def}")
    if not input_found:
        lines.append("  (No input devices found)")

    lines.append("")

    # Output devices
    lines.append("OUTPUT DEVICES (Headsets / Speakers):")
    lines.append("  ID | Name | Host API | Out Ch| Default SR")
    lines.append("  ---+------------------------------------+------------------+-------+-----------")
    output_found = False
    for idx, d in enumerate(devices):
        if d["max_output_channels"] > 0:
            output_found = True
            is_def = " (DEFAULT)" if idx == default_out else ""
            api_name = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else str(d["hostapi"])
            name = d["name"][:34]
            lines.append(f"  {idx:>2} | {name:<34} | {api_name:<16} | {d['max_output_channels']:>5} | {int(d['default_samplerate']):>6} Hz{is_def}")
    if not output_found:
        lines.append("  (No output devices found)")

    lines.append("=========================================================================")
    return "\n".join(lines)


def print_audio_devices() -> None:
    """Prints audio devices to stdout."""
    print(list_audio_devices(), flush=True)


if __name__ == "__main__":
    print_audio_devices()
