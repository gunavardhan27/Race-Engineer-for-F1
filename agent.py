"""Race Engineer AI Agent.

Builds prompt from compact telemetry state JSON and track notes,
streams tokens from LLM, splits sentences on punctuation, cleans for speech,
caps at 2 sentences, pushes to RadioQueue at priority -1, and runs number guardrail.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Optional

from f1telemetry.deterministic import Callout, RadioQueue
from latency import LatencyTracker
from llm import LLMClient

log = logging.getLogger("engineer.agent")


def clean_snapshot(d: Any) -> Any:
    """Recursively removes None fields from dict/list for compact prompt injection."""
    if isinstance(d, dict):
        return {k: clean_snapshot(v) for k, v in d.items() if v is not None}
    elif isinstance(d, list):
        return [clean_snapshot(v) for v in d if v is not None]
    return d


def clean_for_speech(text: str) -> str:
    """Cleans LLM response text for speech output.

    Strips markdown formatting, brackets, emojis, and normalizes time units like '0.3s' -> '0.3'.
    Preserves position designations like 'P3'.
    """
    if not text:
        return ""

    # Remove markdown symbols: *, #, _, `, ~, >
    text = re.sub(r"[*#_`~>]", "", text)

    # Remove brackets: [], (), {}
    text = re.sub(r"[\[\]\(\)\{\}]", "", text)

    # Strip emojis and special non-ASCII symbols
    text = re.sub(r"[^\x00-\x7F]+", "", text)

    # Convert e.g. "0.3s" -> "0.3", "1.5s" -> "1.5", "2s" -> "2"
    text = re.sub(r"\b(\d+(?:\.\d+)?)s\b", r"\1", text, flags=re.I)

    # Remove bullet markers: "- ", "* "
    text = re.sub(r"^\s*[-*•]\s*", "", text, flags=re.M)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def check_number_guardrail(reply: str, snapshot_str: str, question: str) -> list[str]:
    """Checks if reply contains numbers not present in snapshot or question (except 1-3 setup clicks)."""
    reply_nums = re.findall(r"\b\d+(?:\.\d+)?\b", reply)
    if not reply_nums:
        return []

    context_str = f"{snapshot_str} {question}"
    context_nums = set(re.findall(r"\b\d+(?:\.\d+)?\b", context_str))

    # Small setup adjustments: 1, 2, 3 clicks/steps are always allowed
    allowed_setup = {"1", "2", "3", "1.0", "2.0", "3.0"}
    hallucinated = []
    for n in reply_nums:
        if n not in context_nums and n not in allowed_setup:
            hallucinated.append(n)

    if hallucinated:
        log.warning(
            "Number guardrail warning: reply %r contains numbers %s not found in telemetry state or question",
            reply,
            hallucinated,
        )
    return hallucinated


def build_prompt(
    question: str,
    snapshot: dict,
    knowledge_text: str = "",
    system_prompt_path: str = "prompts/engineer_system.txt",
) -> tuple[str, str]:
    """Constructs (system_prompt, user_message) with compact telemetry state."""
    p = Path(system_prompt_path)
    if p.is_file():
        system_prompt = p.read_text(encoding="utf-8").strip()
    else:
        system_prompt = (
            "You are the driver's race engineer on the pit wall during an F1 25 race, speaking over team radio.\n"
            "Reply in at most 2 short sentences, under 30 words total. Only use numbers from STATE."
        )

    clean_state = clean_snapshot(snapshot)
    state_json = json.dumps(clean_state, separators=(",", ":"))

    user_parts = [f"STATE:\n{state_json}"]
    if knowledge_text and knowledge_text.strip():
        user_parts.append(f"TRACK NOTES:\n{knowledge_text.strip()}")
    user_parts.append(f"DRIVER: {question.strip()}")

    user_message = "\n\n".join(user_parts)
    return system_prompt, user_message


class RaceEngineerAgent:
    def __init__(
        self,
        llm: LLMClient,
        radio: RadioQueue,
        latency: Optional[LatencyTracker] = None,
    ):
        self.llm = llm
        self.radio = radio
        self.latency = latency

    async def answer(self, question: str, snapshot: dict, knowledge_text: str = "") -> str:
        """Streams LLM reply, splits into sentences, and pushes each sentence to RadioQueue at priority -1."""
        system, user = build_prompt(question, snapshot, knowledge_text)
        snapshot_json = json.dumps(clean_snapshot(snapshot))

        first_token_recorded = False
        buffer = ""
        sentences_pushed = 0
        all_sentences = []

        try:
            async for token in self.llm.stream(system, user):
                if not first_token_recorded and token.strip():
                    first_token_recorded = True
                    if self.latency:
                        self.latency.record("llm_first_token")

                buffer += token

                # Split sentences on . ! ? (not inside decimal numbers like 1.2)
                while True:
                    match = re.search(r"(?<=[^0-9\s])[.!?](?:\s+|$)", buffer)
                    if not match:
                        break

                    sentence_raw = buffer[: match.end()].strip()
                    buffer = buffer[match.end() :]

                    cleaned = clean_for_speech(sentence_raw)
                    if cleaned:
                        sentences_pushed += 1
                        all_sentences.append(cleaned)
                        self.radio.push(Callout(-1, time.monotonic(), cleaned))

                    if sentences_pushed >= 2:
                        break

                if sentences_pushed >= 2:
                    break

            # Handle remaining buffer if under 2 sentences
            if sentences_pushed < 2 and buffer.strip():
                cleaned = clean_for_speech(buffer.strip())
                if cleaned:
                    all_sentences.append(cleaned)
                    self.radio.push(Callout(-1, time.monotonic(), cleaned))

        finally:
            if self.latency:
                self.latency.record("llm_done")

        full_reply = " ".join(all_sentences)
        if not full_reply:
            fallback_text = "Radio check, say again."
            self.radio.push(Callout(-1, time.monotonic(), fallback_text))
            return fallback_text

        # Validate numbers against telemetry state
        check_number_guardrail(full_reply, snapshot_json, question)
        return full_reply
