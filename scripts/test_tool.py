"""Step D - Automated testing (Playwright, headless Chromium).

Loads the generated tool fragment inside a minimal harness page and runs the
quality gate:

  1. Load check: zero uncaught JS errors / console errors.
  2. Render check: #tool-root exists, is visible and non-empty.
  3. Functional check: every META test_case is simulated; the expected output
     must appear (fuzzy substring match) in #tool-result / #tool-root.
  4. Accessibility sanity: every control has an associated label.
  5. Responsiveness: at 360px viewport there is no horizontal overflow.
  6. Forbidden content scan: no external scripts beyond the allowlist,
     no self-inserted ads/tracking.

Any failure = reject. The tool is discarded, not retried same-day.
"""
from __future__ import annotations

import re

from common import TEST_TMP, config  # noqa: F401

HARNESS_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>harness</title>
<style>body{margin:0;font-family:system-ui,sans-serif;line-height:1.5;color:#1f2937}.container{max-width:960px;margin:0 auto;padding:16px}</style>
</head><body><div class="container">__TOOL_HTML__</div></body></html>"""


def test_tool(slug: str, tool_html: str, meta: dict) -> tuple[bool, list]:
    """Returns (passed, check_details)."""
    from playwright.sync_api import sync_playwright

    checks: list = []
    harness_path = TEST_TMP / f"{slug}.html"
    harness_path.parent.mkdir(parents=True, exist_ok=True)
    harness_path.write_text(
        HARNESS_TEMPLATE.replace("__TOOL_HTML__", tool_html), encoding="utf-8"
    )

    forbidden = _scan_forbidden(tool_html)
    checks.append({"check": "forbidden_content", "passed": not forbidden,
                   "detail": forbidden or "clean"})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on("console", lambda m: errors.append(f"console: {m.text}")
                        if m.type == "error" and "Failed to load resource" not in m.text else None)
                page.route("**/*", lambda route: route.continue_()
                           if route.request.url.startswith("file://") else route.abort())

                page.goto(harness_path.as_uri())
                page.wait_for_timeout(700)

                # 1) load check
                checks.append({"check": "zero_js_errors", "passed": not errors,
                               "detail": errors[:5] or "no errors"})

                # 2) render check
                root = page.locator("#tool-root")
                root_ok = False
                detail = "#tool-root missing"
                if root.count():
                    try:
                        visible = root.is_visible()
                        text = (root.inner_text() or "").strip()
                        box = root.bounding_box()
                        root_ok = visible and len(text) > 0 and box and box["width"] > 50
                        detail = f"visible={visible} text_len={len(text)}"
                    except Exception as e:
                        detail = f"evaluate failed: {e}"
                checks.append({"check": "render", "passed": root_ok, "detail": detail})

                # 3) functional check (each test case on a fresh reload)
                func_ok, func_detail = _run_test_cases(page, harness_path, meta, errors)
                checks.append({"check": "test_cases", "passed": func_ok, "detail": func_detail})

                # 4) label audit
                label_issues = _label_audit(page)
                checks.append({"check": "labels", "passed": not label_issues,
                               "detail": label_issues or "all controls labelled"})

                # 5) responsiveness at 360px
                resp_ok, resp_detail = _responsiveness_check(browser, harness_path)
                checks.append({"check": "responsive_360", "passed": resp_ok,
                               "detail": resp_detail})
            finally:
                browser.close()
    except Exception as e:
        checks.append({"check": "playwright_run", "passed": False,
                       "detail": f"{type(e).__name__}: {e}"})

    passed = all(c["passed"] for c in checks)
    return passed, checks


# ------------------------------------------------------------------ pieces --

def _scan_forbidden(tool_html: str) -> list:
    problems = []
    low = tool_html.lower()
    for m in re.finditer(r"<script[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", tool_html, re.I):
        src = m.group(1).lower()
        allowed = config().get("allowed_cdn_hosts", [])
        if not any(host in src for host in allowed):
            problems.append(f"external script: {src[:120]}")
    for snippet in ("atoptions", "versatilesentiment", "adsbygoogle",
                    "googlesyndication", "gtag(", "dataLayer", "plausible.io"):
        if snippet in low:
            problems.append(f"forbidden snippet: {snippet}")
    if re.search(r"\bfetch\s*\(|\bXMLHttpRequest\b|navigator\.sendBeacon", low):
        problems.append("network call (fetch/XHR/beacon)")
    return problems


def _find_field(page, key: str):
    """Locate an input by id (exact), then by name, then by loose id."""
    for selector in (f"#{key}", f"[name=\"{key}\"]",
                     f"[id$=\"{key}\"]"):
        loc = page.locator(selector).first
        if loc.count():
            return loc
    return None


def _fill_field(page, loc, value: str) -> str | None:
    """Fill one control. Returns error string or None on success."""
    try:
        tag = loc.evaluate("e => e.tagName.toLowerCase()")
        ftype = (loc.evaluate("e => (e.type || '').toLowerCase()") or "").lower()
    except Exception as e:
        return f"cannot inspect element: {e}"

    try:
        if tag == "select":
            try:
                loc.select_option(value)
            except Exception:
                loc.select_option(label=value)
            loc.dispatch_event("change")
        elif ftype == "checkbox":
            if str(value).lower() in ("true", "1", "yes", "on", "checked"):
                loc.check()
            else:
                loc.uncheck()
        elif ftype == "radio":
            loc.check()
        elif ftype == "file":
            return "file inputs are not supported in automated tests"
        else:
            loc.fill(str(value))
            loc.dispatch_event("input")
            loc.dispatch_event("change")
        return None
    except Exception as e:
        return f"fill failed: {str(e)[:160]}"


def _result_text(page) -> str:
    for sel in ("#tool-result", "#tool-root"):
        loc = page.locator(sel)
        if loc.count():
            try:
                return loc.inner_text() or ""
            except Exception:
                continue
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _matches(expected: str, actual: str) -> bool:
    e, a = _norm(expected), _norm(actual)
    if not e:
        return True
    if e in a:
        return True
    e2 = re.sub(r"[^a-z0-9.%/-]", "", expected.lower())
    a2 = re.sub(r"[^a-z0-9.%/-]", "", actual.lower())
    return bool(e2) and e2 in a2


def _run_test_cases(page, harness_path, meta: dict, errors: list) -> tuple[bool, str]:
    cases = meta.get("test_cases") or []
    if not cases:
        return False, "no test cases supplied"
    results = []
    all_ok = True

    for idx, tc in enumerate(cases):
        errors.clear()
        page.goto(harness_path.as_uri())
        page.wait_for_timeout(400)

        problems = []
        for key, value in (tc.get("input") or {}).items():
            loc = _find_field(page, key)
            if not loc:
                problems.append(f"input '{key}' not found in DOM")
                continue
            err = _fill_field(page, loc, value)
            if err:
                problems.append(f"{key}: {err}")

        action = page.locator("#tool-action")
        if action.count():
            action.first.click()
        else:
            for key in (tc.get("input") or {}):
                loc = _find_field(page, key)
                if loc:
                    try:
                        loc.dispatch_event("input")
                        loc.dispatch_event("change")
                    except Exception:
                        pass
        page.wait_for_timeout(600)

        actual = _result_text(page)
        expected = str(tc.get("expected_output", ""))
        matched = _matches(expected, actual)
        if not matched:
            problems.append(
                f"expected {expected[:80]!r} not found; actual: {_norm(actual)[:160]!r}"
            )
        if errors:
            problems.append(f"js errors: {errors[:3]}")

        ok = not problems
        all_ok = all_ok and ok
        results.append(f"case{idx + 1}: {'PASS' if ok else 'FAIL'}"
                       + ("" if ok else f" ({'; '.join(problems[:3])})"))

    return all_ok, " | ".join(results)


def _label_audit(page) -> list:
    try:
        return page.evaluate(
            """() => {
              const issues = [];
              const controls = document.querySelectorAll(
                '#tool-root input:not([type=hidden]):not([type=submit]):not([type=button]), ' +
                '#tool-root select, #tool-root textarea');
              controls.forEach(el => {
                if (!el.id) { issues.push('control without id'); return; }
                const lbl = document.querySelector('label[for="' + el.id + '"]')
                            || el.closest('label');
                if (!lbl) issues.push('control without label: ' + el.id);
              });
              return issues.slice(0, 6);
            }"""
        )
    except Exception as e:
        return [f"label audit failed: {e}"]


def _responsiveness_check(browser, harness_path) -> tuple[bool, str]:
    ctx = browser.new_context(viewport={"width": 360, "height": 740})
    page = ctx.new_page()
    try:
        page.goto(harness_path.as_uri())
        page.wait_for_timeout(500)
        metrics = page.evaluate(
            """() => {
              const docW = document.documentElement.scrollWidth;
              const offenders = [];
              document.querySelectorAll('#tool-root, #tool-root *').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && (r.right > 362 || r.left < -2)) {
                  offenders.push(el.tagName.toLowerCase()
                    + (el.id ? '#' + el.id : '')
                    + ' right=' + Math.round(r.right));
                }
              });
              return {docW, offenders: offenders.slice(0, 4)};
            }"""
        )
        ok = metrics["docW"] <= 362 and not metrics["offenders"]
        detail = f"scrollWidth={metrics['docW']}"
        if metrics["offenders"]:
            detail += f"; offenders: {metrics['offenders']}"
        return ok, detail
    except Exception as e:
        return False, f"responsive check failed: {e}"
    finally:
        ctx.close()


if __name__ == "__main__":
    import json
    import sys
    payload = json.loads(sys.argv[1])
    ok, detail = test_tool(payload["slug"], payload["html"], payload["meta"])
    print(json.dumps({"passed": ok, "checks": detail}, indent=2))
