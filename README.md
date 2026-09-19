# QuickToolsHub - Daily Tool Factory

An autonomous, quality-gated system that grows a website of small, free,
client-side web tools (calculators, converters, formatters, generators) - one
run per day via GitHub Actions, monetized with fixed-size ad slots.

**Quality over quantity**: a tool is published only if it passes every
automated check. Publishing 0 tools on a given day is a normal, expected
outcome.

## How it works (daily pipeline)

| Step | Script | What it does |
|------|--------|--------------|
| A | `scripts/generate_ideas.py` | Cheap model proposes ~8 tool ideas, grounded against the existing registry |
| B | `scripts/dedup.py` | Two-layer dedup (token overlap + title similarity, cheap-LLM judge for ambiguous cases) |
| C | `scripts/generate_tool.py` | Strong model builds each tool fragment + SEO meta + its own test cases |
| D | `scripts/test_tool.py` | Headless Chromium quality gate: zero JS errors, functional test cases pass, labelled inputs, no horizontal overflow at 360px, no unauthorized scripts |
| E | `scripts/inject_shell.py` | Wraps the tool in the shared shell: SEO tags, canonical URL, breadcrumb, JSON-LD, usage instructions, related links, ad slots (verbatim from config) |
| F | `scripts/update_registry.py`, `scripts/build_sitemap_and_home.py` | Registry upsert + regenerate homepage, category pages, sitemap.xml, robots.txt |
| G | `scripts/run_daily.py` | Commit + push (CI), clean exit on budget cap |

The whole flow is orchestrated by `scripts/run_daily.py` and triggered by
`.github/workflows/daily.yml` (cron 02:17 UTC + manual dispatch). Each step
fails independently - 2 failed tools never block 2 good ones.

## One-time setup (already done)

1. Repository secret `LLM_API_KEY` holds the Groq API key (never committed).
2. `config/site.json` holds the site name, domain and the **ad slot snippets -
   inserted verbatim into pages, never touched by the LLM**.
3. GitHub Pages serves this repo from the `main` branch root.

## Day-to-day operation

Fully automatic. To force an extra run: **Actions tab -> Daily Tool Factory ->
Run workflow**. Each run produces `logs/{date}.json` (also uploaded as a
workflow artifact): ideas proposed, rejections with reasons, published/failed
tools, and per-call token/cost accounting.

## Cost control (enforced in code)

- Cheap model (`idea_model`) for Steps A/B, strong model only for Step C.
- Daily caps in `config/site.json`: `daily_token_budget` (tokens) and
  `daily_cost_budget_usd`; remaining generation aborts cleanly when either is
  hit, and whatever already passed still gets published.
- Hard caps: `max_tools_published_per_day`, `max_generation_attempts_per_day`.
- Zero runtime LLM usage: published tools are 100% client-side forever.

## Ad slots

`config/site.json` -> `ad_slots` holds four snippets: `leaderboard_728x90`
(desktop top), `rectangle_300x250` (mobile top), `native_banner` (mid-page),
`banner_468x60` (desktop footer). Desktop/mobile variants are toggled with CSS
media queries (`hide-desktop` / `hide-mobile`). To swap ad networks, replace
the snippet values in that one file - every page is regenerated on the next
run.

## Local development

```bash
pip install -r requirements.txt
playwright install chromium
export LLM_API_KEY=...          # your key; never commit it
python scripts/run_daily.py     # full run (commit happens locally, push only in CI)
python scripts/build_sitemap_and_home.py   # rebuild static pages only
```

`git` commit runs locally too; pushing requires the CI environment or
`PUSH_ENABLED=1` plus an `origin` remote.

## Publishing schedule

The pipeline runs **3 times a day** (02:17, 10:17, 18:17 UTC — 07:47 / 15:47 /
23:47 IST), defined as literal cron entries in `.github/workflows/daily.yml`.
Daily publish caps are shared across all runs of the same UTC day, so the
worst case per day is still `max_tools_published_per_day`.

The public **schedule page** (`/schedule/`) is rebuilt from
`config/site.json` -> `schedule.times_utc` on every run and shows:

- the exact run times (UTC + configured display timezone),
- a live countdown to the next batch,
- how the pipeline works, and
- the publish history grouped by day (from `registry.json`).

To change the schedule: edit both the cron entries in `daily.yml` and
`schedule.times_utc` in `config/site.json`, then push — GitHub requires
literal cron values in workflow files, so there is no runtime override.

## Search-engine verification

`config/site.json` -> `google_site_verification` is injected as a
`<meta name="google-site-verification">` tag into the head of every generated
page (home, schedule, categories, tools). To verify the site in Google Search
Console, just replace that value — no template edits needed.
