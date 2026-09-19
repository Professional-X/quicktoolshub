"""Step F (part 1) - Registry updates.

Appends/upserts published tools in registry.json (the master ledger). Upsering
by slug keeps re-runs idempotent - a tool can never be registered twice.
"""
from __future__ import annotations

from common import registry, save_registry, utc_today


def register_published(slug: str, meta: dict) -> dict:
    reg = registry()
    entry = {
        "slug": slug,
        "title": meta["title"],
        "category": meta["category"],
        "date_published": utc_today(),
        "status": "published",
        "h1": meta.get("h1", meta["title"]),
        "seo_description": meta.get("seo_description", ""),
        "keywords": meta.get("keywords", []),
        "usage_instructions": meta.get("usage_instructions", ""),
    }
    for i, existing in enumerate(reg["tools"]):
        if existing.get("slug") == slug:
            reg["tools"][i] = {**existing, **entry}  # idempotent upsert
            save_registry(reg)
            return reg["tools"][i]
    reg["tools"].append(entry)
    save_registry(reg)
    return entry


if __name__ == "__main__":
    import sys
    meta = {"title": sys.argv[2], "category": sys.argv[3],
            "h1": sys.argv[2], "seo_description": "", "keywords": [],
            "usage_instructions": ""}
    print(register_published(sys.argv[1], meta))
