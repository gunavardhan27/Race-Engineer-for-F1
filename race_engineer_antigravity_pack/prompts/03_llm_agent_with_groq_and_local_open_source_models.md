# Prompt 3: LLM agent with Groq AND local/open-source models

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** Check Groq's current model list before using the default model name; models are retired often.

```
Implement HOOK 2 in llm.py and agent.py. The LLM backend must be swappable from config.yaml.

Providers (llm.py), one interface:
    class LLMProvider(Protocol):
        async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]
        async def warmup(self) -> None

1. GroqProvider: OpenAI-compatible endpoint (base_url https://api.groq.com/openai/v1), API key
   from env var GROQ_API_KEY. Model from config, default "llama-3.1-8b-instant". Check Groq's
   current model list and make sure the default model still exists.
2. OllamaProvider (local, free, open source): use Ollama's NATIVE API (POST /api/chat with
   "stream": true), not the /v1 endpoint, because only the native API accepts "options".
   Send options {"num_gpu": 0 (CPU only, configurable), "num_ctx": 2048,
   "num_predict": max_tokens, "temperature": 0.3} and "keep_alive": "60m" so the model stays
   loaded. Default model "qwen2.5:3b-instruct"; also document "llama3.2:3b" and
   "qwen2.5:1.5b-instruct" as faster options for weak CPUs.
3. OpenAICompatibleProvider (local): any OpenAI-compatible server, e.g. llama.cpp's
   llama-server or LM Studio (default base_url http://localhost:8080/v1, no key).

Config:
    llm:
      primary: groq            # groq | ollama | openai_compatible
      fallback: ollama         # used if primary errors, times out, or no internet; or null
      timeout_s: 4
      max_tokens: 60
      groq: {model: llama-3.1-8b-instant}
      ollama: {base_url: http://localhost:11434, model: qwen2.5:3b-instruct, num_gpu: 0}
      openai_compatible: {base_url: http://localhost:8080/v1, model: local}

Behavior:
- Call warmup() for primary and fallback at startup (tiny prompt) so the first real request
  isn't a cold start. Log which providers are ready.
- If primary fails or doesn't produce a first token within timeout_s, retry once on fallback.
  If both fail, say "Radio check, say again." via the deterministic path.
- Use httpx.AsyncClient (one client per provider, reused) for all HTTP calls.

Agent (agent.py):
- build_prompt(question, snapshot, knowledge_text) -> (system, user). The user message contains
  STATE as compact JSON (state.snapshot(), with None fields removed), then TRACK NOTES if any,
  then DRIVER: <question>.
- Use the system prompt in prompts/engineer_system.txt (create it with the text below).
- Stream tokens; split into sentences on . ! ? and push each finished sentence to the radio
  queue as priority -1 immediately, so TTS starts before the whole reply is done.
- Clean output for speech: strip markdown, emojis, brackets; expand "P3" → "P3" is fine,
  "0.3s" → "0.3". Hard cap at 2 sentences.
- Guardrail: if the reply contains a number that doesn't appear in the snapshot JSON or the
  question, and isn't a small setup step (1–3 clicks/steps), log a warning with the reply.
  Don't block it, just log, so we can tune the prompt.
- Engineer.ask(): route() first; on kind="ai", run the agent as an asyncio task. If the driver
  presses PTT again, cancel the in-flight agent task.

Contents of prompts/engineer_system.txt:
<<<
You are the driver's race engineer on the pit wall during an F1 25 race, speaking over team radio.

Rules:
- Reply in at most 2 short sentences, under 30 words total. Radio style: direct, calm, no filler,
  no greetings, no "as an AI".
- Only use numbers that appear in STATE. If what you need isn't in STATE, say you don't have it.
- Give one clear action when the driver asks for help. Prefer things the driver can change NOW
  from the car: brake bias, differential (on-throttle), ERS mode, driving lines and inputs
  (earlier/later braking, lift points, smoother throttle). Front wing angle can only change at a
  pit stop. Other setup items are locked in a race.
- For setup advice, give direction and size in clicks or steps ("one click more front wing at the
  stop", "bias two forward"), never absolute setup values.
- Wheel order in STATE is rear left, rear right, front left, front right.
- If the driver reports a problem, acknowledge it in 1–3 words, then advise ("Copy. ...").
- Strategy questions: use pit_window, tyre wear and wear_pct_per_lap, fuel laps_delta, gaps and
  weather.next_rain. Commit to a recommendation; don't list options.
- Never use markdown, lists, emojis or symbols. Write numbers the way an engineer says them
  ("gap one point two" is fine as "gap 1.2").
>>>

Tests with a fake provider: streaming → sentence chunks, fallback on timeout, fallback on error,
cancellation on new PTT, speech cleaning, the number guardrail.

Also add docs/LOCAL_LLM.md: how to install Ollama on Windows, `ollama pull qwen2.5:3b-instruct`,
how to run it CPU-only, how to point the app at llama.cpp/LM Studio instead, and an honest note:
on a laptop CPU a 3B model takes roughly 1–3 s for a short reply, so Groq is the faster primary and
the local model is the offline/free fallback. Running the local model on the RTX 2050 while the
game runs will likely cause stutter.
```
