"""Step E - SEO, ads and shell injection.

Wraps a passing tool fragment into templates/tool_shell.html: canonical URLs,
OpenGraph tags, breadcrumb, JSON-LD structured data, usage instructions,
related-tool links and the user's configured ad slots (inserted VERBATIM from
config/site.json - never generated or altered by the LLM).
"""
from __future__ import annotations

import json
import re

from common import (
    TEMPLATES_DIR,
    TOOLS_DIR,
    category_map,
    config,
    domain,
    html_escape,
    registry_tools,
    save_json,
    truncate_chars,
    utc_today,
)

APP_CATEGORY = {
    "finance": "FinanceApplication",
    "health": "HealthApplication",
    "game": "GameApplication",
}


def inject_shell(slug: str, tool_html: str, meta: dict) -> str:
    cfg = config()
    shell = (TEMPLATES_DIR / "tool_shell.html").read_text(encoding="utf-8")
    dom = domain()
    cats = category_map()
    cat_slug = meta["category"]
    cat_name = cats.get(cat_slug, {}).get("name", cat_slug.replace("-", " ").title())
    url = f"{dom}/tools/{slug}/"

    jsonld = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": meta["title"],
        "url": url,
        "description": meta["seo_description"],
        "applicationCategory": APP_CATEGORY.get(cat_slug, "UtilitiesApplication"),
        "operatingSystem": "Any",
        "browserRequirements": "Requires JavaScript",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "datePublished": utc_today(),
        "publisher": {"@type": "Organization", "name": cfg["site_name"]},
    }
    jsonld_str = json.dumps(jsonld, ensure_ascii=False).replace("</", "<\\/")

    ads = cfg["ad_slots"]
    values = {
        "SEO_TITLE": html_escape(meta["seo_title"]),
        "SEO_DESCRIPTION": html_escape(meta["seo_description"]),
        "CANONICAL_URL": html_escape(url),
        "H1": html_escape(meta["h1"]),
        "TITLE": html_escape(meta["title"]),
        "CATEGORY_NAME": html_escape(cat_name),
        "CATEGORY_URL": html_escape(f"{dom}/category/{cat_slug}/"),
        "HOMEPAGE_URL": html_escape(f"{dom}/"),
        "SITE_NAME": html_escape(cfg["site_name"]),
        "DOMAIN": html_escape(dom),
        "YEAR": utc_today()[:4],
        "USAGE_INSTRUCTIONS": html_escape(
            meta.get("usage_instructions") or "Use the interactive tool above."
        ),
        "TOOL_HTML": tool_html,
        "JSONLD": jsonld_str,
        "RELATED_LINKS": _related_links(slug, cat_slug),
        "AD_TOP_DESKTOP": ads["leaderboard_728x90"],
        "AD_TOP_MOBILE": ads["rectangle_300x250"],
        "AD_NATIVE": ads["native_banner"],
        "AD_FOOTER": ads["banner_468x60"],
        "ANALYTICS": cfg.get("analytics_snippet", ""),
    }

    page = shell
    for key, val in values.items():
        page = page.replace("{{" + key + "}}", val)

    # ---- idempotency / injection sanity checks -----------------------------
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", page)
    if leftover:
        raise RuntimeError(f"unreplaced template tokens: {leftover[:5]}")
    ad_network_count = page.count("versatilesentiment.com")
    if ad_network_count != 4:
        raise RuntimeError(
            f"ad injection sanity failed: expected 4 ad snippets, found {ad_network_count}"
        )
    if page.count('id="tool-root"') != 1:
        raise RuntimeError("tool-root not injected exactly once")

    out_path = TOOLS_DIR / slug / "index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return str(out_path)


def _related_links(slug: str, category: str, limit: int = 4) -> str:
    dom = domain()
    others = [t for t in registry_tools() if t.get("slug") != slug and t.get("status") == "published"]
    same = [t for t in others if t.get("category") == category]
    recent = sorted(others, key=lambda t: t.get("date_published", ""), reverse=True)
    picked, seen = [], set()
    for t in same + recent:
        if t["slug"] in seen:
            continue
        seen.add(t["slug"])
        picked.append(t)
        if len(picked) >= limit:
            break
    if not picked:
        return ('<li><a href="' + html_escape(dom) + '/">Browse all tools</a></li>')
    items = [
        f'<li><a href="{html_escape(dom)}/tools/{t["slug"]}/">{html_escape(t["title"])}</a></li>'
        for t in picked
    ]
    return "\n      ".join(items)


if __name__ == "__main__":
    import sys
    payload = json.loads(sys.argv[1])
    print(inject_shell(payload["slug"], payload["html"], payload["meta"]))
