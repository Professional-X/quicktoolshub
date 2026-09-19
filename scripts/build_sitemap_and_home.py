"""Step F (part 2) - Regenerate sitemap.xml, robots.txt, homepage and
category index pages from the registry.

Called after every published tool (cheap, idempotent) so the site is always
consistent with the registry, even if a run is interrupted.
"""
from __future__ import annotations

import json

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import (
    ROOT,
    TEMPLATES_DIR,
    category_map,
    categories,
    config,
    domain,
    registry_tools,
    save_json,
    utc_today,
)

_env = None


def jinja_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env


def _tool_view(t: dict) -> dict:
    dom = domain()
    desc = t.get("seo_description") or t.get("h1") or t.get("title", "")
    search_text = " ".join(
        [t.get("title", ""), desc, t.get("category", ""), " ".join(t.get("keywords", []) or [])]
    ).lower()
    search_text = " ".join(search_text.split())  # collapse whitespace
    return {
        "slug": t["slug"],
        "title": t.get("title", t["slug"]),
        "description": desc,
        "url": f"{dom}/tools/{t['slug']}/",
        "category": t.get("category", ""),
        "date": t.get("date_published", ""),
        "search": search_text.replace('"', "'"),
    }


def rebuild() -> None:
    cfg = config()
    dom = domain()
    cats = category_map()
    published = sorted(
        [t for t in registry_tools() if t.get("status") == "published"],
        key=lambda t: (t.get("date_published", ""), t.get("title", "")),
        reverse=True,
    )
    tools_views = [_tool_view(t) for t in published]

    cat_views, with_tools, empty = [], [], []
    for c in categories():
        cat_tools = [v for v in tools_views if v["category"] == c["slug"]]
        view = {
            "slug": c["slug"],
            "name": c.get("name", c["slug"]),
            "description": c.get("description", ""),
            "tools": cat_tools,
            "count": len(cat_tools),
        }
        cat_views.append(view)
        (with_tools if cat_tools else empty).append(view)

    env = jinja_env()

    # ---------------- homepage ----------------
    homepage_tpl = env.get_template("homepage.html.j2")
    jsonld = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": cfg["site_name"],
        "url": f"{dom}/",
        "description": cfg.get("tagline", ""),
    }
    html = homepage_tpl.render(
        site={
            "name": cfg["site_name"],
            "tagline": cfg.get("tagline", ""),
            "domain": dom,
            "base_path": "",
            "meta_description": f"{cfg['site_name']}: {cfg.get('tagline', 'Free online tools that run in your browser.')}"[:155],
        },
        categories_with_tools=with_tools,
        empty_categories=empty,
        total_tools=len(tools_views),
        year=utc_today()[:4],
        jsonld=json.dumps(jsonld, ensure_ascii=False).replace("</", "<\\/"),
        ad_top_desktop=cfg["ad_slots"]["leaderboard_728x90"],
        ad_top_mobile=cfg["ad_slots"]["rectangle_300x250"],
        ad_native=cfg["ad_slots"]["native_banner"],
        ad_footer=cfg["ad_slots"]["banner_468x60"],
        analytics=cfg.get("analytics_snippet", ""),
    )
    (ROOT / "index.html").write_text(html, encoding="utf-8")

    # ---------------- category pages ----------------
    cat_tpl = env.get_template("category.html.j2")
    for view in cat_views:
        html = cat_tpl.render(
            site={
                "name": cfg["site_name"],
                "domain": dom,
                "base_path": "",
            },
            c=view,
            year=utc_today()[:4],
            ad_top_desktop=cfg["ad_slots"]["leaderboard_728x90"],
            ad_top_mobile=cfg["ad_slots"]["rectangle_300x250"],
            ad_footer=cfg["ad_slots"]["banner_468x60"],
            analytics=cfg.get("analytics_snippet", ""),
        )
        out = ROOT / "category" / view["slug"] / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")

    # ---------------- sitemap ----------------
    urls = [(f"{dom}/", utc_today(), "1.0")]
    for view in cat_views:
        urls.append((f"{dom}/category/{view['slug']}/", utc_today(), "0.6"))
    for t in published:
        urls.append((f"{dom}/tools/{t['slug']}/", t.get("date_published", utc_today()), "0.8"))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod, priority in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{loc}</loc>")
        lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append("    <changefreq>weekly</changefreq>")
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    (ROOT / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------------- robots.txt ----------------
    (ROOT / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n\n"
        f"Sitemap: {dom}/sitemap.xml\n",
        encoding="utf-8",
    )

    print(f"[build] homepage + {len(cat_views)} category pages + sitemap "
          f"({len(tools_views)} tools) rebuilt")


if __name__ == "__main__":
    rebuild()
