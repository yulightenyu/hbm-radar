#!/usr/bin/env python3
"""HBM Radar — 投资看板数据抓取
 - 价格 & 涨跌幅 (yfinance)
 - 财报关键指标: 毛利率/营收/EPS (yfinance financials)
 - MU 单支 DCF (config.json 假设)
 - HBM/Micron 新闻 (RSS, 标题+摘要匹配)
"""
import json, datetime, traceback, re
import yfinance as yf
import feedparser
from deep_translator import GoogleTranslator

CFG = json.load(open("config.json", encoding="utf-8"))

FEEDS = [
    "https://blocksandfiles.com/feed/",
    "https://www.techpowerup.com/rss/news",
]
HBM_KW = ["hbm", "hbm3", "hbm3e", "hbm4", "hbm4e", "high bandwidth memory",
          "high-bandwidth memory", "stacked dram"]
MICRON_KW = ["micron"]


def pct(cur, prev):
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 2)


def get_stocks():
    out = []
    for s in CFG["stocks"]:
        row = {"ticker": s["ticker"], "name": s["name"], "currency": s["currency"]}
        try:
            t = yf.Ticker(s["ticker"])
            h = t.history(period="5d")
            if len(h) >= 2:
                cur = float(h["Close"].iloc[-1])
                prev = float(h["Close"].iloc[-2])
                row["price"] = round(cur, 2)
                row["change"] = pct(cur, prev)
            fin = t.financials
            if fin is not None and not fin.empty:
                def grab(k):
                    try:
                        return float(fin.loc[k].iloc[0])
                    except Exception:
                        return None
                rev = grab("Total Revenue")
                gp = grab("Gross Profit")
                row["revenue"] = rev
                row["gross_margin"] = round(gp / rev * 100, 1) if (rev and gp) else None
            info = t.info
            row["eps"] = info.get("trailingEps")
            row["pe"] = info.get("trailingPE")
        except Exception:
            traceback.print_exc()
        out.append(row)
    return out


def get_dcf():
    """仅对 MU 计算。返回内在价值/股 与当前价对比。"""
    d = CFG["dcf"]
    res = {"ok": False}
    try:
        t = yf.Ticker("MU")
        cf = t.cashflow

        def cf_row(*names):
            for nm in names:
                try:
                    v = cf.loc[nm].iloc[0]
                    if v == v:  # not NaN
                        return float(v)
                except Exception:
                    continue
            return None

        # yfinance 行名跨版本有差异，按优先级尝试
        fcf_direct = cf_row("Free Cash Flow")
        if fcf_direct is not None:
            fcf0 = fcf_direct
        else:
            ocf = cf_row("Operating Cash Flow", "Total Cash From Operating Activities", "Cash Flow From Continuing Operating Activities")
            capex = cf_row("Capital Expenditure", "Capital Expenditures")  # 通常为负
            if ocf is None or capex is None:
                raise ValueError("cashflow rows not found: %s" % list(cf.index)[:8])
            fcf0 = ocf + capex
        g, tg, wacc, n = d["growth_5y"], d["terminal_growth"], d["wacc"], d["projection_years"]
        pv, fcf = 0.0, fcf0
        flows = []
        for yr in range(1, n + 1):
            fcf *= (1 + g)
            disc = fcf / ((1 + wacc) ** yr)
            pv += disc
            flows.append(round(fcf / 1e9, 2))
        tv = fcf * (1 + tg) / (wacc - tg)
        pv_tv = tv / ((1 + wacc) ** n)
        ev = pv + pv_tv
        info = t.info
        cash = info.get("totalCash", 0) or 0
        debt = info.get("totalDebt", 0) or 0
        shares = info.get("sharesOutstanding") or 1
        equity = ev + cash - debt
        fair = equity / shares
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        res = {
            "ok": True,
            "fcf0_b": round(fcf0 / 1e9, 2),
            "flows_b": flows,
            "tv_b": round(tv / 1e9, 2),
            "ev_b": round(ev / 1e9, 2),
            "fair_value": round(fair, 2),
            "price": round(price, 2) if price else None,
            "upside": pct(fair, price) if price else None,
            "assumptions": {"growth_5y": g, "terminal_growth": tg, "wacc": wacc},
        }
    except Exception:
        traceback.print_exc()
    return res


def get_news():
    seen, items = set(), []
    for url in FEEDS:
        try:
            f = feedparser.parse(url)
            src = (f.feed.get("title", "") or "").split("|")[0].strip()[:24]
            for e in f.entries[:40]:
                title = " ".join((e.get("title") or "").split())
                key = title.lower()
                if not title or key in seen:
                    continue
                seen.add(key)
                summ = e.get("summary", "") or e.get("description", "") or ""
                summ = " ".join(re.sub("<[^>]+>", " ", summ).split())[:500]
                ts = e.get("published_parsed") or e.get("updated_parsed")
                iso = datetime.datetime(*ts[:6]).isoformat() if ts else ""
                items.append({"title": title, "link": e.get("link", ""),
                              "src": src, "date": iso, "summary": summ})
        except Exception:
            traceback.print_exc()
    return items


def bucket(items, kws):
    out = []
    for it in items:
        hay = " " + it["title"].lower() + " " + it.get("summary", "").lower() + " "
        if any(k in hay for k in kws):
            out.append(it)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:10]


def translate_top(items, n=4):
    try:
        tr = GoogleTranslator(source="auto", target="zh-CN")
    except Exception:
        tr = None
    for it in items[:n]:
        it["zh"] = ""
        if tr:
            try:
                it["zh"] = tr.translate(it["title"])[:140]
            except Exception:
                traceback.print_exc()
    return items


def main():
    news = get_news()
    micron = translate_top(bucket(news, MICRON_KW))
    hbm = translate_top(bucket(news, HBM_KW))
    for b in (micron, hbm):
        for it in b:
            it.pop("summary", None)
    data = {
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "stocks": get_stocks(),
        "dcf": get_dcf(),
        "micron": micron,
        "hbm": hbm,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("done | stocks=%d dcf_ok=%s micron=%d hbm=%d" %
          (len(data["stocks"]), data["dcf"].get("ok"), len(micron), len(hbm)))


if __name__ == "__main__":
    main()
