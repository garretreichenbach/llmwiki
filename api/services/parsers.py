"""Parser helpers shared by hosted/local service implementations."""

from __future__ import annotations

import re

import yaml

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.+?\n)---[ \t]*\n", re.DOTALL)


def parse_frontmatter(content: str) -> dict:
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    try:
        meta = yaml.safe_load(m.group(1))
    except Exception:
        return {}
    return meta if isinstance(meta, dict) else {}


def title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem.replace("-", " ").replace("_", " ").strip().title()


def extract_tags(meta: dict) -> list[str]:
    tags = meta.get("tags", [])
    if isinstance(tags, list):
        return [str(t) for t in tags if t is not None]
    return []
