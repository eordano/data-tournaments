#!/usr/bin/env python3
"""Wave-13 acceptance: drive the six UX surfaces on :4112, screenshot each."""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:4112"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots-w13")
os.makedirs(OUT, exist_ok=True)
fails = []

def shot(page, name):
    p = f"{OUT}/{name}.png"
    page.screenshot(path=p, full_page=True)
    print(name, "->", os.path.getsize(p))

def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0] if b.contexts else b.new_context()
    page = ctx.new_page()
    page.set_viewport_size({"width": 1440, "height": 1100})
    try:
        page.goto(BASE + "/results", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        shot(page, "01-results")
        body = page.inner_text("body")
        revise = page.locator("button:has-text('Revise')")
        check(revise.count() >= 1, "no Revise button on /results")
        if revise.count() >= 1:
            revise.first.click()
            page.wait_for_timeout(800)
            shot(page, "02-results-revise-panel")
            btn = page.locator("button[id^=revise-verdict-]")
            check(btn.count() >= 1, "no verdict buttons in revision panel")
            if btn.count() >= 1:
                btn.first.click()
                page.wait_for_timeout(400)
                reason = page.locator("textarea[name=reason], #revision-reason")
                check(reason.count() >= 1, "no reason textarea in revision panel")
                if reason.count() >= 1:
                    reason.first.fill(
                        "Acceptance pass: revisiting the wheel verdict after review.")
                    sub = page.locator(
                        "button:has-text('Submit revision'), "
                        "#revision-panel button[type=submit]")
                    check(sub.count() >= 1, "no submit in revision panel")
                    if sub.count() >= 1:
                        sub.first.click()
                        page.wait_for_timeout(2000)
                        shot(page, "03-results-revised")

        for tab in ("sources", "prompts", "rubrics", "pipelines", "policies"):
            page.goto(f"{BASE}/environment?tab={tab}",
                      wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(900)
            shot(page, f"04-environment-{tab}")
        body = page.inner_text("body")
        check("approver" in body.lower() or "polic" in body.lower(),
              "policies tab missing expected content")

        page.goto(BASE + "/catalog", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        check("/environment" in page.url, f"/catalog landed on {page.url}")
        page.goto(BASE + "/prompts", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        check("/environment" in page.url, f"/prompts landed on {page.url}")

        page.goto(BASE + "/brackets", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        body = page.inner_text("body")
        check("advanced" in body.lower(), "brackets missing advanced/legacy note")
        shot(page, "05-brackets-legacy")

        page.goto(BASE + "/campaigns/catalyrst-cid-w11",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        body = page.inner_text("body")
        for needle in ("branch", "finding"):
            check(needle in body.lower(), f"campaign hub missing section: {needle}")
        shot(page, "06-campaign-hub")

        page.goto(BASE + "/branch-fixes/3", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        body = page.inner_text("body")
        check("refused" in body.lower() or "tamper" in body.lower(),
              "branch 3 missing tamper refusal banner")
        cards = page.locator("[id^=diff-file-]")
        check(cards.count() >= 1, "no per-file diff cards on branch 3")
        shot(page, "07-branchfix3-diff")

        page.goto(BASE + "/branch-fixes/1", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        shot(page, "08-branchfix1-diff")

        page.goto(BASE + "/runs/show?id=release%3AYOUR_ORG%2Fcatalyrst%3A"
                  "3d2b3b273c63191c8b20d74334e65a381ed8a992",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        body = page.inner_text("body")
        check("DRY-RUN" in body, "run page missing DRY-RUN label")
        check("raw" in body.lower() or "json" in body.lower(),
              "run page missing raw JSON toggle")
        shot(page, "09-run-timeline")

        page.goto(BASE + "/judge?domain=catalyrst-cid-w11-workorders",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        aside = page.evaluate("() => document.querySelectorAll('aside').length")
        check(aside == 0, f"judge page still has {aside} aside(s)")
        hook = page.evaluate(
            "() => document.querySelectorAll('[phx-hook=JudgeShortcuts]').length")
        check(hook == 1, f"JudgeShortcuts hook count {hook}, want 1")
        shot(page, "10-judge-fullwidth")
    finally:
        page.close()

if fails:
    print("ACCEPTANCE FAILURES:", len(fails))
    sys.exit(1)
print("ACCEPTANCE OK — all surfaces verified")
