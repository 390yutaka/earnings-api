from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup
from datetime import date, timedelta
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def next_bizday():
    d = date.today() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d

def prev_bizday(d: date):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def parse_irbank(html: str, date_str: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for row in soup.select("table tr"):
        cols = row.find_all(["td", "th"])
        if not cols or cols[0].name == "th":
            continue
        link = cols[0].find("a")
        if not link:
            continue
        ticker = link.get_text(strip=True)
        if not re.match(r"^\d{4}$", ticker):
            continue
        name          = cols[1].get_text(strip=True) if len(cols) > 1 else ""
        decision_type = cols[2].get_text(strip=True) if len(cols) > 2 else ""
        ann_time      = cols[3].get_text(strip=True) if len(cols) > 3 else ""
        market_cap    = cols[4].get_text(strip=True) if len(cols) > 4 else ""
        per           = cols[5].get_text(strip=True) if len(cols) > 5 else ""
        roe           = cols[6].get_text(strip=True) if len(cols) > 6 else ""
        results.append({
            "ticker": ticker, "name": name, "date": date_str,
            "decision_type": decision_type, "announcement_time": ann_time,
            "market_cap": market_cap, "per": per, "roe": roe,
            "guidance": "未発表", "eps_actual": None, "eps_est": None, "rev_surprise": None,
        })
    return results

# ── 既存エンドポイント ────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Earnings API is running"}

@app.get("/api/next")
async def get_next():
    d = next_bizday()
    date_str = d.strftime("%Y-%m-%d")
    url = f"https://irbank.net/market/kessan?y={date_str}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
    return {"date": date_str, "companies": parse_irbank(r.text, date_str)}

@app.get("/api/month")
async def get_month(year: int, month: int):
    from calendar import monthrange
    _, days = monthrange(year, month)
    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=60) as client:
        for day in range(1, days + 1):
            d = date(year, month, day)
            if d.weekday() >= 5:
                continue
            date_str = d.strftime("%Y-%m-%d")
            try:
                r = await client.get(f"https://irbank.net/market/kessan?y={date_str}")
                companies = parse_irbank(r.text, date_str)
                if companies:
                    results.append({"date": date_str, "count": len(companies)})
            except Exception:
                pass
    return {"year": year, "month": month, "days": results}

@app.get("/api/day")
async def get_day(date_str: str):
    url = f"https://irbank.net/market/kessan?y={date_str}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
    return {"date": date_str, "companies": parse_irbank(r.text, date_str)}

# ── 新エンドポイント①: 今日のストップ高銘柄 ──────────

@app.get("/api/stophigh/today")
async def get_stophigh_today():
    """株探から本日のストップ高銘柄を取得"""
    url = "https://kabutan.jp/warning/?mode=3_1"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    for row in soup.select("table tr"):
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        # コード
        code_el = cols[0].find("a")
        if not code_el:
            continue
        ticker = re.sub(r"\D", "", code_el.get_text())
        if not re.match(r"^\d{4}$", ticker):
            continue
        name      = cols[1].get_text(strip=True) if len(cols) > 1 else ""
        price     = cols[2].get_text(strip=True) if len(cols) > 2 else ""
        change    = cols[3].get_text(strip=True) if len(cols) > 3 else ""
        volume    = cols[4].get_text(strip=True) if len(cols) > 4 else ""
        results.append({
            "ticker": ticker, "name": name,
            "price": price, "change": change, "volume": volume,
            "date": date.today().strftime("%Y-%m-%d")
        })

    return {"date": date.today().strftime("%Y-%m-%d"), "stocks": results}

# ── 新エンドポイント②: 決算後ストップ高照合 ────────────

@app.get("/api/stophigh/after_earnings")
async def get_stophigh_after_earnings(days: int = 30):
    """過去N日の決算発表銘柄のうち翌日ストップ高になったものを返す"""
    today = date.today()
    results = []

    async with httpx.AsyncClient(headers=HEADERS, timeout=60) as client:
        for i in range(1, days + 1):
            target = today - timedelta(days=i)
            if target.weekday() >= 5:
                continue
            date_str = target.strftime("%Y-%m-%d")

            # その日の決算発表銘柄を取得
            try:
                r = await client.get(f"https://irbank.net/market/kessan?y={date_str}")
                companies = parse_irbank(r.text, date_str)
            except Exception:
                continue

            if not companies:
                continue

            # 翌営業日を計算
            next_d = target + timedelta(days=1)
            while next_d.weekday() >= 5:
                next_d += timedelta(days=1)

            # 株探のストップ高ページから翌日のストップ高銘柄リストを取得
            next_str = next_d.strftime("%Y%m%d")
            try:
                sh_url = f"https://kabutan.jp/warning/?mode=3_1&date={next_str}"
                sr = await client.get(sh_url)
                sh_soup = BeautifulSoup(sr.text, "html.parser")
                sh_tickers = set()
                for row in sh_soup.select("table tr"):
                    cols = row.find_all("td")
                    if not cols:
                        continue
                    code_el = cols[0].find("a")
                    if not code_el:
                        continue
                    t = re.sub(r"\D", "", code_el.get_text())
                    if re.match(r"^\d{4}$", t):
                        sh_tickers.add(t)
            except Exception:
                continue

            # 照合
            for c in companies:
                if c["ticker"] in sh_tickers:
                    results.append({
                        "ticker": c["ticker"],
                        "name": c["name"],
                        "earnings_date": date_str,
                        "stophigh_date": next_d.strftime("%Y-%m-%d"),
                        "decision_type": c["decision_type"],
                        "market_cap": c["market_cap"],
                        "per": c["per"],
                        "roe": c["roe"],
                    })

    return {"period_days": days, "stocks": results}
