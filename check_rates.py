#!/usr/bin/env python3
"""
Weekly bank rate monitor.
Fetches APY/yield from each bank, logs rate changes, and sends a monthly
summary to ntfy.sh.
"""
import asyncio
import json
import re
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "rates_log.json"
DEBUG_DIR = BASE_DIR / "debug_screenshots"
NTFY_URL = "https://ntfy.sh/bank-rates-kma9f"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def send_monthly_ntfy_alert(rates: dict):
    lines = [f"{name}: {rate}%" for name, rate in rates.items() if rate is not None]
    message = "Monthly Bank Rates\n" + "\n".join(lines)
    req = urllib.request.Request(NTFY_URL, data=message.encode(), method="POST")
    urllib.request.urlopen(req, timeout=10)
    print("Monthly ntfy alert sent.")


def should_send_monthly_alert(log: dict) -> bool:
    last = log.get("_last_alert")
    if not last:
        return True
    last_date = date.fromisoformat(last)
    today = date.today()
    return (today.year, today.month) != (last_date.year, last_date.month)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            return json.load(f)
    return {}


def save_log(log: dict):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def get_last_rate(log: dict, bank: str) -> Optional[float]:
    entries = log.get(bank, [])
    return entries[-1]["rate"] if entries else None


def record_rate_if_changed(log: dict, bank: str, rate: float) -> bool:
    if get_last_rate(log, bank) == rate:
        return False
    log.setdefault(bank, []).append({"date": str(date.today()), "rate": rate})
    return True


def parse_rate(text: str, strategy: str) -> Optional[float]:
    if strategy == "7day":
        m = re.search(r'7\s*Day\s+Yield\s*[\r\n]+\s*(\d+\.?\d+)\s*%', text, re.IGNORECASE)
        if m:
            return _validated(m.group(1))
        m = re.search(r'7[\s-]?day\s+yield[:\s]+(\d+\.?\d+)\s*%', text, re.IGNORECASE)
        if m:
            return _validated(m.group(1))
        for line in text.splitlines():
            if "7" in line and ("yield" in line.lower() or "%" in line):
                m = re.search(r'(\d+\.\d+)\s*%', line)
                if m:
                    return _validated(m.group(1))

    patterns = [
        r'(\d+\.\d+)\s*%\s*APY',
        r'APY[:\s]*(\d+\.\d+)\s*%',
        r'earn[^\n]{0,30}?(\d+\.\d+)\s*%',
        r'rate[:\s]+(\d+\.\d+)\s*%',
        r'yield[:\s]+(\d+\.\d+)\s*%',
        r'(\d+\.\d+)\s*%',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            v = _validated(m.group(1))
            if v is not None:
                return v
    return None


def _validated(raw: str) -> Optional[float]:
    v = float(raw)
    return v if 0.01 <= v <= 20.0 else None


async def get_page_text(page: Page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        try:
            return await page.evaluate("() => document.body.innerText")
        except Exception:
            return ""


async def fetch_rate(page: Page, bank: dict) -> Optional[float]:
    name = bank["name"]
    url = bank["url"]
    strategy = bank.get("strategy", "apy")
    timeout = bank.get("timeout_ms", 60000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(5000)
        text = await get_page_text(page)
        rate = parse_rate(text, strategy)
        if rate is None:
            DEBUG_DIR.mkdir(exist_ok=True)
            slug = name.replace(" ", "_")
            await page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=True)
            (DEBUG_DIR / f"{slug}.txt").write_text(text[:5000])
            print(f"  [{name}] rate not found — saved screenshot + text to {DEBUG_DIR}/")
        return rate
    except Exception as e:
        print(f"  [{name}] error: {e}")
        return None


async def check_all_rates(config: dict) -> dict:
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        for bank in config["banks"]:
            page = await context.new_page()
            print(f"Checking {bank['name']}...")
            results[bank["name"]] = await fetch_rate(page, bank)
            await page.close()
        await browser.close()
    return results


def main():
    config = load_config()
    log = load_log()
    rates = asyncio.run(check_all_rates(config))

    any_logged = False
    for bank in config["banks"]:
        name = bank["name"]
        rate = rates.get(name)

        if rate is None:
            print(f"  {name}: FAILED — could not parse rate")
            continue

        print(f"  {name}: {rate}%")

        if record_rate_if_changed(log, name, rate):
            any_logged = True

    if should_send_monthly_alert(log):
        send_monthly_ntfy_alert(rates)
        log["_last_alert"] = str(date.today())
        any_logged = True

    if any_logged:
        save_log(log)
        print("Log updated.")
    else:
        print("No rate changes — log unchanged.")


if __name__ == "__main__":
    main()
