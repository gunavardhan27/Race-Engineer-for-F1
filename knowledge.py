"""Track and handling knowledge base.

Loads structured track markdown files and general car notes without needing a vector database.
Selects sections via simple keyword matching and extracts corner names for STT prompt biasing.
Enforces a configurable token cap (~400 tokens / 1600 characters).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any, Optional

from config import KnowledgeConfig
from f1telemetry.state import TRACKS

log = logging.getLogger("engineer.knowledge")


def track_to_slug(track_id_or_name: int | str) -> str:
    """Normalizes track id or name to filesystem slug."""
    if isinstance(track_id_or_name, int):
        track_name = TRACKS.get(track_id_or_name, f"track_{track_id_or_name}")
    else:
        track_name = str(track_id_or_name)
    slug = track_name.lower().replace(" ", "_").replace("-", "_")
    if slug.endswith(".md"):
        slug = slug[:-3]
    return slug


def parse_corners_list(corners_text: str) -> list[dict[str, Any]]:
    """Parses Corners markdown section into list of {number, name, description}."""
    corners: list[dict[str, Any]] = []
    for line in corners_text.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        m = re.match(r"^(\d+)\.\s*([^:]+?)\s*:\s*(.*)$", line_str)
        if m:
            corners.append({
                "number": int(m.group(1)),
                "name": m.group(2).strip(),
                "description": m.group(3).strip(),
            })
        else:
            m2 = re.match(r"^(\d+)\.\s*(.*)$", line_str)
            if m2:
                corners.append({
                    "number": int(m2.group(1)),
                    "name": m2.group(2).strip(),
                    "description": "",
                })
    return corners


def build_corners_markdown(corners: list[dict[str, Any]]) -> str:
    """Formats corner dicts back into markdown numbered list."""
    lines: list[str] = []
    for c in sorted(corners, key=lambda x: x.get("number", 0)):
        num = c.get("number", 1)
        name = c.get("name", "").strip()
        desc = c.get("description", "").strip()
        if desc:
            lines.append(f"{num}. {name}: {desc}")
        else:
            lines.append(f"{num}. {name}")
    return "\n".join(lines)


def parse_markdown_sections(text: str) -> dict[str, str]:
    """Splits markdown into a dictionary of section_title -> section_body."""
    sections: dict[str, str] = {}
    current_title = "Intro"
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections[current_title.strip()] = "\n".join(current_lines).strip()
                current_lines = []
            current_title = line[3:].strip()
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_title.strip()] = "\n".join(current_lines).strip()

    return sections


class KnowledgeBase:
    def __init__(self, config: Optional[KnowledgeConfig] = None):
        self.config = config or KnowledgeConfig()
        self.tracks_dir = Path(self.config.tracks_dir)
        self.general_path = Path(self.config.general_path)

        self.current_track_name: Optional[str] = None
        self.track_sections: dict[str, str] = {}
        self.general_sections: dict[str, str] = {}
        self.corner_lines: dict[str, str] = {}      # lowercase_name -> full line
        self.corner_names: list[str] = []           # list of names for STT prompt

        self.load_general()

    def load_general(self) -> None:
        """Loads general handling and car setup notes."""
        if self.general_path.is_file():
            try:
                content = self.general_path.read_text(encoding="utf-8")
                self.general_sections = parse_markdown_sections(content)
                log.info("Loaded general knowledge (%d sections)", len(self.general_sections))
            except Exception as e:
                log.warning("Failed to load general notes from %s: %s", self.general_path, e)
        else:
            log.debug("General knowledge file not found: %s", self.general_path)

    def load_track(self, track_id_or_name: int | str) -> bool:
        """Loads the track markdown matching the track ID or name."""
        if isinstance(track_id_or_name, int):
            track_name = TRACKS.get(track_id_or_name, f"track_{track_id_or_name}")
        else:
            track_name = str(track_id_or_name)

        if track_name == self.current_track_name and self.track_sections:
            return True

        self.current_track_name = track_name
        self.track_sections.clear()
        self.corner_lines.clear()
        self.corner_names.clear()

        # Filename convention: silverstone.md, spa.md, red_bull_ring.md
        clean_filename = track_name.lower().replace(" ", "_").replace("-", "_") + ".md"
        track_file = self.tracks_dir / clean_filename

        if not track_file.is_file():
            # Try matching with simple lowercase
            track_file = self.tracks_dir / f"{track_name.lower()}.md"

        if not track_file.is_file():
            log.info("No track knowledge file found for '%s' (%s)", track_name, track_file.name)
            return False

        try:
            content = track_file.read_text(encoding="utf-8")
            self.track_sections = parse_markdown_sections(content)
            self._extract_corners(self.track_sections.get("Corners", ""))
            log.info("Loaded track notes for '%s' (%d corners, %d sections)",
                     track_name, len(self.corner_names), len(self.track_sections))
            return True
        except Exception as e:
            log.warning("Failed to load track file %s: %s", track_file, e)
            return False

    def _extract_corners(self, corners_text: str) -> None:
        """Extracts corner names and lines from the Corners section."""
        for line in corners_text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            # Matches: "9. Copse (T9): ..." or "1. Abbey: ..."
            m = re.match(r"^\d+\.\s*([A-Za-z0-9\s'-]+?)(?:\s*\([A-Za-z0-9]+\))?\s*:", line_str)
            if m:
                c_name = m.group(1).strip()
                self.corner_names.append(c_name)
                self.corner_lines[c_name.lower()] = line_str

    def get_corner_names(self) -> list[str]:
        """Returns corner names for STT initial_prompt biasing."""
        return list(self.corner_names)

    def list_available_tracks(self) -> list[dict[str, Any]]:
        """Lists all known tracks and whether they have markdown notes."""
        result: list[dict[str, Any]] = []
        seen_slugs: set[str] = set()

        for tid, tname in sorted(TRACKS.items(), key=lambda x: x[1]):
            slug = track_to_slug(tname)
            seen_slugs.add(slug)
            file_path = self.tracks_dir / f"{slug}.md"
            has_file = file_path.is_file()
            corners_count = 0
            if has_file:
                try:
                    txt = file_path.read_text(encoding="utf-8")
                    sec = parse_markdown_sections(txt)
                    corners_count = len(parse_corners_list(sec.get("Corners", "")))
                except Exception:
                    pass
            result.append({
                "id": tid,
                "name": tname,
                "slug": slug,
                "has_file": has_file,
                "corner_count": corners_count,
            })

        if self.tracks_dir.is_dir():
            for p in sorted(self.tracks_dir.glob("*.md")):
                if p.name.endswith(".bak") or p.name.endswith(".tmp"):
                    continue
                slug = p.stem
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    corners_count = 0
                    try:
                        txt = p.read_text(encoding="utf-8")
                        sec = parse_markdown_sections(txt)
                        corners_count = len(parse_corners_list(sec.get("Corners", "")))
                    except Exception:
                        pass
                    result.append({
                        "id": None,
                        "name": slug.replace("_", " ").title(),
                        "slug": slug,
                        "has_file": True,
                        "corner_count": corners_count,
                    })

        return sorted(result, key=lambda x: x["name"])

    def get_track_markdown(self, track_slug_or_id: str | int) -> dict[str, Any]:
        """Loads and parses track markdown file into structured sections and raw text."""
        slug = track_to_slug(track_slug_or_id)
        file_path = self.tracks_dir / f"{slug}.md"
        exists = file_path.is_file()

        raw = ""
        title = ""
        sections: dict[str, str] = {}
        corners: list[dict[str, Any]] = []

        if exists:
            try:
                raw = file_path.read_text(encoding="utf-8")
                sections = parse_markdown_sections(raw)
                corners = parse_corners_list(sections.get("Corners", ""))
                for line in raw.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
            except Exception as e:
                log.warning("Could not read track file %s: %s", file_path, e)

        if not title:
            if isinstance(track_slug_or_id, int) and track_slug_or_id in TRACKS:
                title = f"{TRACKS[track_slug_or_id]} Circuit"
            else:
                title = f"{slug.replace('_', ' ').title()} Circuit"

        if not exists:
            sections = {
                "Corners": "",
                "Overtaking": "",
                "Tyres": "",
                "Setup notes": "",
            }
            raw = f"# {title}\n\n## Corners\n\n## Overtaking\n\n## Tyres\n\n## Setup notes\n"

        return {
            "slug": slug,
            "title": title,
            "name": title,
            "raw": raw,
            "raw_markdown": raw,
            "sections": sections,
            "corners": corners,
            "corners_list": corners,
            "exists": exists,
        }

    def save_track_markdown(
        self,
        track_slug_or_id: str | int,
        content: str | dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically saves track markdown file, backs up previous version as .bak, and reloads knowledge."""
        slug = track_to_slug(track_slug_or_id)
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.tracks_dir / f"{slug}.md"
        bak_path = self.tracks_dir / f"{slug}.md.bak"
        tmp_path = self.tracks_dir / f"{slug}.md.tmp"

        if isinstance(content, str):
            raw_text = content
        else:
            title = content.get("title") or f"{slug.replace('_', ' ').title()} Circuit"
            corners_val = content.get("corners")
            if isinstance(corners_val, list):
                corners_text = build_corners_markdown(corners_val)
            else:
                corners_text = content.get("sections", {}).get("Corners", "").strip()

            overtaking = content.get("overtaking", "")
            if not overtaking and "sections" in content:
                overtaking = content["sections"].get("Overtaking", "").strip()

            tyres = content.get("tyres", "")
            if not tyres and "sections" in content:
                tyres = content["sections"].get("Tyres", "").strip()

            setup_notes = content.get("setup_notes", "")
            if not setup_notes and "sections" in content:
                setup_notes = content["sections"].get("Setup notes", "").strip()

            raw_text = (
                f"# {title}\n\n"
                f"## Corners\n{corners_text}\n\n"
                f"## Overtaking\n{overtaking}\n\n"
                f"## Tyres\n{tyres}\n\n"
                f"## Setup notes\n{setup_notes}\n"
            )

        # 1. Backup existing file to .bak
        if target_path.is_file():
            try:
                shutil.copy2(target_path, bak_path)
            except Exception as e:
                log.warning("Could not create backup %s: %s", bak_path, e)

        # 2. Write temp file in same directory
        tmp_path.write_text(raw_text, encoding="utf-8")

        # 3. Atomic rename/replace
        os.replace(str(tmp_path), str(target_path))
        log.info("Atomically saved track notes to %s (backup: %s)", target_path, bak_path)

        # 4. Immediate in-memory reload
        self.current_track_name = None  # Reset cache to force reload
        self.load_track(slug)

        return self.get_track_markdown(slug)


    def select_knowledge(self, question: str, max_tokens: Optional[int] = None) -> str:
        """Selects relevant track and setup notes via keyword overlap, capped at max_tokens."""
        if not self.config.enabled:
            return ""

        limit_tokens = max_tokens or self.config.max_tokens
        max_chars = limit_tokens * 4   # 4 characters per token heuristic
        q_lower = question.lower()
        selected_parts: list[str] = []

        # 1. Corner matching: if any corner is named, always include that corner's line
        for c_lower, c_line in self.corner_lines.items():
            if re.search(r"\b" + re.escape(c_lower) + r"\b", q_lower):
                selected_parts.append(f"Corner Note: {c_line}")

        # 2. Tyres
        if any(w in q_lower for w in ("tyre", "tyres", "tire", "tires", "wear", "deg", "blister", "grain")):
            if "Tyres" in self.track_sections:
                selected_parts.append(f"Track Tyres:\n{self.track_sections['Tyres']}")

        # 3. Setup, handling, and balance
        if any(w in q_lower for w in ("setup", "balance", "understeer", "oversteer", "diff", "bias", "wing", "roll", "stiff", "loose", "snap")):
            if "Setup notes" in self.track_sections:
                selected_parts.append(f"Track Setup:\n{self.track_sections['Setup notes']}")

            # Match general car components
            if any(w in q_lower for w in ("bias", "brake", "locking", "stopping")):
                if "Brake Bias" in self.general_sections:
                    selected_parts.append(f"Brake Bias Guide:\n{self.general_sections['Brake Bias']}")

            if any(w in q_lower for w in ("diff", "differential", "traction", "rotation")):
                if "Differential (On-Throttle)" in self.general_sections:
                    selected_parts.append(f"Diff Guide:\n{self.general_sections['Differential (On-Throttle)']}")

            if any(w in q_lower for w in ("wing", "aero", "downforce")):
                if "Front Wing Angle" in self.general_sections:
                    selected_parts.append(f"Front Wing Guide:\n{self.general_sections['Front Wing Angle']}")

            if any(w in q_lower for w in ("line", "trail", "throttle", "coast")):
                if "Driving Line Adjustments" in self.general_sections:
                    selected_parts.append(f"Driving Line Guide:\n{self.general_sections['Driving Line Adjustments']}")

        # 4. Overtaking & Strategy
        if any(w in q_lower for w in ("overtake", "pass", "drs", "defend", "gap", "dive")):
            if "Overtaking" in self.track_sections:
                selected_parts.append(f"Overtaking Zones:\n{self.track_sections['Overtaking']}")

        # Fallback if no specific keyword matched: provide general setup or track setup
        if not selected_parts:
            if "Setup notes" in self.track_sections:
                selected_parts.append(f"Track Setup:\n{self.track_sections['Setup notes']}")
            elif "Brake Bias" in self.general_sections:
                selected_parts.append(f"Brake Bias Guide:\n{self.general_sections['Brake Bias']}")

        # Assemble and enforce token cap
        combined = "\n\n".join(selected_parts).strip()
        if len(combined) > max_chars:
            combined = combined[:max_chars].rsplit(" ", 1)[0] + "..."

        return combined
