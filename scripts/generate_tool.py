"""Step C - Tool code generation (stronger model, one call per surviving idea).

The LLM produces ONLY the tool fragment (<div id="tool-root"> + <style> +
<script>) plus a META block with SEO data and its own acceptance test cases.
Hard validation happens here; anything suspicious is rejected before testing.
"""
from __future__ import annotations

import re

from common import (
    config,
    groq_chat,
    resolve_model,
    slugify,
    truncate_chars,
)

PROMPT_TEMPLATE = """Build a single, self-contained HTML file fragment for this utility tool. This fragment will be inserted into a template that already provides the page header, footer, navigation, and ad slots - so you must ONLY produce the tool's own content and functionality.

TOOL SPEC:
Title: {title}
Category: {category}
Pitch: {one_line_pitch}
Expected inputs: {inputs}
Expected output: {output}

HARD REQUIREMENTS:
1. Output must be a single <div id="tool-root">...</div> block containing all HTML, plus a <script> block with all JS, plus a <style> block scoped to #tool-root (do not touch global styles, body, header, footer, or any element outside #tool-root).
2. All logic must run client-side in vanilla JavaScript. No external libraries, no <script src> of any kind, no fetch/XHR to external services, no web fonts.
3. Mobile-first responsive layout: usable on a 360px-wide screen without horizontal scroll. Inputs must not exceed 100% width.
4. Accessible: every input has a proper <label> tied with for="..." and matching id, sufficient color contrast, keyboard-operable controls.
5. Include clear, brief usage instructions visible on the page (2-3 sentences, not a wall of text).
6. Handle empty/invalid input gracefully - never throw an uncaught JS error; show a friendly inline message inside #tool-result instead.
7. No placeholder/fake functionality - the tool must actually perform the stated task correctly. Double-check formulas, rounding and edge cases. Wrap the core logic in try/catch and print friendly error messages.
8. No ads, no tracking scripts, no <html>/<head>/<body> tags - those are handled by the wrapper.
9. JavaScript must be plain ES2018-compatible (no imports, no top-level await).

STRUCTURAL CONTRACT (an automated tester depends on this - follow exactly):
- The root element must be exactly: <div id="tool-root">.
- Every user input element must have an id of the form f-<short-lowercase-name> (e.g. f-number, f-rate, f-start-date). The ids must match the expected inputs listed above.
- The main action button (if the tool needs one) must be: <button type="button" id="tool-action">...</button>.
- All result text must be rendered inside an element with id="tool-result" (create it always, even before the first calculation; it may be empty or contain a hint).
- If the tool computes live while typing (no button needed), still update #tool-result on every input event.

ALSO OUTPUT, as a separate JSON object after the HTML, delimited exactly as shown:

---META---
{{
  "seo_title": "under 60 chars, includes primary keyword",
  "seo_description": "under 155 chars, action-oriented",
  "h1": "on-page H1, can differ slightly from seo_title",
  "keywords": ["3-6 relevant search terms people use"],
  "usage_instructions": "2-3 sentence plain-text instructions, same as shown on page",
  "test_cases": [
    {{"input": {{"f-<input-id>": "value", "f-<input-id-2>": "value"}}, "expected_output": "short text that must appear in #tool-result, e.g. '25%' or 'Error: enter a number'"}},
    {{"input": {{"f-<input-id>": "edge or invalid value"}}, "expected_output": "expected friendly message or edge-case result"}}
  ]
}}
---END META---

test_cases rules:
- Include 2 to 4 cases: at least one normal case and one edge/invalid-input case.
- Every key in "input" MUST be a full input element id (starting with f-) that exists in your HTML.
- expected_output MUST quote a short verbatim snippet (numbers, units, or message text) of what will literally appear in #tool-result, because a machine checks for it.

Output the HTML block first, then the META block. Nothing else - no explanation, no markdown fences."""


class ToolRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


FORBIDDEN_SNIPPETS = (
    "atoptions", "versatilesentiment", "adsbygoogle", "googlesyndication",
    "googletagmanager", "googleanalytics", "gtag(", "dataLayer", "plausible.io",
    "hotjar", "clicky", "matomo", "histats", "addthis", "sharethis",
)

ALLOWED_CDN_HOSTS = config().get("allowed_cdn_hosts", [])


def generate_tool(idea: dict) -> tuple[str, dict]:
    cfg = config()["llm"]
    prompt = PROMPT_TEMPLATE.format(
        title=idea["title"],
        category=idea["category"],
        one_line_pitch=idea.get("one_line_pitch", ""),
        inputs=", ".join(idea.get("inputs", [])) or "(tool-appropriate defaults)",
        output=idea.get("output", ""),
    )
    content = groq_chat(
        resolve_model("code"),
        [{"role": "user", "content": prompt}],
        max_tokens=cfg.get("max_output_tokens_code", 6000),
        temperature=0.35,
        json_mode=False,
        purpose=f"code_generation:{idea['slug']}",
    )
    html, meta = _split_output(content)
    _validate(idea, html, meta)
    return html, meta


def _split_output(content: str) -> tuple[str, dict]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:html)?\s*", "", text)
    i = text.rfind("---META---")
    j = text.rfind("---END META---")
    if i == -1 or j == -1 or j <= i:
        raise ToolRejected("missing ---META--- block in LLM output")
    html_part = re.sub(r"\s*```$", "", text[:i].strip())
    meta_part = text[i + len("---META---"):j].strip()
    from common import parse_llm_json
    meta = parse_llm_json(meta_part)
    if not isinstance(meta, dict):
        raise ToolRejected("META block is not a JSON object")
    return html_part, meta


def _validate(idea: dict, html: str, meta: dict) -> None:
    low = html.lower()

    if 'id="tool-root"' not in low:
        raise ToolRejected("output missing <div id=\"tool-root\">")
    if re.search(r"<\s*/?\s*(html|head|body)\b", low):
        raise ToolRejected("output contains html/head/body tags")
    if re.search(r"<script[^>]*\bsrc\s*=", low):
        # allow only explicitly allowlisted CDN hosts (default: none allowed)
        for m in re.finditer(r"<script[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", html, re.I):
            src = m.group(1).lower()
            if not any(host in src for host in ALLOWED_CDN_HOSTS):
                raise ToolRejected(f"unauthorized external script: {src[:120]}")
    for snippet in FORBIDDEN_SNIPPETS:
        if snippet in low:
            raise ToolRejected(f"self-inserted ad/tracking content: {snippet!r}")
    if re.search(r"\bfetch\s*\(|\bXMLHttpRequest\b|navigator\.sendBeacon", low):
        raise ToolRejected("output performs network calls (fetch/XHR/beacon)")

    # ---- meta sanity + auto-fixes (log-visible but non-fatal) ----
    meta["slug"] = idea["slug"]
    meta["category"] = idea["category"]
    meta["title"] = idea["title"]
    if not meta.get("seo_title"):
        meta["seo_title"] = idea["title"]
    if not meta.get("h1"):
        meta["h1"] = idea["title"]
    if not meta.get("seo_description"):
        meta["seo_description"] = idea.get("one_line_pitch") or meta["seo_title"]
    if not isinstance(meta.get("keywords"), list) or not meta["keywords"]:
        meta["keywords"] = [w for w in idea["title"].split() if len(w) > 2][:5]
    meta["seo_title"] = truncate_chars(str(meta["seo_title"]), 60)
    meta["seo_description"] = truncate_chars(str(meta["seo_description"]), 155)
    meta["h1"] = str(meta["h1"])[:120]
    meta["keywords"] = [str(k)[:60] for k in meta["keywords"]][:8]
    meta["usage_instructions"] = truncate_chars(str(meta.get("usage_instructions", "")), 600)

    cases = meta.get("test_cases")
    if not isinstance(cases, list) or not cases:
        raise ToolRejected("no test_cases defined in META")

    cleaned_cases = []
    for tc in cases:
        if not isinstance(tc, dict):
            continue
        inputs = tc.get("input")
        expected = str(tc.get("expected_output", "")).strip()
        if not isinstance(inputs, dict) or not expected:
            continue
        # every input key must reference an element that exists in the HTML
        if not all(_element_exists(html, key) for key in inputs):
            continue
        cleaned_cases.append({"input": {str(k): str(v) for k, v in inputs.items()},
                              "expected_output": expected})
    if not cleaned_cases:
        raise ToolRejected("all test_cases were dropped (unknown input ids / empty)")
    meta["test_cases"] = cleaned_cases[:4]


def _element_exists(html: str, key: str) -> bool:
    key = str(key).strip()
    if not key:
        return False
    escaped = re.escape(key)
    return bool(re.search(rf"id\s*=\s*[\"']{escaped}[\"']", html, re.I)) or \
        bool(re.search(rf"name\s*=\s*[\"']{escaped}[\"']", html, re.I))


if __name__ == "__main__":
    import json
    import sys
    idea = json.loads(sys.argv[1])
    html_out, meta_out = generate_tool(idea)
    print(html_out)
    print("---META---")
    import json as _j
    print(_j.dumps(meta_out, indent=2, ensure_ascii=False))
    print("---END META---")
