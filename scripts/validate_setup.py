"""Pre-flight validation for the daily run.

Verifies the LLM_API_KEY works against the configured provider before the
pipeline spends anything. Writes a human-readable diagnosis to the GitHub
step summary when running in Actions, and exits non-zero on fatal problems.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import config, groq_chat, registry_tools, utc_today  # noqa: E402


def main() -> int:
    cfg = config()["llm"]
    lines = ["## Daily Tool Factory - pre-flight check", ""]
    ok = True

    if not os.environ.get("LLM_API_KEY"):
        print("FATAL: LLM_API_KEY secret is not set")
        return 1

    # cheap models endpoint style check via tiny completion
    try:
        reply = groq_chat(
            cfg["idea_model"],
            [{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=5,
            temperature=0.0,
            purpose="preflight",
        )
        lines.append(f"- Idea model `{cfg['idea_model']}`: reachable (reply: {reply.strip()[:20]!r})")
    except Exception as e:
        ok = False
        lines.append(f"- Idea model `{cfg['idea_model']}`: FAILED - {e}")

    try:
        reply = groq_chat(
            cfg["code_model"],
            [{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=5,
            temperature=0.0,
            purpose="preflight",
        )
        lines.append(f"- Code model `{cfg['code_model']}`: reachable (reply: {reply.strip()[:20]!r})")
    except Exception as e:
        ok = False
        lines.append(f"- Code model `{cfg['code_model']}`: FAILED - {e}")

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
