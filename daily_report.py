# -*- coding: utf-8 -*-
"""
每日財經日報 — GitHub Actions 自動排程版
流程：抓美股指數 + 財經/科技新聞 → GitHub Models（免費 AI）挑重點寫繁中摘要 → Gmail 寄出
所需 Secrets：GMAIL_ADDRESS、GMAIL_APP_PASSWORD、（選）SUBSCRIBERS_URL
"""

import os
import ssl
import time
import smtplib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import feedparser
import yfinance as yf

LA = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# ── 市場指數（^TNX 為 10 年期公債殖利率，特殊顯示）─────────────────
INDICES = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("^DJI",  "道瓊工業"),
    ("^VIX",  "VIX 恐慌指數"),
    ("^TNX",  "美債 10 年期殖利率"),
]

# ── 新聞來源（可自行增減，格式：(顯示名稱, RSS 網址)）──────────────
FEEDS = [
    ("CNBC",          "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("CNBC Tech",     "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("TechCrunch",    "https://techcrunch.com/feed/"),
]

PER_FEED = 10  # 每個來源取最新幾則給 AI 篩選


def is_manual() -> bool:
    """是否為手動觸發（Run workflow）。手動一律放行，方便隨時測試。"""
    return os.environ.get("GITHUB_EVENT_NAME") != "schedule"


def should_run() -> bool:
    """認班次不看時鐘：比對「這次是哪組 cron 叫醒的」與現在的冬夏令。
    只讀 cron 的「小時」欄位，所以之後微調分鐘（避開整點壅塞）不會改壞。
    GitHub 遲到再久都照寄，也不會重複寄；手動觸發一律放行。"""
    if is_manual():
        return True
    expr = os.environ.get("SCHEDULE_EXPR", "").split()
    if len(expr) < 2:
        return True  # 判斷不出來就照寄，寧可多寄也不要漏寄
    offset = datetime.now(LA).utcoffset().total_seconds() / 3600  # 夏令 -7、冬令 -8
    return expr[1] == ("2" if offset == -7 else "3")


def report_date():
    """這一班日報要報的日期（洛杉磯）。若因排程延遲跑到凌晨，仍算前一晚那一班。"""
    now = datetime.now(LA)
    return now.date() if now.hour >= 12 else (now - timedelta(days=1)).date()


def fetch_market():
    """回傳 (指數列表, 最近交易日日期)；交易日由 S&P 500 的最後一筆收盤決定。"""
    rows, session = [], None
    for symbol, name in INDICES:
        try:
            hist = yf.Ticker(symbol).history(period="7d")["Close"].dropna()
            if len(hist) >= 2:
                if symbol == "^GSPC":
                    session = hist.index[-1].date()
                last, prev = float(hist.iloc[-1]), float(hist.iloc[-2])
                rows.append({
                    "name": name,
                    "symbol": symbol,
                    "last": last,
                    "chg_pct": (last - prev) / prev * 100,
                    "chg_abs": last - prev,
                })
        except Exception as e:
            print(f"[market] {symbol} 抓取失敗：{e}")
    return rows, session


def fetch_news():
    items = []
    for source, url in FEEDS:
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            feed = feedparser.parse(resp.content)
            for e in feed.entries[:PER_FEED]:
                title = (e.get("title") or "").strip()
                link = e.get("link") or ""
                if title:
                    items.append({"source": source, "title": title, "link": link})
        except Exception as e:
            print(f"[news] {source} 抓取失敗：{e}")
    print(f"[news] 共收集 {len(items)} 則標題")
    return items


def fetch_subscribers(lang="zh"):
    """向 Apps Script 小窗口領取訂閱名單。
    Apps Script 偶爾會冷啟動吐出非 JSON 的頁面，因此重試 3 次，
    並把回應開頭印進 log 方便診斷；真的領不到就只寄給自己。"""
    base = os.environ.get("SUBSCRIBERS_URL", "").strip()
    if not base:
        print("[subscribers] 未設定 SUBSCRIBERS_URL，本次僅寄給自己")
        return []

    url = f"{base}&lang={lang}"
    for attempt in range(1, 4):
        resp = None
        try:
            resp = requests.get(url, headers=UA, timeout=30)
            emails = [str(e).strip() for e in resp.json() if "@" in str(e)]
            print(f"[subscribers] 領到 {len(emails)} 位訂閱者")
            return emails
        except Exception as e:
            snippet = ""
            if resp is not None:
                snippet = resp.text[:120].replace("\n", " ")
            print(f"[subscribers] 第 {attempt} 次領取失敗：{e}｜回應開頭：{snippet}")
            if attempt < 3:
                time.sleep(5)
    print("[subscribers] 三次都失敗，本次僅寄給自己")
    return []


def ai_digest(market, news):
    """呼叫 GitHub Models（免費額度）產生繁中摘要。失敗回傳 None 改用備援版。"""
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not news:
        return None

    market_text = "\n".join(
        f"{r['name']}: {r['last']:.2f} ({r['chg_pct']:+.2f}%)" for r in market
    ) or "（今日市場數據抓取失敗）"
    news_text = "\n".join(
        f"({n['source']}) {n['title']} | {n['link']}" for n in news
    )

    prompt = f"""你是一位資深財經編輯，請用繁體中文製作今日晨間日報。

【市場數據】
{market_text}

【過去 24 小時新聞標題與連結】
{news_text}

請只輸出「純 HTML 片段」（不要 markdown、不要 ``` 圍欄），僅能使用 <h3> <p> <ul> <li> <a> <strong> 標籤，內容依序為：
1. <h3>市場總評</h3>：2-3 句話點評美股整體情勢與值得留意的訊號
2. <h3>財經重點</h3>：從標題中挑出 5 則最重要的財經／市場新聞，每則一個 <li>，格式：<strong>一句話繁中摘要</strong>—<a href="原始連結">原文</a>
3. <h3>科技重點</h3>：挑 5 則最重要的科技／AI 新聞，格式同上
只挑真正重要、對投資人有資訊價值的，其餘捨棄；連結務必使用我提供的原始連結，不要自行編造。"""

    resp = requests.post(
        "https://models.github.ai/inference/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # 保險：若模型仍包了 code fence，剝掉
    if content.startswith("```"):
        content = content.strip("`").replace("html\n", "", 1).strip()
    return content


def fallback_html(news):
    """AI 失效時的備援：直接列出原始標題。"""
    lis = "".join(
        f'<li>({n["source"]}) <a href="{n["link"]}">{n["title"]}</a></li>'
        for n in news[:15]
    )
    return f"<h3>今日新聞</h3><p>（AI 摘要暫時失效，以下為原始標題）</p><ul>{lis}</ul>"


def build_email(market, digest_html, session):
    today = (session or report_date()).strftime("%Y%m%d")  # 以交易日為準的 8 位數日期

    # 美式配色：綠漲紅跌（想改台式紅漲綠跌，把下面兩個色碼對調即可）
    UP, DOWN = "#1e8449", "#c0392b"

    market_rows = ""
    for r in market:
        color = UP if r["chg_pct"] >= 0 else DOWN
        if r["symbol"] == "^TNX":  # 殖利率：顯示 % 值與 bp 變化
            value = f"{r['last']:.2f}%"
            change = f"{r['chg_abs'] * 100:+.0f} bp"
        else:
            value = f"{r['last']:,.2f}"
            change = f"{r['chg_pct']:+.2f}%"
        market_rows += (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{value}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:{color};font-weight:600;">{change}</td>'
            f'</tr>'
        )
    market_table = (
        f'<table style="border-collapse:collapse;width:100%;font-size:14px;">{market_rows}</table>'
        if market_rows else "<p>（今日市場數據抓取失敗，可能為資料源暫時限流）</p>"
    )

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f6f5f2;">
<div style="max-width:640px;margin:0 auto;padding:24px 16px;font-family:-apple-system,'PingFang TC','Microsoft JhengHei',sans-serif;color:#222;line-height:1.7;">
  <div style="background:#fff;border-radius:12px;padding:28px 24px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
    <p style="margin:0;font-size:12px;letter-spacing:2px;color:#a08a4f;">DAILY BRIEF</p>
    <h2 style="margin:4px 0 20px;font-size:22px;">每日財經日報 <span style="color:#a08a4f;">{today}</span></h2>
    <h3 style="font-size:16px;border-left:3px solid #a08a4f;padding-left:10px;">市場快照（{today} 收盤）</h3>
    {market_table}
    <div style="margin-top:8px;font-size:15px;">{digest_html}</div>
    <p style="margin-top:28px;font-size:12px;color:#999;border-top:1px solid #eee;padding-top:12px;">
      由 GitHub Actions 自動產生・美股交易日晚間發送・想退訂請直接回覆此信告知
    </p>
  </div>
</div>
</body></html>"""

    return f"每日財經日報 {today}", html


def send_email(subject, html):
    addr = os.environ["GMAIL_ADDRESS"]
    # 自動清掉不小心混進來的空格與不斷行空格
    pwd = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").replace("\u00a0", "")

    # 收件名單：自己 + 訂閱者，去重；抬頭只放自己，訂閱者全走密件副本（BCC）
    recipients = list(dict.fromkeys([addr] + fetch_subscribers("zh")))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(addr, pwd)
        server.sendmail(addr, recipients, msg.as_string())
    print(f"[mail] 已寄給 {len(recipients)} 位收件人（含自己）")


def main():
    if not should_run():
        print("非洛杉磯當地晚上 7 點的那組排程，跳過（冬夏令雙 cron 機制）")
        return

    market, session = fetch_market()
    today = report_date()

    # 只在美股有開盤的日子寄：週末與國定假日自動略過（用最近交易日判斷，不必維護假日表）
    if session and session != today:
        if is_manual():
            print(f"[market] {today} 美股休市（最近交易日 {session}），但手動觸發照寄")
        else:
            print(f"[market] {today} 美股休市（最近交易日 {session}），今天不寄日報")
            return
    if not session:
        print("[market] 無法判斷交易日（資料源可能限流），保險起見照常寄出")

    news = fetch_news()

    digest = None
    try:
        digest = ai_digest(market, news)
        print("[ai] 摘要產生成功")
    except Exception as e:
        print(f"[ai] 失敗，改用備援版：{e}")
    if not digest:
        digest = fallback_html(news)

    subject, html = build_email(market, digest, session)
    send_email(subject, html)
    print("✅ 日報已寄出")


if __name__ == "__main__":
    main()
