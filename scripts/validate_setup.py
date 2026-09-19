"""Pre-flight validation for the daily run.

Verifies the LLM_API_KEY works against the configured provider before the
pipeline spends anything, resolves which models this account can actually
use (models get deprecated over time), and writes a human-readable diagnosis
to the GitHub step summary. Exits non-zero on fatal problems.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    config,
    groq_chat,
    list_models,
    registry_tools,
    resolve_model,
    utc_today,
)


def main() -> int:
    cfg = config()["llm"]
    lines = ["## Daily Tool Factory - pre-flight check", ""]
    ok = True

    if not os.environ.get("LLM_API_KEY"):
        print("FATAL: LLM_API_KEY secret is not set")
        return 1

    # ---- model discovery ---------------------------------------------------
    try:
        available = list_models()
        lines.append(f"- Account models available: {len(available)}")
        lines.append("  ```")
        for m in available[:25]:
            lines.append(f"  {m}")
        lines.append("  ```")
        idea_model = resolve_model("idea")
        code_model = resolve_model("code")
        lines.append(f"- Resolved idea model: `{idea_model}`")
        lines.append(f"- Resolved code model: `{code_model}`")
    except Exception as e:
        print("\n".join(lines))
        print(f"FATAL: model discovery failed - {e}")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"\n**FATAL (model discovery):** {e}\n")
        return 1

    # ---- live completion smoke tests ---------------------------------------
    try:
        reply = groq_chat(idea_model,
                          [{"role": "user", "content": "Reply with exactly: OK"}],
                          max_tokens=10, temperature=0.0, purpose="preflight")
        lines.append(f"- Idea model smoke test: OK ({reply.strip()[:20]!r})")
    except Exception as e:
        ok = False
        lines.append(f"- Idea model smoke test: FAILED - {e}")

    try:
        reply = groq_chat(code_model,
                          [{"role": "user", "content": "Reply with exactly: OK"}],
                          max_tokens=10, temperature=0.0, purpose="preflight")
        lines.append(f"- Code model smoke test: OK ({reply.strip()[:20]!r})")
    except Exception as e:
        ok = False
        lines.append(f"- Code model smoke test: FAILED - {e}")

    reg = registry_tools()
    lines += [
        f"- Registry: {len(reg)} published tool(s)",
        f"- Publish cap today: {cfg['max_tools_published_per_day']} "
        f"(already published today: "
        f"{sum(1 for t in reg if t.get('date_published') == utc_today())})",
        "",
    ]

    report = "\n".join(lines)
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(report + "\n")
        except Exception:
            pass

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
