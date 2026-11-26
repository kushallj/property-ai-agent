"""Per-lead memory: one plain Markdown file of notes per customer.

Idea borrowed from OpenMausBot's bot memory (docs/memory.md there): notes live in
ordinary files a person can open, edit or delete, and the agent reads them before
every reply. Here the notes are about the buyer, not the bot:

    data/memory/wa-919800004521.md
    # Notes about this customer
    - 2026-11-26 · Wife works in Hinjewadi (agent)
    - 2026-11-26 · Wants east-facing, not ground floor (agent)
    - 2026-11-27 · Prefers calls after 7 pm (broker)

Requirement details (city, BHK, budget) are not repeated here; they live in state
and the leads table. Memory is for the personal details a good broker remembers.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from src.util.india import now_ist

BACKEND_DIR = Path(__file__).resolve().parents[2]
MAX_NOTES = 40  # oldest notes drop out of the prompt beyond this; the file keeps everything
_LINE = re.compile(r"^- (?:(\d{4}-\d{2}-\d{2}) · )?(.*?)(?: \((agent|broker)\))?\s*$")


def memory_dir() -> Path:
    path = Path(os.getenv("MEMORY_DIR") or BACKEND_DIR / "data" / "memory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file(thread_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id)[:80] or "unknown"
    return memory_dir() / f"{safe}.md"


def read_notes(thread_id: str) -> list[dict]:
    path = _file(thread_id)
    if not path.exists():
        return []
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line.strip())
        if m and m.group(2):
            notes.append({"date": m.group(1), "text": m.group(2).strip(), "by": m.group(3) or "broker"})
    return notes


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def add_notes(thread_id: str, notes: list[str], by: str = "agent") -> list[str]:
    """Append notes that aren't already there. Returns the ones actually added."""
    existing = {_norm(n["text"]) for n in read_notes(thread_id)}
    added = []
    for note in notes:
        note = " ".join(str(note).split())[:200]
        if note and _norm(note) not in existing:
            existing.add(_norm(note))
            added.append(note)
    if added:
        path = _file(thread_id)
        today = now_ist().strftime("%Y-%m-%d")
        header = "" if path.exists() else "# Notes about this customer\n\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(header + "".join(f"- {today} · {n} ({by})\n" for n in added))
    return added


def as_context(thread_id: str) -> str:
    notes = read_notes(thread_id)[-MAX_NOTES:]
    return "\n".join(f"  - {n['text']}" for n in notes)
