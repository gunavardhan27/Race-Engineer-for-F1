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


def get_audio_devices() -> dict[str, Any]:
    """Returns structured metadata of input and output audio devices."""
    result = {"input": [], "output": [], "default_input": None, "default_output": None}
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        default_in, default_out = sd.default.device
        result["default_input"] = default_in
        result["default_output"] = default_out

        for idx, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                result["input"].append({
                    "id": idx,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "sample_rate": int(d.get("default_samplerate", 16000)),
                    "is_default": (idx == default_in),
                })
            if d.get("max_output_channels", 0) > 0:
                result["output"].append({
                    "id": idx,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                    "sample_rate": int(d.get("default_samplerate", 22050)),
                    "is_default": (idx == default_out),
                })
    except Exception as e:
        result["error"] = str(e)
    return result


def resolve_device(device: str | int | None, kind: str = "output") -> tuple[int | None, str | None]:
    """Validates device availability. If unavailable or unplugged, falls back to None with a warning."""
    if device is None:
        return None, None

    try:
        import sounddevice as sd
        devices = sd.query_devices()
        num_devices = len(devices)

        # Check by numeric index
        if isinstance(device, int) or (isinstance(device, str) and device.isdigit()):
            idx = int(device)
            if 0 <= idx < num_devices:
                ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
                if devices[idx].get(ch_key, 0) > 0:
                    return idx, None
                return None, f"Device ID {idx} does not support {kind} channels. Falling back to system default."
            return None, f"Audio {kind} device index {idx} not found. Falling back to system default."

        # Check by device name substring
        target_name = str(device).strip().lower()
        ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
        for idx, d in enumerate(devices):
            if d.get(ch_key, 0) > 0 and target_name in d["name"].lower():
                return idx, None

        return None, f"Audio {kind} device {device!r} was unplugged or not found. Falling back to system default."
    except Exception as e:
        return None, f"Audio driver query failed ({e}). Falling back to system default."


def print_audio_devices() -> None:
    """Prints audio devices to stdout."""
    print(list_audio_devices(), flush=True)


if __name__ == "__main__":
    print_audio_devices()
