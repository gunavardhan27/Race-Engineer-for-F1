"""Interactive Microphone & Voice Pipeline Tester.

Allows testing microphone input, speech-to-text (Whisper), and voice reply (TTS)
directly from the terminal without needing the F1 game running.
"""
import asyncio
import os
import sys
import time
import numpy as np
import sounddevice as sd

from config import load_config
from stt import SpeechToText, build_initial_prompt
from tts import create_tts_engine
from audio_player import AudioPlayer
from radio_fx import apply_radio_fx
from f1telemetry.state import RaceState
from f1telemetry.deterministic import route
from llm import LLMClient


async def main():
    cfg = load_config("config.yaml")
    print("=" * 65)
    print(" F1 Race Engineer - Microphone & Voice Pipeline Test")
    print("=" * 65)
    print(f"Input Device : {cfg.audio.input_device or 'Default Microphone'}")
    print(f"Output Device: {cfg.audio.output_device or 'Default Speakers/Headset'}")
    print(f"STT Model    : {cfg.stt.model} ({cfg.stt.device})")
    print("=" * 65)

    print("\n1. Initializing Speech-to-Text engine...")
    stt = SpeechToText(config=cfg.stt, warmup=True)

    print("2. Initializing Text-to-Speech engine...")
    tts = create_tts_engine(cfg.tts, cache_config=getattr(cfg, "cache", None))
    player = AudioPlayer(device=cfg.audio.output_device, volume=cfg.volumes.engineer)

    sr = 16000  # 16 kHz mono float32 for Whisper
    record_seconds = 4.0

    print("\n" + "-" * 65)
    print("Ready! You have two test modes:")
    print("  [1] Echo Test  : Speak -> STT Transcribes -> Hear yourself with Radio FX")
    print("  [2] Ask Engineer: Speak a command -> Transcribes -> Engineer replies")
    print("-" * 65)

    mode = input("\nChoose mode [1 or 2, default=2]: ").strip() or "2"

    while True:
        input(f"\nPress [ENTER] to start recording ({record_seconds:.0f} seconds)... ")
        print("🔴 RECORDING... Speak now into your microphone!")
        
        try:
            recording = sd.rec(
                int(record_seconds * sr),
                samplerate=sr,
                channels=1,
                dtype="float32",
                device=cfg.audio.input_device,
            )
            sd.wait()
            audio = recording.flatten()
        except Exception as e:
            print(f"❌ Recording error: {e}")
            break

        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio**2)))
        peak_db = 20.0 * np.log10(peak) if peak > 1e-5 else -100.0
        print(f"🎙️  Audio captured (Peak: {peak:.2f} / {peak_db:.1f} dB, RMS: {rms:.3f})")

        if peak < 0.02:
            print("⚠️  Very low volume detected. Check your system mic volume or permissions.")

        print("⏳ Transcribing with Whisper...")
        t0 = time.monotonic()
        transcript = stt.transcribe(audio, initial_prompt=build_initial_prompt())
        t_stt = (time.monotonic() - t0) * 1000.0
        print(f"📝 You said: {transcript!r} ({t_stt:.0f} ms)")

        if not transcript or not transcript.strip():
            print("⚠️  No speech detected. Try speaking closer to the microphone.")
            continue

        if mode == "1":
            print("🔊 Playing back through Team Radio FX...")
            fx_audio = apply_radio_fx(audio, sr, enable_clicks=True)
            player.play_direct(fx_audio, sr)
        else:
            state = RaceState()
            # Set dummy state values so lookups can respond
            state.last_packet_time = time.monotonic()
            
            print("🏎️  Routing query...")
            rep = route(transcript, state)
            if rep.kind == "answer":
                reply_text = rep.text or "Loud and clear. Telemetry's live."
            else:
                print("🧠 Query routed to AI (Groq)...")
                llm = LLMClient(cfg.llm)
                system_prompt = "You are the driver's race engineer on the pit wall during an F1 race. Reply in at most 2 short sentences, direct radio style."
                chunks = []
                async for tok in llm.stream(system_prompt, transcript):
                    chunks.append(tok)
                reply_text = "".join(chunks).strip() or "Copy that, standing by."
            print(f"📻 Engineer says: {reply_text}")

            print("🔊 Generating speech...")
            synth_audio, synth_sr = await tts.synth(reply_text)
            if synth_audio is not None and len(synth_audio) > 0:
                if cfg.tts.radio_fx:
                    synth_audio = apply_radio_fx(synth_audio, synth_sr, enable_clicks=True)
                player.play_direct(synth_audio, synth_sr)

        again = input("\nTest another phrase? [Y/n]: ").strip().lower()
        if again in ("n", "no", "exit", "q"):
            break

    print("\nTest completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExited.")
