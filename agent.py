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


REFUSAL_PATTERN = re.compile(
    r"\b(I cannot|I can't|I am unable|I'm unable|unable to comply|as an AI|I am an AI|my guidelines|apologize|I'm sorry|apologies)\b",
    re.I,
)


def is_refusal_or_empty(text: str) -> bool:
    """Checks if model output is empty, an apology, or a safety refusal."""
    if not text or not text.strip():
        return True
    return bool(REFUSAL_PATTERN.search(text))


def build_prompt(
    question: str,
    snapshot: dict,
    knowledge_text: str = "",
    persona_name: str = "cocky",
    profanity: str = "mild",
    system_prompt_path: str = "prompts/engineer_system.txt",
    personas_dir: str = "prompts/personas",
) -> tuple[str, str]:
    """Constructs (system_prompt, user_message) with compact telemetry state and persona additions."""
    p = Path(system_prompt_path)
    if p.is_file():
        system_prompt = p.read_text(encoding="utf-8").strip()
    else:
        system_prompt = (
            "You are the driver's race engineer on the pit wall during an F1 25 race, speaking over team radio.\n"
            "Reply in at most 2 short sentences, under 30 words total. Only use numbers from STATE."
        )

    if persona_name and persona_name != "professional":
        p_persona = Path(personas_dir) / f"{persona_name}.txt"
        if p_persona.is_file():
            system_prompt += "\n\n" + p_persona.read_text(encoding="utf-8").strip()

        prof = (profanity or "mild").lower()
        if prof == "none":
            prof_inst = "Do not swear."
        elif prof == "mild":
            prof_inst = "Mild swearing allowed."
        else:
            prof_inst = "Swearing allowed, keep it natural, not every sentence."
        system_prompt += f"\n\nProfanity guidelines: {prof_inst}"

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
        persona_name: str = "cocky",
        profanity: str = "mild",
    ):
        self.llm = llm
        self.radio = radio
        self.latency = latency
        self.persona_name = persona_name
        self.profanity = profanity

    async def answer(
        self,
        question: str,
        snapshot: dict,
        knowledge_text: str = "",
        fallback_text: Optional[str] = None,
    ) -> str:
        """Streams LLM reply, splits into sentences, pushes to RadioQueue, and falls back if refused/empty."""
        system, user = build_prompt(
            question,
            snapshot,
            knowledge_text,
            persona_name=self.persona_name,
            profanity=self.profanity,
        )
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
                        if is_refusal_or_empty(cleaned):
                            all_sentences = [cleaned]
                            break
                        sentences_pushed += 1
                        all_sentences.append(cleaned)
                        self.radio.push(Callout(-1, time.monotonic(), cleaned))

                    if sentences_pushed >= 2:
                        break

                if sentences_pushed >= 2 or (all_sentences and is_refusal_or_empty(all_sentences[0])):
                    break

            # Handle remaining buffer if under 2 sentences
            if sentences_pushed < 2 and buffer.strip():
                cleaned = clean_for_speech(buffer.strip())
                if cleaned:
                    if is_refusal_or_empty(cleaned):
                        all_sentences = [cleaned]
                    else:
                        all_sentences.append(cleaned)
                        self.radio.push(Callout(-1, time.monotonic(), cleaned))

        finally:
            if self.latency:
                self.latency.record("llm_done")

        full_reply = " ".join(all_sentences).strip()

        # Check refusal or empty response -> fallback
        if is_refusal_or_empty(full_reply):
            log.warning("LLM response was refused or empty (%r); using fallback", full_reply)
            while sentences_pushed > 0:
                self.radio.pop()
                sentences_pushed -= 1
            chosen_fallback = fallback_text or ("Radio check, say again." if not full_reply else "Copy, keep pushing.")
            self.radio.push(Callout(-1, time.monotonic(), chosen_fallback))
            return chosen_fallback

        # Validate numbers against telemetry state
        check_number_guardrail(full_reply, snapshot_json, question)
        return full_reply

    async def generate_post_race_message(
        self,
        summary: dict,
    ) -> str:
        """Generates a 2-3 sentence post-race radio debrief message in persona from race summary."""
        system = (
            "You are the driver's race engineer speaking over team radio right after crossing the finish line.\n"
            "Write a concise 2 to 3 sentence post-race message reacting to the race results."
        )
        if self.persona_name and self.persona_name != "professional":
            p_persona = Path("prompts/personas") / f"{self.persona_name}.txt"
            if p_persona.is_file():
                system += "\n\n" + p_persona.read_text(encoding="utf-8").strip()
            prof = (self.profanity or "mild").lower()
            prof_inst = "Do not swear." if prof == "none" else ("Mild swearing allowed." if prof == "mild" else "Swearing allowed, keep it natural, not every sentence.")
            system += f"\n\nProfanity guidelines: {prof_inst}"

        summary_json = json.dumps(summary, indent=2)
        user = f"RACE SUMMARY:\n{summary_json}\n\nDeliver the post-race debrief over the radio now."

        tokens = []
        try:
            async for tok in self.llm.stream(system, user, max_tokens=100, timeout_s=6.0):
                tokens.append(tok)
            result = clean_for_speech("".join(tokens).strip())
            if not is_refusal_or_empty(result):
                return result
        except Exception as e:
            log.warning("Post-race LLM message generation failed: %s", e)

        pos = summary.get("finish_position", 10)
        if pos == 1:
            return "P1! You've won it! Absolutely mega drive!"
        elif 2 <= pos <= 3:
            return f"P{pos}! On the podium, mate! Great drive today."
        elif pos <= 10:
            return f"P{pos}, points in the bag. Solid job today."
        return f"P{pos}. Not our day today, but we'll come back stronger."
