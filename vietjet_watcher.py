import os, json, re, asyncio, ssl, http.client, urllib.parse, subprocess
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ORIGIN = os.getenv("ORIGIN", "HAN")
DEST = os.getenv("DEST", "SGN")
DATE = os.getenv("DATE")  # YYYY-MM-DD
CURRENCY = os.getenv("CURRENCY", "VND")
PRICE_DROP_NOTIFY = int(os.getenv("PRICE_DROP_NOTIFY", "0"))

STATE_DIR = Path("state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"vietjet_{ORIGIN}_{DEST}_{DATE}.json"

SEARCH_URL = (
    "https://www.vietjetair.com/vi/search?tripType=1"
    f"&origin={ORIGIN}&destination={DEST}&departureDate={DATE}"
    f"&adult=1&child=0&infant=0&currency={CURRENCY}"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN/CHAT_ID"); return
    host = "api.telegram.org"
    path = f"/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, 443, context=ctx, timeout=30)
    try:
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="ignore")
        print("Telegram HTTP", resp.status, body[:400])
    finally:
        conn.close()

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text("utf-8"))
        except: return {}
    return {}

def save_state(obj):
    STATE_FILE.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")

async def fetch_price(play):
    browser = await play.chromium.launch(headless=True, args=[
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox","--disable-dev-shm-usage",
    ])
    ctx = await browser.new_context(user_agent=USER_AGENT, locale="vi-VN")
    page = await ctx.new_page()
    try:
        await page.goto(SEARCH_URL, wait_until="load", timeout=120000)
        try: await page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout: pass

        for sel in ["[data-testid='fare-card']", ".fare", "[class*='price']", "[data-test*='price']"]:
            try:
                await page.wait_for_selector(sel, timeout=15000)
                break
            except PWTimeout:
                continue

        html = await page.content()
        nums = re.findall(r"(\d{1,3}(?:[.,]\d{3})+|\d{5,10})", html)
        prices = []
        for s in nums:
            v = int(re.sub(r"[^\d]", "", s))
            if v >= 100000: prices.append(v)
        return min(prices) if prices else None
    finally:
        await ctx.close(); await browser.close()

def to_vnd(x): return f"{x:,}".replace(",", ".") + f" {CURRENCY}"

def git_commit_if_changed(message: str):
    subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
    subprocess.run(["git", "config", "user.email", "github-actions@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "state"], check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode != 0:
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
    else:
        print("No state changes to commit.")

async def main():
    print(f"URL: {SEARCH_URL}")
    state = load_state()
    prev = state.get("price")

    async with async_playwright() as p:
        price = await fetch_price(p)

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    if price is None:
        send_telegram(f"⚠️ Không đọc được giá VietJet {ORIGIN}→{DEST} {DATE}. ({ts})\n{SEARCH_URL}")
        return

    if prev is None:
        send_telegram(f"🛩️ VietJet {ORIGIN}→{DEST} ({DATE})\nGiá hiện tại: <b>{to_vnd(price)}</b>\n{SEARCH_URL}")
        save_state({"price": price, "last_update": ts})
        git_commit_if_changed(f"state: init {ORIGIN}-{DEST}-{DATE}")
        return

    diff = price - prev
    if diff != 0 and (PRICE_DROP_NOTIFY == 0 or diff <= -PRICE_DROP_NOTIFY):
        arrow = "⬇️" if diff < 0 else "⬆️"
        send_telegram(
            f"🛎️ Cập nhật {ORIGIN}→{DEST} ({DATE}) {arrow}\n"
            f"Cũ: {to_vnd(prev)}\nMới: <b>{to_vnd(price)}</b>\n"
            f"Chênh: {to_vnd(abs(diff))}\n({ts})\n{SEARCH_URL}"
        )

    save_state({"price": price, "last_update": ts})
    git_commit_if_changed(f"state: update {ORIGIN}-{DEST}-{DATE}")

if __name__ == "__main__":
    asyncio.run(main())
