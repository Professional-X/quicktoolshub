"""Step B - Deduplication (runs before any expensive code generation).

Layer 1 (cheap/local): slug match, normalized title match and token-overlap
similarity against every registry entry and against other ideas accepted in
this batch.

Layer 2 (cheap LLM, only for ambiguous zone scores): asks the fast model
whether the candidate is genuinely a different tool or a near-duplicate.
"""
from __future__ import annotations

import json

from common import (
    config,
    content_tokens,
    groq_chat,
    log_append,
    normalize_text,
    parse_llm_json,
    registry_tools,
    resolve_model,
    title_similarity,
    token_overlap,
    utc_now_iso,
)


def _similarity_vs_registry(idea: dict) -> tuple[float, dict | None]:
    cand_tokens = content_tokens(
        f'{idea["title"]} {idea.get("one_line_pitch", "")} '
        f'{" ".join(idea.get("inputs", []))} {idea.get("output", "")}'
    )
    cand_title = normalize_text(idea["title"])
    best_score, best_tool = 0.0, None
    for tool in registry_tools():
        ref_tokens = content_tokens(
            f'{tool.get("title", "")} {tool.get("seo_description", "")} '
            f'{" ".join(tool.get("keywords", []) or [])}'
        )
        score = max(
            token_overlap(cand_tokens, ref_tokens),
            title_similarity(idea["title"], tool.get("title", "")),
        )
        if score > best_score:
            best_score, best_tool = score, tool
    return best_score, best_tool


def _similarity_vs_idea(idea: dict, other: dict) -> float:
    a = content_tokens(
        f'{idea["title"]} {idea.get("one_line_pitch", "")} {" ".join(idea.get("inputs", []))}'
    )
    b = content_tokens(
        f'{other["title"]} {other.get("one_line_pitch", "")} {" ".join(other.get("inputs", []))}'
    )
    return max(token_overlap(a, b), title_similarity(idea["title"], other["title"]))


def _llm_semantic_check(idea: dict, best_tool: dict | None) -> bool:
    """Returns True if the idea is a duplicate (rejected)."""
    cfg = config()
    llm_checks_used = sum(
        1 for c in _today_llm_calls() if c.get("purpose") == "dedup_check"
    )
    if llm_checks_used >= cfg["dedup"].get("max_llm_checks_per_day", 2):
        return False  # budget for ambiguity checks exhausted -> give benefit of doubt

    neighbors = []
    tools = registry_tools()
    if best_tool:
        neighbors.append(best_tool)
    for t in tools:
        if t not in neighbors:
            neighbors.append(t)
        if len(neighbors) >= 6:
            break
    payload = {
        "candidate": {
            "title": idea["title"],
            "pitch": idea.get("one_line_pitch", ""),
            "inputs": idea.get("inputs", []),
            "output": idea.get("output", ""),
        },
        "existing_tools": [
            {"slug": t["slug"], "title": t.get("title", ""),
             "description": t.get("seo_description", "")}
            for t in neighbors
        ],
    }
    prompt = (
        "You are a strict deduplication judge for a utility website. "
        "Does the candidate tool duplicate or closely overlap (same user job) any existing tool?\n"
        f"Data: {json.dumps(payload, ensure_ascii=False)}\n"
        'Answer ONLY JSON: {"duplicate": true|false, "matching_slug": "slug-or-null"}'
    )
    try:
        content = groq_chat(
            resolve_model("idea"),
            [{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.0,
            json_mode=True,
            purpose="dedup_check",
        )
        result = parse_llm_json(content)
        return bool(result.get("duplicate"))
    except Exception:
        return False  # judge unavailable -> benefit of the doubt


def _today_llm_calls() -> list:
    from common import load_daily_log
    return load_daily_log().get("llm_calls", [])


def dedup_ideas(ideas: list) -> list:
    cfg = config()["dedup"]
    reject_at = cfg.get("reject_threshold", 0.8)
    ambiguous_at = cfg.get("ambiguous_zone", 0.6)

    survivors, rejected = [], []
    for idea in ideas:
        reason = None

        # 1) exact slug match
        for tool in registry_tools():
            if idea["slug"] == tool["slug"]:
                reason = f"duplicate of {tool['slug']} (slug match)"
                break

        # 2) similarity vs registry
        if not reason:
            score, best_tool = _similarity_vs_registry(idea)
            if score > reject_at and best_tool:
                reason = (f"duplicate of {best_tool['slug']} "
                          f"(similarity {score:.2f} > {reject_at})")
            elif score > ambiguous_at and best_tool and _llm_semantic_check(idea, best_tool):
                reason = f"duplicate of {best_tool['slug']} (semantic check)"

        # 3) similarity vs ideas accepted earlier in this batch
        if not reason:
            for other in survivors:
                if _similarity_vs_idea(idea, other) > reject_at:
                    reason = f"duplicate of same-batch idea '{other['title']}'"
                    break

        if reason:
            entry = {"slug": idea["slug"], "title": idea["title"],
                     "reason": f"rejected: {reason}", "at": utc_now_iso()}
            rejected.append(entry)
            print(f"  [dedup] REJECT {idea['slug']}: {reason}")
        else:
            survivors.append(idea)
            print(f"  [dedup] PASS  {idea['slug']}")

    log_append("ideas_rejected", rejected)
    return survivors


if __name__ == "__main__":
    import sys
    from common import load_json, pathlib
    ideas = json.loads(sys.argv[1]) if len(sys.argv) > 1 else []
    print(json.dumps(dedup_ideas(ideas), indent=2, ensure_ascii=False))
