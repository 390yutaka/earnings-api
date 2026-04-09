from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup
from datetime import date, timedelta
import re
import json
import os

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

PREDICTIONS_FILE = "/tmp/predictions.json"
GAS_URL = "https://script.google.com/macros/s/AKfycbxLejWYi8Z3HR9MJPz690hID-84tFq47s4sJIiC9QwcTVkzN9nxyIpDhMmvibEoLdFf/exec"

def load_predictions():
    if os.path.exists(PREDICTIONS_FILE):
        with open(PREDICTIONS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_predictions(data):
    with open(PREDICTIONS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)

async def load_predictions_from_sheets() -> dict:
    """Google Sheetsから予測データを取得"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(GAS_URL)
            data = r.json()
            return data.get("predictions", {})
    except Exception:
        return load_predictions()

async def save_predictions_to_sheets(date_str: str, companies: list):
    """Google Sheetsに予測データを保存"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            await client.post(GAS_URL, json={"date": date_str, "companies": companies})
    except Exception:
        pass
    # ローカルにも保存（バックアップ）
    preds = load_predictions()
    preds[date_str] = companies
    save_predictions(preds)

def next_bizday():
    import datetime as dt
    jst_now = dt.datetime.utcnow() + dt.timedelta(hours=9)
    today = jst_now.date()
    d = today + timedelta(days=1)
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

def parse_stophigh(html: str) -> list:
    """株探のストップ高ページをパース（銘柄名を正しく取得）"""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    # 「銘柄名」ヘッダーを含むテーブルを探す
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text()
        if "銘柄名" in text and "コード" in text:
            target_table = table
            break
    if not target_table:
        return results
    
    for row in target_table.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
        # コードのリンクを探す
        code_el = cols[0].find("a")
        if not code_el:
            continue
        ticker = re.sub(r"\D", "", code_el.get_text())
        if not re.match(r"^\d{4}$", ticker):
            continue
        # 銘柄名のリンクを探す（2列目のaタグ）
        name_el = cols[1].find("a")
        name = name_el.get_text(strip=True) if name_el else cols[1].get_text(strip=True)
        price  = cols[4].get_text(strip=True) if len(cols) > 4 else ""
        change = cols[7].get_text(strip=True) if len(cols) > 7 else ""
        results.append({
            "ticker": ticker,
            "name": name,
            "price": price,
            "change": change,
        })
    return results

async def get_stock_change(ticker: str, target_date: str) -> dict:
    symbol = f"{ticker}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            data = r.json()
        timestamps = data["chart"]["result"][0]["timestamp"]
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        opens = data["chart"]["result"][0]["indicators"]["quote"][0]["open"]
        import datetime
        target_dt = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
        for i, ts in enumerate(timestamps):
            d = datetime.datetime.fromtimestamp(ts).date()
            if d == target_dt and closes[i] and opens[i]:
                change_pct = round((closes[i] - opens[i]) / opens[i] * 100, 2)
                return {"ticker": ticker, "date": target_date, "open": round(opens[i], 1), "close": round(closes[i], 1), "change_pct": change_pct}
    except Exception:
        pass
    return {"ticker": ticker, "date": target_date, "change_pct": None}

@app.get("/")
def root():
    return {"status": "ok", "message": "Earnings API is running"}

@app.get("/app", response_class=HTMLResponse)
async def serve_app():
    html_path = Path(__file__).parent / "earnings_radar.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>earnings_radar.html が見つかりません</h1>", status_code=404)

@app.get("/api/next")
async def get_next():
    d = next_bizday()
    date_str = d.strftime("%Y-%m-%d")
    url = f"https://irbank.net/market/kessan?y={date_str}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    return {"date": date_str, "companies": parse_irbank(r.text, date_str)}

@app.get("/api/month")
async def get_month(year: int, month: int):
    from calendar import monthrange
    _, days = monthrange(year, month)
    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=60, follow_redirects=True) as client:
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
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    return {"date": date_str, "companies": parse_irbank(r.text, date_str)}

@app.get("/api/debug/stophigh")
async def debug_stophigh():
    url = "https://kabutan.jp/warning/?mode=3_1"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    result = []
    for i, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        for j, row in enumerate(rows[:3]):
            cols = row.find_all(["td", "th"])
            if cols:
                result.append({"table": i, "row": j, "cols": [c.get_text(strip=True)[:20] for c in cols[:8]]})
    return {"rows": result[:20]}

@app.get("/api/stophigh/today")
async def get_stophigh_today():
    url = "https://kabutan.jp/warning/?mode=3_1"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    stocks = parse_stophigh(r.text)
    today_str = date.today().strftime("%Y-%m-%d")
    for s in stocks:
        s["date"] = today_str
    return {"date": today_str, "stocks": stocks}

@app.get("/api/stophigh/after_earnings")
async def get_stophigh_after_earnings(days: int = 30):
    today = date.today()
    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        for i in range(1, days + 1):
            target = today - timedelta(days=i)
            if target.weekday() >= 5:
                continue
            date_str = target.strftime("%Y-%m-%d")
            try:
                r = await client.get(f"https://irbank.net/market/kessan?y={date_str}")
                companies = parse_irbank(r.text, date_str)
            except Exception:
                continue
            if not companies:
                continue
            next_d = target + timedelta(days=1)
            while next_d.weekday() >= 5:
                next_d += timedelta(days=1)
            try:
                sh_url = f"https://kabutan.jp/warning/?mode=3_1&date={next_d.strftime('%Y%m%d')}"
                sr = await client.get(sh_url)
                sh_stocks = parse_stophigh(sr.text)
                sh_tickers = {s["ticker"] for s in sh_stocks}
            except Exception:
                continue
            for c in companies:
                if c["ticker"] in sh_tickers:
                    results.append({
                        "ticker": c["ticker"], "name": c["name"],
                        "earnings_date": date_str,
                        "stophigh_date": next_d.strftime("%Y-%m-%d"),
                        "decision_type": c["decision_type"],
                        "market_cap": c["market_cap"],
                        "per": c["per"], "roe": c["roe"],
                    })
    return {"period_days": days, "stocks": results}

@app.post("/api/prediction/save")
async def save_prediction(body: dict):
    date_str = body.get("date")
    if not date_str:
        return {"error": "date required"}
    companies = body.get("companies", [])
    await save_predictions_to_sheets(date_str, companies)
    return {"saved": len(companies), "date": date_str}

@app.get("/api/prediction/verify")
async def verify_predictions(days: int = 30):
    predictions = await load_predictions_from_sheets()
    if not predictions:
        predictions = load_predictions()
    if not predictions:
        return {"results": [], "summary": {"total": 0, "hit": 0, "rate": 0}}
    today = date.today()
    results = []
    for date_str, companies in predictions.items():
        pred_date = date.fromisoformat(date_str)
        next_d = pred_date + timedelta(days=1)
        while next_d.weekday() >= 5:
            next_d += timedelta(days=1)
        if next_d > today:
            continue
        next_str = next_d.strftime("%Y-%m-%d")
        for c in companies:
            ticker = c.get("ticker")
            verdict = c.get("verdict")
            upside = c.get("upside", 0)
            if not ticker:
                continue
            price_data = await get_stock_change(ticker, next_str)
            change_pct = price_data.get("change_pct")
            if change_pct is None:
                continue
            hit_thresholds = {
                "stop-high": lambda x: x >= 15,
                "surge":     lambda x: x >= 3,
                "neutral":   lambda x: -3 <= x <= 3,
                "fall":      lambda x: x <= -3,
                "stop-low":  lambda x: x <= -15,
            }
            checker = hit_thresholds.get(verdict)
            is_hit = checker(change_pct) if checker else False
            results.append({
                "ticker": ticker, "name": c.get("name", ""),
                "earnings_date": date_str, "check_date": next_str,
                "verdict": verdict, "upside": upside,
                "change_pct": change_pct, "is_hit": is_hit,
                "open": price_data.get("open"), "close": price_data.get("close"),
            })
    total = len(results)
    hit = sum(1 for r in results if r["is_hit"])
    rate = round(hit / total * 100, 1) if total > 0 else 0
    high_upside = [r for r in results if r["upside"] >= 70]
    mid_upside  = [r for r in results if 40 <= r["upside"] < 70]
    low_upside  = [r for r in results if r["upside"] < 40]
    def calc_rate(lst):
        if not lst: return 0
        return round(sum(1 for r in lst if r["is_hit"]) / len(lst) * 100, 1)
    return {
        "results": sorted(results, key=lambda x: x["earnings_date"], reverse=True),
        "summary": {
            "total": total, "hit": hit, "rate": rate,
            "by_upside": {
                "high_70plus": {"count": len(high_upside), "rate": calc_rate(high_upside)},
                "mid_40_70":   {"count": len(mid_upside),  "rate": calc_rate(mid_upside)},
                "low_under40": {"count": len(low_upside),  "rate": calc_rate(low_upside)},
            }
        }
    }

@app.get("/api/prediction/list")
async def list_predictions():
    predictions = load_predictions()
    return {"dates": list(predictions.keys()), "total_days": len(predictions)}

@app.get("/api/enrich/{ticker}")
async def enrich_ticker(ticker: str):
    result = {"ticker": ticker}
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            url = f"https://kabutan.jp/stock/kabuka?code={ticker}"
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("table tr"):
                if "信用倍率" in row.get_text():
                    cols = row.find_all("td")
                    if cols:
                        val = cols[-1].get_text(strip=True).replace(",","")
                        try:
                            result["margin_ratio"] = float(val)
                        except:
                            pass
                    break
        except Exception:
            pass
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.T?interval=1d&range=10d"
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            data = r.json()
            volumes = data["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
            volumes = [v for v in volumes if v]
            if len(volumes) >= 6:
                avg5 = sum(volumes[-6:-1]) / 5
                today_vol = volumes[-1]
                if avg5 > 0:
                    result["volume_ratio"] = round(today_vol / avg5, 2)
        except Exception:
            pass
        try:
            url = f"https://kabutan.jp/stock/news?code={ticker}"
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            news = []
            for item in soup.select("div.news_headline, .news_list li, table.news_table tr")[:5]:
                text = item.get_text(strip=True)
                if text and len(text) > 10:
                    news.append(text[:100])
            result["news"] = news[:3]
        except Exception:
            pass
        news_score = 0
        for n in result.get("news", []):
            for kw in ["上方修正","増益","最高益","大幅増","サプライズ","好決算","増配","自社株買"]:
                if kw in n: news_score += 15
            for kw in ["下方修正","減益","赤字","損失","悪化","減配","希薄化"]:
                if kw in n: news_score -= 15
        result["news_score"] = news_score
        margin_score = 0
        mr = result.get("margin_ratio")
        if mr is not None:
            if mr < 1:    margin_score = 20
            elif mr < 2:  margin_score = 10
            elif mr < 5:  margin_score = 0
            elif mr < 10: margin_score = -10
            else:         margin_score = -20
        result["margin_score"] = margin_score
        vol_score = 0
        vr = result.get("volume_ratio")
        if vr is not None:
            if vr >= 3:     vol_score = 20
            elif vr >= 2:   vol_score = 12
            elif vr >= 1.5: vol_score = 6
            elif vr < 0.5:  vol_score = -5
        result["volume_score"] = vol_score
        result["extra_score"] = news_score + margin_score + vol_score
    return result
