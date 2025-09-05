import os, json, re, asyncio, ssl, http.client, urllib.parse, subprocess, time
from datetime import datetime, date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --- ENV ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ORIGIN   = os.getenv("ORIGIN", "HAN")
DEST     = os.getenv("DEST", "SGN")
YEAR     = int(os.getenv("YEAR", "2026"))
MONTH    = int(os.getenv("MONTH", "2"))
CURRENCY = os.getenv("CURRENCY", "VND")
ALWAYS_SEND = os.getenv("ALWAYS_SEND", "false").lower() == "true"
PRICE_DROP_NOTIFY = int(os.getenv("PRICE_DROP_NOTIFY", "0"))

# Tinh chỉnh tốc độ/an toàn
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "90000"))   # 90s
NETWORK_IDLE_MS = int(os.getenv("NETWORK_IDLE_MS", "7000"))    # 7s
SLEEP_BETWEEN_S = float(os.getenv("SLEEP_BETWEEN_S", "0.7"))   # nghỉ giữa lần điều hướng
MAX_MINUTES     = int(os.getenv("MAX_MINUTES", "15"))          # cắt nếu chạy quá lâu

STATE_DIR = Path("state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"vietjet_month_{ORIGIN}_{DEST}_{YEAR}-{MONTH:02d}.json"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SEARCH_BASE = ("https://www.vietjetair.com/vi/search?tripType=1"
               f"&origin={ORIGIN}&destination={DEST}"
               "&adult=1&child=0&infant=0"
               f"&currency={CURRENCY}")

def to_vnd(x:int)->str: return f"{x:,}".replace(",", ".") + f" {CURRENCY}"

def send_telegram(text:str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN/CHAT_ID"); return
    host="api.telegram.org"; path=f"/bot{BOT_TOKEN}/sendMessage"
    payload=urllib.parse.urlencode({"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":"true"})
    headers={"Content-Type":"application/x-www-form-urlencoded"}
    ctx=ssl.create_default_context()
    conn=http.client.HTTPSConnection(host,443,context=ctx,timeout=30)
    try:
        conn.request("POST",path,body=payload,headers=headers)
        resp=conn.getresponse(); body=resp.read().decode("utf-8","ignore")
        print("Telegram HTTP",resp.status,body[:300])
    finally:
        conn.close()

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text("utf-8"))
        except: return {}
    return {}

def git_commit_if_changed(message:str):
    subprocess.run(["git","config","user.name","github-actions"],check=True)
    subprocess.run(["git","config","user.email","github-actions@users.noreply.github.com"],check=True)
    subprocess.run(["git","add","state"],check=False)
    diff=subprocess.run(["git","diff","--cached","--quiet"])
    if diff.returncode!=0:
        subprocess.run(["git","commit","-m",message],check=True)
        subprocess.run(["git","push"],check=True)
    else:
        print("No state changes to commit.")

def iter_days(year:int,month:int):
    d=date(year,month,1)
    while d.month==month:
        yield d
        d+=timedelta(days=1)

async def fetch_min_price_on_page(page, url:str)->int|None:
    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    try: await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
    except PWTimeout: pass
    html = await page.content()

    # Bắt số tiền (VND) từ toàn trang
    nums = re.findall(r"(\d{1,3}(?:[.,]\d{3})+|\d{5,10})", html)
    prices=[]
    for s in nums:
        v=int(re.sub(r"[^\d]","",s))
        if v>=100000: prices.append(v)
    return min(prices) if prices else None

async def main():
    start=time.time()
    prev_state = load_state()
    prev_prices: dict[str,int] = prev_state.get("prices", {})

    results: dict[str,int] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox","--disable-dev-shm-usage",
        ])
        ctx = await browser.new_context(user_agent=USER_AGENT, locale="vi-VN")
        page = await ctx.new_page()
        try:
            for d in iter_days(YEAR, MONTH):
                if (time.time()-start)/60 > MAX_MINUTES:
                    print("⏳ Hit MAX_MINUTES, stop early"); break
                url=f"{SEARCH_BASE}&departureDate={d.isoformat()}"
                print("→", d.isoformat(), url)
                price = await fetch_min_price_on_page(page, url)
                if price is not None:
                    results[d.isoformat()] = price
                await asyncio.sleep(SLEEP_BETWEEN_S)
        finally:
            await ctx.close(); await browser.close()

    # So sánh & chuẩn bị message
    changes=[]
    for day, newp in results.items():
        oldp = prev_prices.get(day)
        if oldp is None:
            changes.append((day, None, newp))
        elif newp!=oldp and (PRICE_DROP_NOTIFY==0 or (newp-oldp)<=-PRICE_DROP_NOTIFY):
            changes.append((day, oldp, newp))

    min_price = min(results.values()) if results else None
    cheapest_days = [d for d,p in results.items() if p==min_price] if results else []

    ts=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    month_title=f"{MONTH:02d}/{YEAR}"
    seo_link="https://www.vietjetair.com/vi/ve-may-bay/ve-may-bay-ha-noi-di-tp-ho-chi-minh/"

    should_send = (ALWAYS_SEND and results) or bool(changes)
    if should_send:
        sorted_days = sorted(results.items(), key=lambda x:(x[1], x[0]))[:10]
        top_lines = [f"{i}. Ngày {d[-2:]}: {to_vnd(p)}" for i,(d,p) in enumerate(sorted_days,1)]
        change_lines=[]
        for k, oldp, newp in changes[:10]:
            dd=k[-2:]
            if oldp is None: change_lines.append(f"+ Thêm Ngày {dd}: {to_vnd(newp)}")
            else:
                arrow="⬇️" if newp<oldp else "⬆️"
                change_lines.append(f"• Ngày {dd}: {to_vnd(oldp)} → <b>{to_vnd(newp)}</b> {arrow}")

        parts=[f"🗓️ VietJet {ORIGIN}→{DEST} tháng {month_title}",
               f"🕒 {ts}"]
        if min_price is not None:
            parts.append(f"💰 Rẻ nhất: <b>{to_vnd(min_price)}</b> (ngày {', '.join(sorted(d[-2:] for d in cheapest_days))})")
        if change_lines: parts.append("🔁 Thay đổi:\n" + "\n".join(change_lines))
        if top_lines: parts.append("🔟 Top 10 ngày rẻ nhất:\n" + "\n".join(top_lines))
        parts.append(f"📎 Xem lịch tháng: {seo_link}")
        send_telegram("\n".join(parts))

    new_state={"prices":results,"last_update":ts}
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    git_commit_if_changed(f"month state: {ORIGIN}-{DEST} {YEAR}-{MONTH:02d}")

if __name__ == "__main__":
    asyncio.run(main())
