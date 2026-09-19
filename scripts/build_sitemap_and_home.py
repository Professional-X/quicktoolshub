"""Step F (part 2) - Regenerate sitemap.xml, robots.txt, homepage,
schedule page and category index pages from the registry.

Called after every published tool (cheap, idempotent) so the site is always
consistent with the registry, even if a run is interrupted.
"""
from __future__ import annotations

import datetime
import json

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import (
    ROOT,
    TEMPLATES_DIR,
    TOOLS_DIR,
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


def _schedule_views(cfg: dict) -> list:
    """Configured pipeline run times as display rows (UTC + display tz)."""
    sch = cfg.get("schedule", {}) or {}
    times = sch.get("times_utc") or ["02:17"]
    offset_min = int(sch.get("display_utc_offset_minutes", 0) or 0)
    label = sch.get("display_timezone_label", "local")
    views = []
    for t in times:
        try:
            hh, mm = (int(x) for x in str(t).split(":"))
            base = datetime.datetime(2000, 1, 1, hh, mm, tzinfo=datetime.timezone.utc)
            local = base + datetime.timedelta(minutes=offset_min)
            views.append({"utc": f"{hh:02d}:{mm:02d}",
                          "local": local.strftime("%H:%M"),
                          "label": label})
        except Exception:
            views.append({"utc": str(t), "local": str(t), "label": label})
    return views


def _history(published: list, cat_map: dict, limit_days: int = 14) -> list:
    """Published tools grouped by publish date (newest first)."""
    days: dict = {}
    for t in published:
        d = t.get("date_published") or "unknown"
        days.setdefault(d, []).append(t)
    out = []
    for d in sorted(days, reverse=True)[:limit_days]:
        try:
            label = datetime.date.fromisoformat(d).strftime("%A, %d %B %Y")
        except Exception:
            label = d
        tools = []
        for t in days[d]:
            tools.append({
                "title": t.get("title", t["slug"]),
                "url": t.get("url", ""),
                "category_name": cat_map.get(t.get("category", ""), {}).get(
                    "name", (t.get("category") or "").replace("-", " ").title()),
            })
        out.append({"date": d, "label": label, "tools": tools})
    return out


def _patch_tool_pages(dom: str, gsv: str) -> int:
    """Backfill already-generated tool pages with the verification meta tag
    and the Schedule nav link. Idempotent: pages already carrying both are
    left untouched, so repeated rebuilds are cheap no-ops."""
    patched = 0
    if not TOOLS_DIR.exists():
        return 0
    for page_path in sorted(TOOLS_DIR.glob("*/index.html")):
        try:
            html = page_path.read_text(encoding="utf-8")
        except Exception:
            continue
        orig = html
        if gsv and "google-site-verification" not in html:
            html = html.replace(
                '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
                '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
                f'<meta name="google-site-verification" content="{gsv}"/>',
                1,
            )
        if "/schedule/" not in html:
            html = html.replace(
                '">All Tools</a>',
                f'">All Tools</a>\n      <a href="{dom}/schedule/">Schedule</a>',
                1,
            )
        if html != orig:
            page_path.write_text(html, encoding="utf-8")
            patched += 1
    return patched


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
    sched_views = _schedule_views(cfg)
    html = homepage_tpl.render(
        site={
            "name": cfg["site_name"],
            "tagline": cfg.get("tagline", ""),
            "domain": dom,
            "base_path": "",
            "meta_description": f"{cfg['site_name']}: {cfg.get('tagline', 'Free online tools that run in your browser.')}"[:155],
            "google_verification": cfg.get("google_site_verification", ""),
        },
        categories_with_tools=with_tools,
        empty_categories=empty,
        total_tools=len(tools_views),
        schedule_count=len(sched_views),
        schedule_times_json=json.dumps([v["utc"] for v in sched_views]),
        year=utc_today()[:4],
        jsonld=json.dumps(jsonld, ensure_ascii=False).replace("</", "<\\/"),
        ad_top_desktop=cfg["ad_slots"]["leaderboard_728x90"],
        ad_top_mobile=cfg["ad_slots"]["rectangle_300x250"],
        ad_native=cfg["ad_slots"]["native_banner"],
        ad_footer=cfg["ad_slots"]["banner_468x60"],
        analytics=cfg.get("analytics_snippet", ""),
    )
    (ROOT / "index.html").write_text(html, encoding="utf-8")

    # ---------------- schedule page ----------------
    max_per_day = cfg.get("llm", {}).get("max_tools_published_per_day", 4)
    sched_desc = (
        f"New free browser tools are published automatically {len(sched_views)} times "
        f"a day on {cfg['site_name']}. See the exact publishing times, the next batch "
        f"countdown and every tool released so far."
    )[:155]
    sched_jsonld = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": f"Publishing Schedule — {cfg['site_name']}",
        "url": f"{dom}/schedule/",
        "description": sched_desc,
        "isPartOf": {"@type": "WebSite", "name": cfg["site_name"], "url": f"{dom}/"},
    }
    sched_tpl = env.get_template("schedule.html.j2")
    html = sched_tpl.render(
        site={
            "name": cfg["site_name"],
            "tagline": cfg.get("tagline", ""),
            "domain": dom,
            "base_path": "",
            "meta_description": sched_desc,
            "google_verification": cfg.get("google_site_verification", ""),
        },
        schedule_times=sched_views,
        schedule_count=len(sched_views),
        next_runs_json=json.dumps([v["utc"] for v in sched_views]),
        max_per_day=max_per_day,
        total_tools=len(tools_views),
        history=_history(tools_views, cats),
        year=utc_today()[:4],
        jsonld=json.dumps(sched_jsonld, ensure_ascii=False).replace("</", "<\\/"),
        ad_top_desktop=cfg["ad_slots"]["leaderboard_728x90"],
        ad_top_mobile=cfg["ad_slots"]["rectangle_300x250"],
        ad_native=cfg["ad_slots"]["native_banner"],
        ad_footer=cfg["ad_slots"]["banner_468x60"],
        analytics=cfg.get("analytics_snippet", ""),
    )
    sched_out = ROOT / "schedule" / "index.html"
    sched_out.parent.mkdir(parents=True, exist_ok=True)
    sched_out.write_text(html, encoding="utf-8")

    # ---------------- category pages ----------------
    cat_tpl = env.get_template("category.html.j2")
    for view in cat_views:
        html = cat_tpl.render(
            site={
                "name": cfg["site_name"],
                "domain": dom,
                "base_path": "",
                "google_verification": cfg.get("google_site_verification", ""),
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

    # ---------------- tool page backfill (verification + schedule nav) ---
    patched = _patch_tool_pages(dom, cfg.get("google_site_verification", ""))

    # ---------------- sitemap ----------------
    urls = [(f"{dom}/", utc_today(), "1.0"),
            (f"{dom}/schedule/", utc_today(), "0.7")]
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

    print(f"[build] homepage + schedule page + {len(cat_views)} category pages + sitemap "
          f"({len(tools_views)} tools, {patched} tool pages backfilled) rebuilt")


if __name__ == "__main__":
    rebuild()
