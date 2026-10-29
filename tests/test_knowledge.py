"""Unit tests for knowledge base: section selection, corner extraction, and token cap."""
from pathlib import Path
import pytest

from config import KnowledgeConfig
from knowledge import KnowledgeBase, parse_markdown_sections


def test_parse_markdown_sections():
    """Verify markdown sections are parsed accurately by ## headings."""
    sample = """# Title
Intro text here.

## Corners
1. Copse: fast right
2. Stowe: heavy braking

## Tyres
Front-left wears quickly.
"""
    sections = parse_markdown_sections(sample)
    assert "Corners" in sections
    assert "Tyres" in sections
    assert "Copse: fast right" in sections["Corners"]
    assert "Front-left wears quickly." in sections["Tyres"]


def test_load_silverstone_corners():
    """Verify loading silverstone.md extracts all corner names for STT prompt biasing."""
    kb = KnowledgeBase()
    loaded = kb.load_track("Silverstone")
    assert loaded

    corners = kb.get_corner_names()
    assert "Copse" in corners
    assert "Maggotts" in corners
    assert "Becketts" in corners
    assert "Stowe" in corners
    assert "Club" in corners


def test_select_knowledge_corner_matching():
    """Asking about Copse must specifically include Copse's corner line in injected context."""
    kb = KnowledgeBase()
    kb.load_track("Silverstone")

    context = kb.select_knowledge("I'm having heavy understeer at Copse")
    assert "Corner Note:" in context
    assert "Copse" in context
    assert "high-speed right" in context


def test_select_knowledge_tyres_keyword():
    """Tyre questions must pull track tyre notes."""
    kb = KnowledgeBase()
    kb.load_track("Silverstone")

    context = kb.select_knowledge("How is tyre wear on this stint?")
    assert "Track Tyres:" in context
    assert "front-left" in context.lower() or "thermal" in context.lower()


def test_select_knowledge_setup_and_general_notes():
    """Setup and brake bias questions must pull track setup and general car components."""
    kb = KnowledgeBase()
    kb.load_track("Silverstone")

    context = kb.select_knowledge("What brake bias should I run to fix front locking?")
    assert "Brake Bias Guide:" in context or "Track Setup:" in context


def test_select_knowledge_overtaking_keyword():
    """Overtake/DRS questions must pull overtaking zones."""
    kb = KnowledgeBase()
    kb.load_track("Silverstone")

    context = kb.select_knowledge("Where is the best place to overtake?")
    assert "Overtaking Zones:" in context
    assert "Hangar Straight" in context or "Brooklands" in context


def test_token_cap_enforcement():
    """Injected knowledge must not exceed the configured token limit."""
    cfg = KnowledgeConfig(max_tokens=30)   # 30 tokens = 120 chars max
    kb = KnowledgeBase(config=cfg)
    kb.load_track("Silverstone")

    context = kb.select_knowledge("Give me full setup, tyre, and overtaking guide for Copse, Stowe, and Becketts", max_tokens=30)
    # 30 tokens * 4 chars/token = 120 chars (+ a small margin for ellipsis)
    assert len(context) <= 130
