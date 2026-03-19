import json
import urllib.request
import xml.etree.ElementTree as ET
import concurrent.futures
from flask import Flask, jsonify, send_from_directory
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

app = Flask(__name__, static_folder="static")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
}

TROY_OZ_TO_GRAM = 31.1035

# ── Piyasa verileri ──────────────────────────────────────────────────────────

def fmt(val, decimals=2):
    return round(val, decimals) if val is not None else None

def yf_chart(symbol, interval="1m", range_="1d"):
    encoded = urllib.request.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={interval}&range={range_}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["chart"]["result"][0]

def get_yesterday_avg(result, decimals=2):
    q = result["indicators"]["quote"][0]
    opens  = q.get("open",  [])
    highs  = q.get("high",  [])
    lows   = q.get("low",   [])
    closes = q.get("close", [])
    for i in range(len(closes) - 2, -1, -1):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if all(v is not None for v in [o, h, l, c]):
            return fmt((o + h + l + c) / 4, decimals)
    return None

def fetch_symbol(symbol, decimals=2):
    intraday = yf_chart(symbol, "1m", "1d")
    daily    = yf_chart(symbol, "1d", "5d")
    meta  = intraday["meta"]
    price = meta.get("regularMarketPrice")
    prev  = meta.get("chartPreviousClose")
    change     = fmt(price - prev) if price and prev else None
    change_pct = fmt((price - prev) / prev * 100) if price and prev else None
    return {
        "price":         fmt(price, decimals),
        "prev":          fmt(prev,  decimals),
        "change":        change,
        "change_pct":    change_pct,
        "yesterday_avg": get_yesterday_avg(daily, decimals),
    }

@app.route("/api/price")
def api_price():
    symbol_map = {
        "gold":    ("GC=F",     2),
        "silver":  ("SI=F",     2),
        "bitcoin": ("BTC-USD",  0),
        "brent":   ("BZ=F",     2),
        "usd_try": ("TRY=X",    4),
        "eur_try": ("EURTRY=X", 4),
    }

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_symbol, sym, dec): key for key, (sym, dec) in symbol_map.items()}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                results[key] = {"price": None, "prev": None, "change": None, "change_pct": None, "yesterday_avg": None}

    gold_usd  = results.get("gold",    {}).get("price")
    gold_prev = results.get("gold",    {}).get("prev")
    gold_yavg = results.get("gold",    {}).get("yesterday_avg")
    usd_try   = results.get("usd_try", {}).get("price")
    usd_prev  = results.get("usd_try", {}).get("prev")
    usd_yavg  = results.get("usd_try", {}).get("yesterday_avg")

    gram_tl = gram_tl_change = gram_tl_pct = gram_tl_yavg = None
    if gold_usd and usd_try:
        gram_tl = fmt((gold_usd / TROY_OZ_TO_GRAM) * usd_try)
        if gold_prev and usd_prev:
            gram_tl_prev  = (gold_prev / TROY_OZ_TO_GRAM) * usd_prev
            gram_tl_change = fmt(gram_tl - gram_tl_prev)
            gram_tl_pct    = fmt((gram_tl - gram_tl_prev) / gram_tl_prev * 100)
        if gold_yavg and usd_yavg:
            gram_tl_yavg = fmt((gold_yavg / TROY_OZ_TO_GRAM) * usd_yavg)

    def item(key):
        r = results.get(key, {})
        return {"price": r.get("price"), "change": r.get("change"), "change_pct": r.get("change_pct"), "yesterday_avg": r.get("yesterday_avg")}

    return jsonify({
        "gold":    item("gold"),
        "gram_tl": {"price": gram_tl, "change": gram_tl_change, "change_pct": gram_tl_pct, "yesterday_avg": gram_tl_yavg},
        "silver":  item("silver"),
        "bitcoin": item("bitcoin"),
        "brent":   item("brent"),
        "usd_try": item("usd_try"),
        "eur_try": item("eur_try"),
    })

# ── Haberler ─────────────────────────────────────────────────────────────────

GOLD_KEYWORDS   = ["altın", "altin", "gold", "xau", "ons", "gram altın", "çeyrek altın"]
ALLOWED_DOMAINS = ["investing.com", "bigpara.com", "bigpara.hurriyet.com.tr", "bloomberght.com", "bloomberg.com"]

def is_gold_related(title):
    return any(kw in title.lower() for kw in GOLD_KEYWORDS)

def is_allowed_source(link, source):
    combined = (link + " " + source).lower()
    return any(d in combined for d in ALLOWED_DOMAINS)

@app.route("/api/news")
def api_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    q = "altın fiyat (site:investing.com OR site:bigpara.hurriyet.com.tr OR site:bloomberght.com)"
    url = "https://news.google.com/rss/search?q=" + urllib.request.quote(q) + "&hl=tr&gl=TR&ceid=TR:tr"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        xml_data = r.read()

    root  = ET.fromstring(xml_data)
    items = []
    for item in root.findall(".//item"):
        title   = item.findtext("title", "")
        link    = item.findtext("link", "#")
        pubdate = item.findtext("pubDate", "")
        source_el = item.find("source")
        source = source_el.text if source_el is not None else ""

        try:
            if parsedate_to_datetime(pubdate) < cutoff:
                continue
        except Exception:
            pass

        if not is_gold_related(title):
            continue
        if not is_allowed_source(link, source):
            continue

        items.append({"title": title, "link": link, "pubDate": pubdate, "source": source})

    return jsonify(items)

# ── Statik dosyalar ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8765)
