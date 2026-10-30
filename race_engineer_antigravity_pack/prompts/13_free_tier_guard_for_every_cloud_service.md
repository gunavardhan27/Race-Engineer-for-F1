# Prompt 13: Free-tier guard for every cloud service

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Add a usage guard so the app never exceeds a free tier and never incurs a charge. Default STT
(faster-whisper) and TTS (Piper/Kokoro) and Ollama are local and unlimited; the guard covers every
cloud service: Groq LLM, Groq speech-to-text (if added as an STT option), and edge-tts.

1. Check the real limits first.
   - Look up Groq's current free-tier rate limits for every model we use (requests per minute,
     requests per day, tokens per minute, tokens per day; audio seconds per hour/day for Groq
     Whisper) on Groq's official docs (console.groq.com/docs/rate-limits). Put them in
     config.yaml as defaults, with the date you checked in a comment.
   - edge-tts has no published quota: set a conservative self-imposed limit
     (e.g. 20 requests/minute) and note it's our own limit, not an official one.
   - These config values are a fallback. The live source of truth is Groq's rate-limit response
     headers (x-ratelimit-limit-requests, x-ratelimit-remaining-requests,
     x-ratelimit-reset-requests, x-ratelimit-limit-tokens, x-ratelimit-remaining-tokens,
     x-ratelimit-reset-tokens). Verify the exact header names against Groq's docs.

2. usage.py — one UsageGuard used by every cloud provider:
   - Before each call: check_allowed(service, est_tokens) returns allow / deny with reason.
   - After each call: record actual usage from the response (usage.total_tokens for LLM calls,
     audio duration for STT, characters/requests for TTS) and the rate-limit headers.
   - Track rolling minute windows in memory and daily totals in SQLite (table usage_daily), so
     restarting the app doesn't reset the daily count. Use reset times from headers when present.
   - Safety margin: config usage.max_fraction (default 0.8). The app stops using a service at 80%
     of any limit (per minute or per day), not at 100%.
   - Estimate tokens before sending (chars / 4 for prompt + max_tokens) so a request that would
     cross a limit is denied up front.

3. Automatic fallback instead of failure:
   - LLM denied or 429 → use the local fallback (Ollama) for that request; if no local LLM is
     configured, answer deterministically or say "Radio check, say again."
   - edge-tts denied or failing → Piper. Groq STT denied → local faster-whisper.
   - On HTTP 429: respect retry-after, mark the service paused until then, never retry in a loop.
     At most one retry per request, and only on the fallback.
   - Cache TTS audio for repeated text (spotter phrases and persona lines are already
     pre-rendered; also cache any cloud-TTS output by text + voice) so cloud TTS is only called
     for new text.

4. Never pay:
   - config usage.allow_paid: false (default). Groq's free plan needs no card, but if the API
     key's account is on a paid tier, the response headers will show higher limits: if limits
     from headers exceed the configured free-tier values, log a warning and keep enforcing the
     configured free-tier values while allow_paid is false.
   - Add no paid APIs anywhere in the app. If any provider is added later, it must go through
     UsageGuard.

5. Keep usage low:
   - Measure prompt size per AI request and log it. Strip None/empty fields from the snapshot,
     round floats, and only include the sections relevant to the question's intent, so a
     typical request stays under ~800 prompt tokens.
   - max_tokens stays at 60 for in-race answers; 150 for the post-race message.

6. Visibility:
   - Dashboard "Usage" panel: for each cloud service, used/limit for the current minute and
     today, percentage bars, current state (ok / near limit / paused / using fallback), and
     which provider served the last answer.
   - CLI: `python main.py --usage` prints today's usage and limits and exits.
   - Log one warning line when a service crosses 50% and 80% of a daily limit. No spoken
     warnings during a race.

Tests with fake providers and fake headers: denial at 80% of per-minute and per-day limits,
daily totals surviving a restart, 429 with retry-after pausing the service, fallback chain for
LLM, TTS and STT, header limits higher than configured free-tier values while allow_paid is false,
token estimation, and TTS cache hits not counting as usage.
```
