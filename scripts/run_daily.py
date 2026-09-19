"""Daily pipeline orchestrator - calls Steps A-G in order.

Design guarantees (per the master spec):
- Quality-gated, not quota-gated: 0 published tools is a valid outcome.
- Each step's failure is caught and logged without crashing the whole run.
- Idempotent: re-runs never double-publish (registry slug checks + upserts).
- Budget-aware: token/cost caps abort remaining generation cleanly.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    ensure_dirs,
    load_daily_log,
    published_today_count,
    save_daily_log,
    utc_now_iso,
    utc_today,
)


def _start_run() -> None:
    log = load_daily_log()
    log["runs"].append({"started_at": utc_now_iso(), "status": "running", "notes": []})
    save_daily_log(log)


def _finish_run(status: str, summary: dict) -> None:
    log = load_daily_log()
    if not log["runs"]:
        log["runs"].append({"started_at": utc_now_iso(), "notes": []})
    log["runs"][-1].update({"finished_at": utc_now_iso(), "status": status, **summary})
    save_daily_log(log)


def _note(msg: str) -> None:
    print(f"[run] {msg}")
    from common import log_note
    log_note(msg)


def main() -> int:
    ensure_dirs()
    today = utc_today()
    print(f"=== Daily Tool Factory run - {today} ===")

    if not os.environ.get("LLM_API_KEY"):
        print("FATAL: LLM_API_KEY is not set. Add it as a repository secret.")
        _start_run()
        _finish_run("failed", {"error": "LLM_API_KEY missing"})
        return 1

    _start_run()

    # ------------------------------------------------ Step A: ideas --------
    ideas = []
    try:
        from generate_ideas import propose_ideas
        ideas = propose_ideas()
        _note(f"Step A: proposed {len(ideas)} ideas")
    except Exception as e:
        if type(e).__name__ == "BudgetExceeded":
            _note(f"Step A aborted: {e}")
        else:
            _note(f"Step A failed: {e}")
            traceback.print_exc()

    # ------------------------------------------------ Step B: dedup --------
    survivors = []
    if ideas:
        try:
            from dedup import dedup_ideas
            survivors = dedup_ideas(ideas)
            _note(f"Step B: {len(survivors)} ideas survived dedup")
        except Exception as e:
            _note(f"Step B failed (continuing with un-deduped ideas): {e}")
            survivors = ideas

    # ------------------------------------- Steps C/D/E/F: build & publish --
    published_slugs: list[str] = []
    failed_slugs: list[str] = []
    attempts = 0

    from common import config
    cfg = config()["llm"]
    cap = max(0, cfg["max_tools_published_per_day"] - published_today_count())
    attempt_cap = cfg.get("max_generation_attempts_per_day", cfg["max_tools_published_per_day"] + 2)

    try:
        for idea in survivors:
            if len(published_slugs) >= cap:
                _note(f"publish cap reached ({cfg['max_tools_published_per_day']}/day); "
                      f"skipping remaining {len(survivors) - attempts} candidate(s)")
                break
            if attempts >= attempt_cap:
                _note(f"generation attempt cap reached ({attempt_cap}); stopping for the day")
                break
            attempts += 1
            slug = idea["slug"]
            print(f"\n--- tool [{attempts}]: {slug} ---")

            # Step C: generate
            try:
                from generate_tool import ToolRejected, generate_tool
                tool_html, meta = generate_tool(idea)
            except Exception as e:
                if type(e).__name__ == "BudgetExceeded":
                    _note(f"budget exceeded during generation of {slug}; stopping")
                    break
                reason = getattr(e, "reason", str(e))
                _note(f"Step C rejected {slug}: {reason}")
                _log_failed(slug, "generate", reason)
                failed_slugs.append(slug)
                continue

            # Step D: test
            try:
                from test_tool import test_tool
                passed, checks = test_tool(slug, tool_html, meta)
            except Exception as e:
                _note(f"Step D crashed for {slug}: {e}")
                _log_failed(slug, "test", f"harness error: {e}")
                failed_slugs.append(slug)
                continue

            if not passed:
                failed_detail = "; ".join(
                    f"{c['check']}={c['passed']}" for c in checks if not c["passed"]
                )
                _note(f"Step D FAILED {slug}: {failed_detail}")
                _log_failed(slug, "test", failed_detail, checks)
                failed_slugs.append(slug)
                continue

            # Step E: inject shell
            try:
                from inject_shell import inject_shell
                inject_shell(slug, tool_html, meta)
            except Exception as e:
                _note(f"Step E failed for {slug}: {e}")
                _log_failed(slug, "inject", str(e))
                failed_slugs.append(slug)
                continue

            # Step F: registry + site rebuild
            try:
                from update_registry import register_published
                from build_sitemap_and_home import rebuild
                register_published(slug, meta)
                rebuild()
            except Exception as e:
                _note(f"Step F failed for {slug}: {e}")
                _log_failed(slug, "registry", str(e))
                failed_slugs.append(slug)
                continue

            published_slugs.append(slug)
            _note(f"PUBLISHED {slug}")

    except Exception as e:
        _note(f"build loop crashed: {e}")
        traceback.print_exc()

    # --------------------------------------------- Step G: commit & push ---
    commit_msg = (
        f"Daily tools: published {len(published_slugs)} - {', '.join(published_slugs)}"
        if published_slugs
        else f"Daily tools: 0 published, {len(failed_slugs) + (len(survivors) - attempts)} rejected"
    )
    git_status = _git_commit_and_push(commit_msg)

    # ------------------------------------------------------------- wrap ----
    from common import budget_status
    budget = budget_status()
    summary = {
        "published": published_slugs,
        "failed": failed_slugs,
        "ideas_proposed": len(ideas),
        "survived_dedup": len(survivors),
        "budget_tokens": budget["tokens"],
        "budget_usd": budget["usd"],
        "git": git_status,
    }
    _finish_run("completed" if git_status != "error" else "completed_with_git_error", summary)

    print("\n=== Daily run summary ===")
    print(f"Ideas proposed : {len(ideas)}")
    print(f"Survived dedup : {len(survivors)}")
    print(f"Attempts       : {attempts}")
    print(f"Published      : {len(published_slugs)} {published_slugs}")
    print(f"Failed         : {len(failed_slugs)} {failed_slugs}")
    print(f"Budget used    : {budget['tokens']} tokens ~ ${budget['usd']:.2f}")
    print(f"Git            : {git_status}")
    return 0


def _log_failed(slug: str, stage: str, reason: str, checks: list | None = None) -> None:
    from common import log_append
    entry = {"slug": slug, "stage": stage, "reason": reason, "at": utc_now_iso()}
    if checks:
        entry["checks"] = checks
    log_append("tools_failed", entry)


def _git_commit_and_push(message: str) -> str:
    """Step G. Commits (and pushes in CI) only when files actually changed."""
    try:
        git = ["git", "-c", "user.name=tool-factory-bot",
               "-c", "user.email=tool-factory-bot@users.noreply.github.com"]
        subprocess.run(["git", "add", "-A"], check=True, cwd=os.getcwd())
        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=os.getcwd()
        ).returncode != 0
        if not changed:
            return "no changes"
        subprocess.run(git + ["commit", "-m", message], check=True, cwd=os.getcwd())
        in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
        if in_ci or os.environ.get("PUSH_ENABLED") == "1":
            remote = subprocess.run(["git", "remote", "get-url", "origin"],
                                    capture_output=True, text=True, cwd=os.getcwd())
            if remote.returncode != 0:
                return "committed (no remote configured)"
            subprocess.run(["git", "push", "origin", "HEAD"], check=True, cwd=os.getcwd())
            return f"committed+pushed ({message})"
        return f"committed locally ({message})"
    except subprocess.CalledProcessError as e:
        print(f"git error: {e}")
        return "error"
    except Exception as e:
        print(f"git step failed: {e}")
        return "error"


if __name__ == "__main__":
    raise SystemExit(main())
