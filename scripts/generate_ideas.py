"""Step A - Idea generation (cheap/fast model).

Proposes single-purpose web tool ideas, grounded in the existing registry so
the model does not reinvent what already exists. Output is logged to the daily
log immediately for auditability.
"""
from __future__ import annotations

import json
import sys

from common import (
    allowed_category_slugs,
    config,
    groq_chat,
    log_append,
    parse_llm_json,
    registry_tools,
    slugify,
)

PROMPT_TEMPLATE = """You are proposing small, genuinely useful, single-purpose web tools for a utility website.

Rules for a good idea:
- Solvable entirely with client-side HTML/CSS/JavaScript (no server, no paid API calls at runtime).
- Narrow and single-purpose (one job, done well) - not a suite.
- Something people actually search for (calculators, converters, formatters, generators, checkers).
- Evergreen demand: prefer tools that will still be searched for in five years.
- Must NOT duplicate or closely overlap with any tool in the existing list below.

Existing tools (do not repeat or closely overlap with these):
{existing}

Categories to draw from: {categories}

Return a JSON object with exactly {n} candidate ideas, in exactly this shape:
{{"ideas": [{{"title": "string, e.g. 'Percentage Increase Calculator'", "slug": "kebab-case-unique-slug", "category": "one of the allowed categories", "one_line_pitch": "what it does, under 20 words", "inputs": ["short list of expected user inputs"], "output": "what the tool produces"}}]}}

Output ONLY the JSON object. No prose, no markdown fences."""


def _existing_context() -> str:
    tools = registry_tools()
    if not tools:
        return "(none yet - the site is brand new, so any genuinely useful idea is fair game)"
    trimmed = [
        {
            "title": t.get("title", ""),
            "category": t.get("category", ""),
            "description": t.get("seo_description", ""),
        }
        for t in tools[-200:]
    ]
    return json.dumps(trimmed, indent=1, ensure_ascii=False)


def propose_ideas(n: int | None = None) -> list:
    cfg = config()["llm"]
    n = n or cfg["max_ideas_per_day"]
    prompt = PROMPT_TEMPLATE.format(
        existing=_existing_context(),
        categories=", ".join(allowed_category_slugs()),
        n=n,
    )

    content = groq_chat(
        cfg["idea_model"],
        [{"role": "user", "content": prompt}],
        max_tokens=cfg.get("max_output_tokens_ideas", 2000),
        temperature=0.85,
        json_mode=True,
        purpose="idea_generation",
    )
    data = parse_llm_json(content)
    raw = data.get("ideas", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError("LLM did not return an idea list")

    valid_slugs = set(allowed_category_slugs())
    ideas, seen_slugs = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        slug = slugify(item.get("slug") or title)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        category = (item.get("category") or "").strip().lower()
        if category not in valid_slugs:
            category = "random-fun" if "random-fun" in valid_slugs else valid_slugs[0]
        inputs = item.get("inputs") or []
        if not isinstance(inputs, list):
            inputs = [str(inputs)]
        ideas.append({
            "title": title,
            "slug": slug,
            "category": category,
            "one_line_pitch": (item.get("one_line_pitch") or "").strip(),
            "inputs": [str(x) for x in inputs][:8],
            "output": (item.get("output") or "").strip(),
        })
        if len(ideas) >= n:
            break

    log_append("ideas_proposed", ideas)
    return ideas


if __name__ == "__main__":
    result = propose_ideas()
    print(json.dumps(result, indent=2, ensure_ascii=False))
