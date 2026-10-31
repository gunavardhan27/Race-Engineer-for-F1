"""Unit tests for LLM providers and fallback client using fake providers."""
import asyncio
from typing import AsyncIterator
import pytest

from config import LLMConfig
from llm import LLMClient


class FakeProvider:
    def __init__(self, tokens: list[str] = None, delay_first_token: float = 0.0, should_raise: bool = False):
        self.tokens = tokens if tokens is not None else ["Copy", ", ", "pit", " this", " lap", "."]
        self.delay_first_token = delay_first_token
        self.should_raise = should_raise
        self.warmed_up = False

    async def warmup(self) -> None:
        self.warmed_up = True

    async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]:
        if self.should_raise:
            raise RuntimeError("Provider connection failed")
        if self.delay_first_token > 0:
            await asyncio.sleep(self.delay_first_token)
        for tok in self.tokens:
            yield tok

    async def aclose(self) -> None:
        pass


def test_fallback_on_error():
    """If primary provider errors, client retries once on fallback."""
    async def _run():
        cfg = LLMConfig(primary="groq", fallback="ollama", timeout_s=0.2)
        client = LLMClient(cfg)
        client.primary = FakeProvider(should_raise=True)
        client.fallback = FakeProvider(tokens=["Fallback", " response", "."])

        collected = []
        async for tok in client.stream("sys", "usr"):
            collected.append(tok)

        assert "".join(collected) == "Fallback response."

    asyncio.run(_run())


def test_fallback_on_timeout():
    """If primary provider doesn't produce first token within timeout_s, client retries on fallback."""
    async def _run():
        cfg = LLMConfig(primary="groq", fallback="ollama", timeout_s=0.1)
        client = LLMClient(cfg)
        # Primary delays 0.5s (longer than timeout_s 0.1s)
        client.primary = FakeProvider(delay_first_token=0.5, tokens=["Too", " slow"])
        client.fallback = FakeProvider(tokens=["Fast", " fallback", "."])

        collected = []
        async for tok in client.stream("sys", "usr"):
            collected.append(tok)

        assert "".join(collected) == "Fast fallback."

    asyncio.run(_run())


def test_both_providers_fail():
    """When both primary and fallback fail, stream finishes empty without crashing."""
    async def _run():
        cfg = LLMConfig(primary="groq", fallback="ollama", timeout_s=0.1)
        client = LLMClient(cfg)
        client.primary = FakeProvider(should_raise=True)
        client.fallback = FakeProvider(should_raise=True)

        collected = []
        async for tok in client.stream("sys", "usr"):
            collected.append(tok)

        assert len(collected) == 0

    asyncio.run(_run())


def test_client_warmup():
    """warmup() calls warmup on both providers."""
    async def _run():
        cfg = LLMConfig(primary="groq", fallback="ollama")
        client = LLMClient(cfg)
        p1 = FakeProvider()
        p2 = FakeProvider()
        client.primary = p1
        client.fallback = p2

        await client.warmup()
        assert p1.warmed_up
        assert p2.warmed_up

    asyncio.run(_run())


def test_load_env_file_and_provider_keys(tmp_path, monkeypatch):
    """load_env_file loads variables into os.environ and providers consume them."""
    import os
    from config import load_env_file
    from llm import GroqProvider, OpenAICompatibleProvider, create_provider

    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=gsk_test_groq_12345\nLLAMA_API_KEY=test_llama_67890\n", encoding="utf-8")

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)

    load_env_file(env_file)

    assert os.getenv("GROQ_API_KEY") == "gsk_test_groq_12345"
    assert os.getenv("LLAMA_API_KEY") == "test_llama_67890"

    groq = GroqProvider()
    assert groq.api_key == "gsk_test_groq_12345"

    llama_prov = OpenAICompatibleProvider()
    assert llama_prov.api_key == "test_llama_67890"
    assert llama_prov.client.headers.get("authorization") == "Bearer test_llama_67890"

    prov = create_provider("llama", LLMConfig())
    assert isinstance(prov, OpenAICompatibleProvider)
