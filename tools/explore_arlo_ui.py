#!/usr/bin/env python3
"""Explore Arlo web app DOM to find livestream button selectors.

Launches headed Playwright, logs in to goldendev, navigates to devices,
and dumps relevant DOM structure around the device card.

Usage:
    python3 explore_arlo_ui.py [--headed]
"""

import time
import sys
from playwright.sync_api import sync_playwright

GOLDENDEV_EMAIL = "voodoojah@gmail.com"
GOLDENDEV_PASSWORD = "RepzRepz3472"
GOLDENDEV_SITE = "https://mygoldendev.arlo.com"
DEVICE_ID = "ALJ15BKX000EA"


def main():
    headed = "--headed" in sys.argv

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        print("Navigating to goldendev...")
        page.goto(GOLDENDEV_SITE, timeout=90000)
        time.sleep(10)  # Cloudflare challenge needs time

        # Check if login page
        print(f"Current URL: {page.url}")
        time.sleep(3)

        # Try to find and fill login form
        email_input = page.query_selector('input[type="email"], input[name="email"], input[id*="email"]')
        if email_input:
            print("Login form detected — filling credentials...")
            email_input.fill(GOLDENDEV_EMAIL)
            time.sleep(0.5)
            pw_input = page.query_selector('input[type="password"], input[name="password"]')
            if pw_input:
                pw_input.fill(GOLDENDEV_PASSWORD)
                time.sleep(0.5)
            submit = page.query_selector('button[type="submit"], button:has-text("Log In"), button:has-text("Sign In"), button:has-text("Continue")')
            if submit:
                print(f"  Clicking submit: {submit.inner_text()}")
                submit.click()
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(5)
        else:
            print("No login form found (may already be authenticated or SPA loading)")
            time.sleep(5)

        print(f"After login URL: {page.url}")
        page.screenshot(path="/tmp/arlo_ui_1_after_login.png")
        print("Screenshot saved: /tmp/arlo_ui_1_after_login.png")

        # Wait for app to load
        time.sleep(5)
        page.screenshot(path="/tmp/arlo_ui_2_loaded.png")
        print("Screenshot saved: /tmp/arlo_ui_2_loaded.png")

        # Look for device cards or livestream buttons
        print("\n--- Searching for interactive elements ---")

        # Dump all buttons
        buttons = page.query_selector_all("button")
        print(f"\nFound {len(buttons)} buttons:")
        for i, btn in enumerate(buttons[:30]):
            text = btn.inner_text().strip()[:60]
            aria = btn.get_attribute("aria-label") or ""
            cls = btn.get_attribute("class") or ""
            data_test = btn.get_attribute("data-testid") or btn.get_attribute("data-test") or ""
            if text or aria or data_test:
                print(f"  [{i}] text='{text}' aria='{aria}' data-test='{data_test}' class='{cls[:60]}'")

        # Look for anything with "live" or "stream"
        print("\n--- Elements matching 'live' or 'stream' ---")
        live_els = page.query_selector_all("[aria-label*='ive' i], [data-testid*='ive' i], [class*='live' i], [title*='ive' i]")
        for el in live_els[:15]:
            tag = el.evaluate("el => el.tagName")
            text = el.inner_text().strip()[:40]
            aria = el.get_attribute("aria-label") or ""
            cls = el.get_attribute("class") or ""
            print(f"  <{tag}> text='{text}' aria='{aria}' class='{cls[:80]}'")

        # Look for device-specific elements
        print(f"\n--- Elements matching device ID '{DEVICE_ID}' ---")
        dev_els = page.query_selector_all(f"[data-device-id='{DEVICE_ID}'], [data-id='{DEVICE_ID}'], [id*='{DEVICE_ID}']")
        print(f"  Found {len(dev_els)} direct matches")

        # Try to find the device card by text content
        print("\n--- Looking for device card by name ---")
        all_text = page.inner_text("body")
        if "Lory" in all_text or "Doorbell" in all_text or "AVD" in all_text:
            print("  Found device-related text on page")
        else:
            print("  No obvious device text found — may need to navigate")

        # Check for navigation links (Devices, Library, etc)
        print("\n--- Navigation elements ---")
        nav_els = page.query_selector_all("nav a, nav button, [role='navigation'] a, [role='tab'], a[href*='device'], a[href*='camera']")
        for el in nav_els[:15]:
            text = el.inner_text().strip()[:40]
            href = el.get_attribute("href") or ""
            print(f"  text='{text}' href='{href}'")

        # Full page HTML structure (top-level)
        print("\n--- Top-level DOM structure ---")
        structure = page.evaluate("""() => {
            function walk(el, depth) {
                if (depth > 3) return '';
                let result = '';
                const indent = '  '.repeat(depth);
                const id = el.id ? '#' + el.id : '';
                const cls = el.className && typeof el.className === 'string' ? '.' + el.className.split(' ').slice(0, 2).join('.') : '';
                const tag = el.tagName.toLowerCase();
                const role = el.getAttribute('role') ? ` role="${el.getAttribute('role')}"` : '';
                result += indent + `<${tag}${id}${cls}${role}>\\n`;
                for (const child of el.children) {
                    result += walk(child, depth + 1);
                }
                return result;
            }
            return walk(document.body, 0);
        }""")
        print(structure[:3000])

        if headed:
            print("\n\nBrowser is open — inspect manually. Press Ctrl+C to close.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        browser.close()


if __name__ == "__main__":
    main()
