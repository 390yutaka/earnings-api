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

def parse_irbank(html: str, date_str: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for row in soup.select("table tr"):
        cols = row.find_all(["td", "th"])
        if not cols or cols[0].name == "th":
            continue
        # 銘柄コードはリンクから取得
        link = cols[0].find("a")
        if not link:
            continue
        ticker = link.get_text(strip=True)
        if not re.match(r"^\d{4}$", ticker):
            continue
        name         = cols[1].get_text(strip=True) if len(cols) > 1 else ""
        decision_type= cols[2].get_text(strip=True) if len(cols) > 2 else ""
        ann_time     = cols[3].get_text(strip=True) if len(cols) > 3 else ""
        market_cap   = cols[4].get_text(strip=True) if len(cols) > 4 else ""
        per          = cols[5].get_text(strip=True) if len(cols) > 5 else ""
        roe          = cols[6].get_text(strip=True) if len(cols) > 6 else ""
        results.append({
            "ticker": ticker,
            "name": name,
            "date": date_str,
            "decision_type": decision_type,
            "announcement_time": ann_time,
            "market_cap": market_cap,
            "per": per,
            "roe": roe,
            "guidance": "未発表",
            "eps_actual": None,
            "eps_est": None,
            "rev_surprise": None,
        })
    return results

@app.get("/")
def root():
    return {"status": "ok", "message": "Earnings API is running"}

@app.get("/api/next")
async def get_next():
    """翌営業日の決算発表銘柄"""
    d = next_bizday()
    date_str = d.strftime("%Y-%m-%d")
    url = f"https://irbank.net/market/kessan?y={date_str}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
    companies = parse_irbank(r.text, date_str)
    return {"date": date_str, "companies": companies}

@app.get("/api/month")
async def get_month(year: int, month: int):
    """月間カレンダー: 件数一覧"""
    # IRBANKトップに近い月のリストが出るが、
    # 各日付ページを叩いて件数を返す
    from calendar import monthrange
    _, days = monthrange(year, month)
    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        for day in range(1, days + 1):
            d = date(year, month, day)
            if d.weekday() >= 5:
                continue
            date_str = d.strftime("%Y-%m-%d")
            url = f"https://irbank.net/market/kessan?y={date_str}"
            try:
                r = await client.get(url)
                companies = parse_irbank(r.text, date_str)
                if companies:
                    results.append({"date": date_str, "count": len(companies)})
            except Exception:
                pass
    return {"year": year, "month": month, "days": results}

@app.get("/api/day")
async def get_day(date_str: str):
    """特定日の銘柄詳細"""
    url = f"https://irbank.net/market/kessan?y={date_str}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
    companies = parse_irbank(r.text, date_str)
    return {"date": date_str, "companies": companies}
