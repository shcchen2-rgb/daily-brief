# -*- coding: utf-8 -*-
"""
Daily Finance Brief (English edition) - GitHub Actions scheduled version
Secrets required: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, (optional) SUBSCRIBERS_URL
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

INDICES = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("^DJI",  "Dow Jones"),
    ("^VIX",  "VIX"),
    ("^TNX",  "US 10-Yr Treasury Yield"),
]

FEEDS = [
    ("CNBC",          "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("WSJ Markets",   "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("CNBC Tech",     "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("TechCrunch",    "https://techcrunch.com/feed/"),
]

PER_FEED = 10


def is_manual() -> bool:
    """Manual runs (Run workflow) always proceed, so testing is never blocked."""
    return os.environ.get("GITHUB_EVENT_NAME") != "schedule"


def should_run() -> bool:
    """Trust the cron slot, not the clock: match which cron fired against the
    current DST offset. Only the hour field is read, so tweaking the minute
    (to dodge top-of-hour congestion) won't break it."""
    if is_manual():
        return True
    expr = os.environ.get("SCHEDULE_EXPR", "").split()
    if len(expr) < 2:
        return True  # if undetermined, send anyway - better early than missing
    offset = datetime.now(LA).utcoffset().total_seconds() / 3600  # PDT -7, PST -8
    return expr[1] == ("2" if offset == -7 else "3")


def report_date():
    """The date this edition covers (LA). If a delayed run slips past midnight,
    it still counts as the previous evening's slot."""
    now = datetime.now(LA)
    return now.date() if now.hour >= 12 else (now - timedelta(days=1)).date()


def fetch_market():
    """Return (index rows, last session date); session date comes from S&P 500."""
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
            print(f"[market] {symbol} failed: {e}")
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
            print(f"[news] {source} failed: {e}")
    print(f"[news] collected {len(items)} headlines")
    return items


def fetch_subscribers(lang="en"):
    """Pull the subscriber list from the Apps Script endpoint.
    Apps Script occasionally cold-starts and returns a non-JSON page, so retry
    up to 3 times and log the start of the response body for diagnosis."""
    base = os.environ.get("SUBSCRIBERS_URL", "").strip()
    if not base:
        print("[subscribers] SUBSCRIBERS_URL not set; sending to self only")
        return []

    url = f"{base}&lang={lang}"
    for attempt in range(1, 4):
        resp = None
        try:
            resp = requests.get(url, headers=UA, timeout=30)
            emails = [str(e).strip() for e in resp.json() if "@" in str(e)]
            print(f"[subscribers] got {len(emails)} subscribers")
            return emails
        except Exception as e:
            snippet = ""
            if resp is not None:
                snippet = resp.text[:120].replace("\n", " ")
            print(f"[subscribers] attempt {attempt} failed: {e} | body starts: {snippet}")
            if attempt < 3:
                time.sleep(5)
    print("[subscribers] all 3 attempts failed; sending to self only")
    return []


def ai_digest(market, news):
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not news:
        return None

    market_text = "\n".join(
        f"{r['name']}: {r['last']:.2f} ({r['chg_pct']:+.2f}%)" for r in market
    ) or "(market data unavailable today)"
    news_text = "\n".join(
        f"({n['source']}) {n['title']} | {n['link']}" for n in news
    )

    prompt = f"""You are a senior financial editor producing today's evening market brief in English.

[Market data]
{market_text}

[Headlines and links from the past 24 hours]
{news_text}

Output ONLY a pure HTML fragment (no markdown, no ``` fences), using only these tags: <h3> <p> <ul> <li> <a> <strong>. Structure, in order:
1. <h3>Market Recap</h3>: 2-3 sentences on the overall US market and signals worth watching
2. <h3>Top Finance Stories</h3>: pick the 5 most important finance/market stories, one <li> each, format: <strong>one-sentence summary</strong> - <a href="original link">source</a>
3. <h3>Top Tech Stories</h3>: pick the 5 most important tech/AI stories, same format
Only include stories that genuinely matter to investors; drop the rest. Use the exact links I provided - never invent URLs."""

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
    if content.startswith("```"):
        content = content.strip("`").replace("html\n", "", 1).strip()
    return content


def fallback_html(news):
    lis = "".join(
        f'<li>({n["source"]}) <a href="{n["link"]}">{n["title"]}</a></li>'
        for n in news[:15]
    )
    return f"<h3>Today's Headlines</h3><p>(AI digest temporarily unavailable; raw headlines below)</p><ul>{lis}</ul>"


def build_email(market, digest_html, session):
    today = (session or report_date()).strftime("%Y%m%d")

    # US convention: green = up, red = down
    UP, DOWN = "#1e8449", "#c0392b"

    market_rows = ""
    for r in market:
        color = UP if r["chg_pct"] >= 0 else DOWN
        if r["symbol"] == "^TNX":
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
        if market_rows else "<p>(Market data unavailable today - source may be rate-limiting.)</p>"
    )

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f6f5f2;">
<div style="max-width:640px;margin:0 auto;padding:24px 16px;font-family:-apple-system,Georgia,serif;color:#222;line-height:1.7;">
  <div style="background:#fff;border-radius:12px;padding:28px 24px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
    <p style="margin:0;font-size:12px;letter-spacing:2px;color:#a08a4f;">DAILY BRIEF - ENGLISH EDITION</p>
    <h2 style="margin:4px 0 20px;font-size:22px;">Daily Finance Brief <span style="color:#a08a4f;">{today}</span></h2>
    <h3 style="font-size:16px;border-left:3px solid #a08a4f;padding-left:10px;">Market Snapshot ({today} close)</h3>
    {market_table}
    <div style="margin-top:8px;font-size:15px;">{digest_html}</div>
    <p style="margin-top:28px;font-size:12px;color:#999;border-top:1px solid #eee;padding-top:12px;">
      Automated by GitHub Actions - sent on US trading days. To unsubscribe, just reply to this email.
    </p>
  </div>
</div>
</body></html>"""

    return f"Daily Finance Brief {today}", html


def send_email(subject, html):
    addr = os.environ["GMAIL_ADDRESS"]
    pwd = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").replace("\u00a0", "")

    # Self + subscribers, deduped; headers show self only, subscribers are BCC
    recipients = list(dict.fromkeys([addr] + fetch_subscribers("en")))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(addr, pwd)
        server.sendmail(addr, recipients, msg.as_string())
    print(f"[mail] sent to {len(recipients)} recipients (incl. self)")


def main():
    if not should_run():
        print("Not this cron's slot for 7 PM LA time; skipping (DST dual-cron mechanism)")
        return

    market, session = fetch_market()
    today = report_date()

    # Only send on days the US market actually traded: weekends and holidays are
    # skipped automatically via the last session date - no holiday table needed.
    if session and session != today:
        if is_manual():
            print(f"[market] Market closed on {today} (last session {session}); manual run, sending anyway")
        else:
            print(f"[market] Market closed on {today} (last session {session}); no brief today")
            return
    if not session:
        print("[market] Could not determine session date (source may be rate-limiting); sending anyway")

    news = fetch_news()

    digest = None
    try:
        digest = ai_digest(market, news)
        print("[ai] digest generated")
    except Exception as e:
        print(f"[ai] failed, using fallback: {e}")
    if not digest:
        digest = fallback_html(news)

    subject, html = build_email(market, digest, session)
    send_email(subject, html)
    print("Email sent successfully")


if __name__ == "__main__":
    main()
