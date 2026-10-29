"""LLM providers and swappable streaming client.

Supports:
1. GroqProvider (remote, fast, default llama-3.1-8b-instant)
2. OllamaProvider (local, native API with CPU-only options and keep_alive)
3. OpenAICompatibleProvider (llama.cpp llama-server, LM Studio)

Includes warm-up at startup and automatic fallback on timeout/error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional, Protocol

import httpx

from config import LLMConfig

log = logging.getLogger("engineer.llm")


class LLMProvider(Protocol):
    async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]:
        """Streams text tokens from the LLM."""
        ...

    async def warmup(self) -> None:
        """Sends a minimal 1-token prompt to warm up connection/model."""
        ...

    async def aclose(self) -> None:
        """Closes the underlying HTTP client."""
        ...


class GroqProvider:
    def __init__(self, model: str = "llama-3.1-8b-instant", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.base_url = "https://api.groq.com/openai/v1"
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)

    async def warmup(self) -> None:
        if not self.api_key:
            log.warning("GroqProvider: GROQ_API_KEY not set; skipping warmup")
            return
        try:
            tokens = []
            async for tok in self.stream(system="Warmup", user="Hi", max_tokens=1, timeout_s=3.0):
                tokens.append(tok)
            log.info("GroqProvider ready (model=%s)", self.model)
        except Exception as e:
            log.warning("GroqProvider warmup failed: %s", e)

    async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY environment variable is not set")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": True,
        }

        req = self.client.build_request("POST", "/chat/completions", headers=headers, json=payload, timeout=timeout_s)
        resp = await self.client.send(req, stream=True)
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise RuntimeError(f"Groq API error {resp.status_code}: {body.decode(errors='replace')}")

        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                except json.JSONDecodeError:
                    continue
        finally:
            await resp.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()


class OllamaProvider:
    """Uses Ollama's native /api/chat endpoint to support CPU-only options and keep_alive."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:3b-instruct", num_gpu: int = 0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_gpu = num_gpu
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=15.0)

    async def warmup(self) -> None:
        try:
            tokens = []
            async for tok in self.stream(system="Warmup", user="Hi", max_tokens=1, timeout_s=4.0):
                tokens.append(tok)
            log.info("OllamaProvider ready (model=%s, num_gpu=%d)", self.model, self.num_gpu)
        except Exception as e:
            log.warning("OllamaProvider warmup failed (is Ollama running?): %s", e)

    async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "options": {
                "num_gpu": self.num_gpu,
                "num_ctx": 2048,
                "num_predict": max_tokens,
                "temperature": 0.3,
            },
            "keep_alive": "60m",
        }

        req = self.client.build_request("POST", "/api/chat", json=payload, timeout=timeout_s)
        resp = await self.client.send(req, stream=True)
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise RuntimeError(f"Ollama API error {resp.status_code}: {body.decode(errors='replace')}")

        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue
        finally:
            await resp.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()


class OpenAICompatibleProvider:
    """Local OpenAI-compatible provider (e.g. llama.cpp llama-server, LM Studio)."""

    def __init__(self, base_url: str = "http://localhost:8080/v1", model: str = "local"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)

    async def warmup(self) -> None:
        try:
            tokens = []
            async for tok in self.stream(system="Warmup", user="Hi", max_tokens=1, timeout_s=3.0):
                tokens.append(tok)
            log.info("OpenAICompatibleProvider ready (base_url=%s, model=%s)", self.base_url, self.model)
        except Exception as e:
            log.warning("OpenAICompatibleProvider warmup failed: %s", e)

    async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]:
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": True,
        }

        req = self.client.build_request("POST", "/chat/completions", headers=headers, json=payload, timeout=timeout_s)
        resp = await self.client.send(req, stream=True)
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise RuntimeError(f"OpenAICompatible API error {resp.status_code}: {body.decode(errors='replace')}")

        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                except json.JSONDecodeError:
                    continue
        finally:
            await resp.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()


def create_provider(name: str, config: LLMConfig) -> Optional[LLMProvider]:
    """Factory creating an LLMProvider by name."""
    name = (name or "").lower()
    if name == "groq":
        return GroqProvider(model=config.groq.model)
    elif name == "ollama":
        return OllamaProvider(base_url=config.ollama.base_url, model=config.ollama.model, num_gpu=config.ollama.num_gpu)
    elif name == "openai_compatible":
        return OpenAICompatibleProvider(base_url=config.openai_compatible.base_url, model=config.openai_compatible.model)
    return None


class LLMClient:
    """Coordinates primary provider with automatic fallback and warmup."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.primary_name = config.primary
        self.fallback_name = config.fallback
        self.primary = create_provider(self.primary_name, config)
        self.fallback = create_provider(self.fallback_name, config) if self.fallback_name else None

    async def warmup(self) -> None:
        """Warms up both primary and fallback providers."""
        if self.primary:
            await self.primary.warmup()
        if self.fallback:
            await self.fallback.warmup()

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        """Streams tokens from primary; if primary errors or fails to yield first token within timeout_s, retries on fallback."""
        timeout_s = self.config.timeout_s
        max_tokens = self.config.max_tokens

        # Try primary provider
        if self.primary:
            try:
                gen = self.primary.stream(system, user, max_tokens, timeout_s)
                first_token = await asyncio.wait_for(gen.__anext__(), timeout=timeout_s)
                yield first_token
                async for tok in gen:
                    yield tok
                return
            except (asyncio.TimeoutError, Exception) as e:
                log.warning("Primary LLM (%s) failed or timed out: %s. Retrying with fallback (%s)...",
                            self.primary_name, e, self.fallback_name)

        # Try fallback provider if primary failed
        if self.fallback:
            try:
                gen = self.fallback.stream(system, user, max_tokens, timeout_s)
                first_token = await asyncio.wait_for(gen.__anext__(), timeout=timeout_s)
                yield first_token
                async for tok in gen:
                    yield tok
                return
            except (asyncio.TimeoutError, Exception) as e:
                log.error("Fallback LLM (%s) failed or timed out: %s", self.fallback_name, e)

    async def complete(self, system: str, user: str) -> str:
        """Collects full streamed response into a single string."""
        buf = []
        async for token in self.stream(system, user):
            buf.append(token)
        return "".join(buf)

    async def aclose(self) -> None:
        if self.primary:
            await self.primary.aclose()
        if self.fallback:
            await self.fallback.aclose()
