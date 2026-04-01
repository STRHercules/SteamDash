#!/usr/bin/env python3
"""
Steam Dashboard - Real-time sales monitoring for Steam games
https://github.com/chihyunn/steam-dashboard

Zero external dependencies (stdlib only).
Settings stored in SQLite. Web-based setup wizard on first run.
Supports multiple games.
"""

import json
import time
import threading
import sqlite3
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timedelta

VERSION = "1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'steam_dashboard.db')
FINANCIAL_BASE = "https://partner.steam-api.com"
FINANCIAL_EMPTY_RESPONSE_WARNING = (
    "Steam returned an empty financial response. This usually means the key "
    "was created from a regular Steamworks group instead of a dedicated "
    "Financial API Group, or the account does not have access to this "
    "partner's financial data."
)
DEFAULT_TELEGRAM_CONFIG = {'enabled': False, 'bot_token': '', 'chat_ids': []}
DEFAULT_DISCORD_CONFIG = {'enabled': False, 'webhook_urls': []}

# ========== DATABASE ==========

def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS player_history (
        app_id TEXT, timestamp TEXT, player_count INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS review_history (
        app_id TEXT, timestamp TEXT, total_positive INTEGER, total_negative INTEGER, total_reviews INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_sales (
        app_id TEXT, date TEXT, units_sold INTEGER, units_returned INTEGER,
        gross_revenue_usd REAL, net_revenue_usd REAL,
        PRIMARY KEY (app_id, date)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales_snapshots (
        app_id TEXT, timestamp TEXT, total_units INTEGER, total_returns INTEGER,
        total_net_usd REAL, PRIMARY KEY (app_id, timestamp)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlist_history (
        app_id TEXT, timestamp TEXT, total_adds INTEGER, total_deletes INTEGER,
        total_purchases INTEGER, net_wishlists INTEGER
    )''')
    conn.commit()
    conn.close()


# --- Settings helpers ---

def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]
    return default


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()


def has_settings():
    return get_setting('steam_api_key') is not None


def get_all_settings():
    return {
        'steam_api_key': get_setting('steam_api_key', ''),
        'steam_financial_key': get_setting('steam_financial_key', ''),
        'games': get_setting('games', []),
        'telegram': get_setting('telegram', DEFAULT_TELEGRAM_CONFIG),
        'discord': get_setting('discord', DEFAULT_DISCORD_CONFIG),
        'dashboard': get_setting('dashboard', {'port': 8081, 'poll_interval': 300, 'language': 'en', 'theme': 'dark', 'accent': 'steam'}),
    }


def save_all_settings(data):
    set_setting('steam_api_key', data.get('steam_api_key', ''))
    set_setting('steam_financial_key', data.get('steam_financial_key', ''))
    set_setting('games', data.get('games', []))
    set_setting('telegram', data.get('telegram', DEFAULT_TELEGRAM_CONFIG))
    set_setting('discord', data.get('discord', DEFAULT_DISCORD_CONFIG))
    set_setting('dashboard', data.get('dashboard', {'port': 8081, 'poll_interval': 300, 'language': 'en', 'theme': 'dark', 'accent': 'steam'}))


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_game(game):
    g = dict(game or {})
    g['app_id'] = str(g.get('app_id', '')).strip()
    g['name'] = str(g.get('name', '')).strip()
    g['launch_date'] = str(g.get('launch_date', '')).strip()
    g['wishlist_baseline'] = parse_int(g.get('wishlist_baseline', 0), 0)
    return g


def get_wishlist_display_total(game, wishlist_data):
    return max(0, parse_int((game or {}).get('wishlist_baseline', 0), 0) + parse_int((wishlist_data or {}).get('net', 0), 0))


# --- Per-game data helpers ---

def save_player_count(app_id, count):
    conn = get_conn()
    conn.execute("INSERT INTO player_history VALUES (?, ?, ?)", (str(app_id), datetime.now().isoformat(), count))
    conn.commit()
    conn.close()


def save_review_data(app_id, pos, neg, total):
    conn = get_conn()
    conn.execute("INSERT INTO review_history VALUES (?, ?, ?, ?, ?)", (str(app_id), datetime.now().isoformat(), pos, neg, total))
    conn.commit()
    conn.close()


def upsert_daily_sales(app_id, date_str, units, returns, gross, net):
    conn = get_conn()
    conn.execute("""INSERT INTO daily_sales VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_id, date) DO UPDATE SET
        units_sold=excluded.units_sold, units_returned=excluded.units_returned,
        gross_revenue_usd=excluded.gross_revenue_usd, net_revenue_usd=excluded.net_revenue_usd
    """, (str(app_id), date_str, units, returns, gross, net))
    conn.commit()
    conn.close()


def get_player_history(app_id, limit=144):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, player_count FROM player_history WHERE app_id=? ORDER BY timestamp DESC LIMIT ?", (str(app_id), limit)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_all_daily_sales(app_id):
    conn = get_conn()
    rows = conn.execute("SELECT date, units_sold, units_returned, gross_revenue_usd, net_revenue_usd FROM daily_sales WHERE app_id=? AND (units_sold != 0 OR units_returned != 0 OR net_revenue_usd != 0) ORDER BY date", (str(app_id),)).fetchall()
    conn.close()
    return rows


def save_sales_snapshot(app_id, total_units, total_returns, total_net):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO sales_snapshots VALUES (?, ?, ?, ?, ?)",
                 (str(app_id), datetime.now().isoformat(), total_units, total_returns, total_net))
    conn.commit()
    conn.close()


def get_sales_snapshots(app_id):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, total_units, total_returns, total_net_usd FROM sales_snapshots WHERE app_id=? ORDER BY timestamp", (str(app_id),)).fetchall()
    conn.close()
    if not rows:
        return []
    result = []
    last_ts = None
    for row in rows:
        ts = datetime.fromisoformat(row[0])
        if last_ts is None or (ts - last_ts).total_seconds() >= 12 * 3600:
            result.append(row)
            last_ts = ts
    if rows[-1] not in result:
        result.append(rows[-1])
    return result


def save_wishlist_snapshot(app_id, adds, deletes, purchases, net):
    conn = get_conn()
    conn.execute("INSERT INTO wishlist_history VALUES (?, ?, ?, ?, ?, ?)",
                 (str(app_id), datetime.now().isoformat(), adds, deletes, purchases, net))
    conn.commit()
    conn.close()


def get_wishlist_history(app_id):
    conn = get_conn()
    rows = conn.execute("SELECT timestamp, net_wishlists FROM wishlist_history WHERE app_id=? ORDER BY timestamp DESC LIMIT 144", (str(app_id),)).fetchall()
    conn.close()
    return list(reversed(rows))


def get_sales_totals(app_id):
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(units_sold),0), COALESCE(SUM(units_returned),0), COALESCE(SUM(gross_revenue_usd),0), COALESCE(SUM(net_revenue_usd),0) FROM daily_sales WHERE app_id=?", (str(app_id),)).fetchone()
    conn.close()
    return row


# ========== HTTP FETCH WITH BACKOFF ==========

_api_fail_counts = {}


def fetch_json(url, label="api"):
    global _api_fail_counts
    try:
        req = Request(url, headers={"User-Agent": "SteamDashboard/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        _api_fail_counts[label] = 0
        return data
    except Exception as e:
        count = _api_fail_counts.get(label, 0) + 1
        _api_fail_counts[label] = count
        wait = min(2 ** count, 60)
        print(f"  [ERROR] {label}: {e} (backoff {wait}s)")
        time.sleep(wait)
        return None


def post_json(url, payload, label="api_post"):
    global _api_fail_counts
    try:
        req = Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "User-Agent": "SteamDashboard/1.0",
                "Content-Type": "application/json"
            }
        )
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode().strip()
        _api_fail_counts[label] = 0
        return json.loads(raw) if raw else {}
    except Exception as e:
        count = _api_fail_counts.get(label, 0) + 1
        _api_fail_counts[label] = count
        wait = min(2 ** count, 60)
        print(f"  [ERROR] {label}: {e} (backoff {wait}s)")
        time.sleep(wait)
        return None


# ========== STEAM API ==========

def get_current_players(api_key, app_id):
    data = fetch_json(
        f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}&key={api_key}",
        f"players_{app_id}"
    )
    if data and "response" in data:
        return data["response"].get("player_count", 0)
    return 0


def get_app_details(app_id):
    data = fetch_json(f"https://store.steampowered.com/api/appdetails?appids={app_id}", f"details_{app_id}")
    if data and str(app_id) in data and data[str(app_id)].get("success"):
        return data[str(app_id)]["data"]
    return None


def get_game_name_from_api(app_id):
    details = get_app_details(app_id)
    if details:
        return details.get("name", f"App {app_id}")
    return f"App {app_id}"


def get_reviews(app_id):
    data = fetch_json(
        f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=all&purchase_type=all&num_per_page=0",
        f"reviews_{app_id}"
    )
    if data and data.get("success") == 1:
        return data.get("query_summary", {})
    return {}


def get_recent_reviews(app_id):
    data = fetch_json(
        f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=all&purchase_type=all&num_per_page=5&filter=recent",
        f"recent_reviews_{app_id}"
    )
    if data and data.get("success") == 1:
        return data.get("reviews", [])
    return []


# ========== FINANCIAL API ==========

def fetch_sales_for_date(financial_key, app_id, date_str):
    app_id = str(app_id)
    units = 0
    returns = 0
    gross = 0.0
    net = 0.0
    hwm = 0

    while True:
        url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
               f"?key={financial_key}&date={date_str}&highwatermark_id={hwm}&include_view_grants=true")
        data = fetch_json(url, f"sales_{app_id}")
        if not data or "response" not in data:
            break
        resp = data["response"]
        for item in resp.get("results", []):
            if str(item.get("primary_appid", item.get("appid", ""))) == app_id:
                units += item.get("gross_units_sold", 0)
                returns += item.get("gross_units_returned", 0)
                gross += float(item.get("gross_sales_usd", 0))
                net += float(item.get("net_sales_usd", 0))
        max_id = resp.get("max_id", 0)
        if max_id == hwm or max_id == 0:
            break
        hwm = max_id

    return units, returns, gross, net


def fetch_sales_by_country(financial_key, app_id, launch_date):
    app_id = str(app_id)
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        hwm = 0
        while True:
            url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
                   f"?key={financial_key}&date={ds}&highwatermark_id={hwm}&include_view_grants=true")
            data = fetch_json(url, f"country_sales_{app_id}")
            if not data or "response" not in data:
                break
            resp = data["response"]
            for item in resp.get("results", []):
                if str(item.get("primary_appid", item.get("appid", ""))) == app_id:
                    cc = item.get("country_code", "??")
                    sold = item.get("gross_units_sold", 0)
                    ret = item.get("gross_units_returned", 0)
                    n = float(item.get("net_sales_usd", 0))
                    if cc not in countries:
                        countries[cc] = {"units": 0, "returns": 0, "net": 0.0}
                    countries[cc]["units"] += sold
                    countries[cc]["returns"] += ret
                    countries[cc]["net"] += n
            max_id = resp.get("max_id", 0)
            if max_id == hwm or max_id == 0:
                break
            hwm = max_id
        current += timedelta(days=1)

    return dict(sorted(countries.items(), key=lambda x: x[1]["units"], reverse=True))


def fetch_wishlist_for_date(financial_key, app_id, date_str):
    url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={financial_key}&appid={app_id}&date={date_str}"
    data = fetch_json(url, f"wishlist_day_{app_id}")
    if data and "response" in data:
        s = data["response"].get("wishlist_summary", data["response"].get("summary", {}))
        return {"adds": s.get("wishlist_adds", 0), "deletes": s.get("wishlist_deletes", 0),
                "purchases": s.get("wishlist_purchases", 0), "gifts": s.get("wishlist_gifts", 0)}
    return {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0}


def fetch_wishlist_totals(financial_key, app_id, launch_date):
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    total = {"adds": 0, "deletes": 0, "purchases": 0, "gifts": 0}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        day = fetch_wishlist_for_date(financial_key, app_id, ds)
        total["adds"] += day["adds"]
        total["deletes"] += day["deletes"]
        total["purchases"] += day["purchases"]
        total["gifts"] += day["gifts"]
        current += timedelta(days=1)

    total["net"] = total["adds"] - total["deletes"] - total["purchases"] - total["gifts"]
    return total


def fetch_wishlist_by_country(financial_key, app_id, launch_date):
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={financial_key}&appid={app_id}&date={ds}"
        data = fetch_json(url, f"wishlist_country_{app_id}")
        if data and "response" in data:
            for c in data["response"].get("country_summary", []):
                cc = c.get("country_code", "??")
                s = c.get("summary_actions", {})
                if cc not in countries:
                    countries[cc] = {"adds": 0, "deletes": 0, "purchases": 0}
                countries[cc]["adds"] += s.get("wishlist_adds", 0)
                countries[cc]["deletes"] += s.get("wishlist_deletes", 0)
                countries[cc]["purchases"] += s.get("wishlist_purchases", 0)
        current += timedelta(days=1)

    return dict(sorted(countries.items(), key=lambda x: x[1]["adds"], reverse=True))


def diagnose_financial_key(financial_key, app_id=None):
    if not financial_key:
        return {"ok": False, "status": "missing", "message": "No Steam Financial API key configured."}

    dates_data = fetch_json(
        f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetChangedDatesForPartner/v001/?key={financial_key}&highwatermark=0&include_view_grants=true",
        "financial_diag_dates"
    )
    if not dates_data or "response" not in dates_data:
        return {
            "ok": False,
            "status": "request_failed",
            "message": "Steam Financial API request failed. Check the key, network access, and Steam availability."
        }

    dates_resp = dates_data.get("response") or {}
    if dates_resp:
        return {"ok": True, "status": "ok", "message": ""}

    if app_id:
        today_str = datetime.now().strftime("%Y-%m-%d")
        wl_data = fetch_json(
            f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={financial_key}&appid={app_id}&date={today_str}",
            f"financial_diag_wishlist_{app_id}"
        )
        if wl_data and "response" in wl_data and (wl_data.get("response") or {}):
            return {"ok": True, "status": "ok", "message": ""}

    return {
        "ok": False,
        "status": "empty_response",
        "message": FINANCIAL_EMPTY_RESPONSE_WARNING
    }


def refresh_all_sales(financial_key, app_id, launch_date):
    launch = datetime.strptime(launch_date, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch

    while current <= today:
        ds = current.strftime("%Y-%m-%d")
        units, returns, gross, net = fetch_sales_for_date(financial_key, app_id, ds)
        upsert_daily_sales(app_id, ds, units, returns, gross, net)
        if units > 0 or returns > 0:
            print(f"  [{app_id}] [{ds}] +{units} sold, -{returns} returned, ${net:.2f} net")
        current += timedelta(days=1)


def refresh_recent_sales(financial_key, app_id):
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    for d in [yesterday, today]:
        ds = d.strftime("%Y-%m-%d")
        units, returns, gross, net = fetch_sales_for_date(financial_key, app_id, ds)
        upsert_daily_sales(app_id, ds, units, returns, gross, net)
        if units > 0 or returns > 0:
            print(f"  [{app_id}] [{ds}] +{units} sold, -{returns} returned, ${net:.2f} net")


# ========== NOTIFICATIONS ==========

def telegram_enabled(tg_config):
    return bool(tg_config.get('enabled') and tg_config.get('bot_token') and tg_config.get('chat_ids'))


def discord_enabled(dc_config):
    return bool(dc_config.get('enabled') and dc_config.get('webhook_urls'))


def send_telegram(tg_config, message):
    if not telegram_enabled(tg_config):
        return
    try:
        encoded = quote(message)
        for chat_id in tg_config['chat_ids']:
            url = f"https://api.telegram.org/bot{tg_config['bot_token']}/sendMessage?chat_id={chat_id}&text={encoded}&parse_mode=HTML"
            fetch_json(url, "telegram")
        print(f"  [TG] Sent to {len(tg_config['chat_ids'])} recipients")
    except Exception as e:
        print(f"  [TG ERROR] {e}")


def build_discord_embed(app_id, game_name, title, description="", color=0x66C0F4, fields=None, footer=None):
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": footer or f"Steam Dashboard - App {app_id}"},
        "url": f"https://store.steampowered.com/app/{app_id}/"
    }
    if fields:
        embed["fields"] = [
            {"name": str(name), "value": str(value), "inline": bool(inline)}
            for name, value, inline in fields
        ]
    return embed


def send_discord(dc_config, embed, content=""):
    if not discord_enabled(dc_config):
        return
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
    try:
        for idx, webhook_url in enumerate(dc_config.get('webhook_urls', []), start=1):
            post_json(webhook_url, payload, f"discord_{idx}")
        print(f"  [DC] Sent to {len(dc_config.get('webhook_urls', []))} webhooks")
    except Exception as e:
        print(f"  [DC ERROR] {e}")


def notify_channels(tg_config, dc_config, telegram_message=None, discord_embed=None, discord_content=""):
    if telegram_message:
        send_telegram(tg_config, telegram_message)
    if discord_embed:
        send_discord(dc_config, discord_embed, discord_content)


def get_game_from_settings(settings, app_id=None):
    games = [normalize_game(game) for game in settings.get('games', [])]
    if not games:
        return None
    if app_id:
        app_id = str(app_id)
        for game in games:
            if str(game.get('app_id')) == app_id:
                return game
    return games[0]


def send_startup_report(settings, game):
    app_id = game['app_id']
    game_name = game.get('name', app_id)
    tg = settings.get('telegram', {})
    dc = settings.get('discord', {})

    totals = get_sales_totals(app_id)
    units, returns, gross, net = totals
    players = get_current_players(settings['steam_api_key'], app_id)
    reviews = get_reviews(app_id)
    total_reviews = reviews.get("total_reviews", 0)
    total_positive = reviews.get("total_positive", 0)
    rate = round(total_positive / max(total_reviews, 1) * 100)
    launch_dt = datetime.strptime(game['launch_date'], "%Y-%m-%d")
    delta = datetime.now() - launch_dt
    days_since = delta.days
    hours_since = int(delta.total_seconds() // 3600)

    daily = get_all_daily_sales(app_id)
    daily_lines = ""
    for row in daily:
        d, u, r, g, n = row
        bar_len = min(u, 30)
        bar = "\u2588" * bar_len + "\u2591" * max(0, 30 - bar_len)
        daily_lines += f"\n  {d[5:]}  {bar} {u} ${n:.0f}"
    daily_lines = daily_lines.strip()
    daily_lines_discord = daily_lines or "No sales data yet."
    if len(daily_lines_discord) > 1000:
        daily_lines_discord = "...\n" + daily_lines_discord[-997:]

    msg = (
        f"\U0001f377 <b>{game_name} Dashboard Online</b>\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\n"
        f"\U0001f4ca <b>D+{days_since} ({hours_since}h)</b>\n"
        f"  Sales: <b>{units}</b> (refunds {returns})\n"
        f"  Revenue: ${gross:.0f} -> net ${net:.0f}\n"
        f"  Reviews: {total_reviews} ({rate}% positive)\n"
        f"  Players: {players}\n"
        f"\n"
        f"\U0001f4c8 <b>Daily Sales</b>\n"
        f"{daily_lines or '  No sales data yet.'}\n"
        f"\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f514 Monitoring started"
    )
    discord_embed = build_discord_embed(
        app_id,
        game_name,
        f"{game_name} Dashboard Online",
        f"D+{days_since} ({hours_since}h since launch)",
        color=0x66C0F4,
        fields=[
            ("Sales", f"{units} sold | {returns} refunded", True),
            ("Revenue", f"${gross:.0f} gross | ${net:.0f} net", True),
            ("Reviews", f"{total_reviews} total | {rate}% positive", True),
            ("Players", str(players), True),
            ("Daily Sales", daily_lines_discord, False),
        ],
        footer="Steam Dashboard startup report"
    )
    notify_channels(tg, dc, telegram_message=msg, discord_embed=discord_embed)


def send_test_alert(settings, game, alert_type):
    tg = settings.get('telegram', {})
    dc = settings.get('discord', {})
    if not telegram_enabled(tg) and not discord_enabled(dc):
        return False, "No Telegram or Discord alert channel is enabled."

    app_id = str(game['app_id'])
    game_name = game.get('name') or get_game_name_from_api(app_id)
    players = get_current_players(settings['steam_api_key'], app_id)
    reviews = get_reviews(app_id)
    total_reviews = reviews.get("total_reviews", 0)
    total_positive = reviews.get("total_positive", 0)
    total_negative = reviews.get("total_negative", 0)
    totals = get_sales_totals(app_id)
    total_units, returns, gross, net = totals
    wl_history = get_wishlist_history(app_id)
    latest_wishlist_net = wl_history[-1][1] if wl_history else 0

    alert_type = (alert_type or "").strip().lower()
    if alert_type == "startup":
        send_startup_report(settings, game)
        return True, f"Sent startup test alert for {game_name}."

    if alert_type == "sale":
        telegram_message = (
            f"\U0001f4b0 <b>Test new sale +1!</b>\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"  Game: {game_name}\n"
            f"  Total: {max(total_units, 0) + 1}\n"
            f"  Net revenue: ${max(net, 0):.0f}\n"
            f"  Players: {players}"
        )
        discord_embed = build_discord_embed(
            app_id,
            game_name,
            "Test new sale +1!",
            "Synthetic sale alert sent from Steam Dashboard.",
            color=0x57F287,
            fields=[
                ("Game", game_name, True),
                ("New Sales", "+1", True),
                ("Total Sales", max(total_units, 0) + 1, True),
                ("Net Revenue", f"${max(net, 0):.0f}", True),
                ("Players", players, True),
            ],
            footer="Steam Dashboard test sales alert"
        )
    elif alert_type == "wishlist":
        telegram_message = (
            f"\u2b50 <b>Test new wishlist +1!</b>\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"  Game: {game_name}\n"
            f"  Adds today: 1\n"
            f"  Net total: ~{max(latest_wishlist_net, 0) + 1}"
        )
        discord_embed = build_discord_embed(
            app_id,
            game_name,
            "Test new wishlist +1!",
            "Synthetic wishlist alert sent from Steam Dashboard.",
            color=0xC9A84C,
            fields=[
                ("Game", game_name, True),
                ("Adds This Poll", "+1", True),
                ("Adds Today", "1", True),
                ("Net Wishlist Total", f"~{max(latest_wishlist_net, 0) + 1}", True),
            ],
            footer="Steam Dashboard test wishlist alert"
        )
    elif alert_type == "review":
        synthetic_total = total_reviews + 1
        synthetic_positive = total_positive + 1
        telegram_message = (
            f"\U0001f4dd <b>Test new review (1)!</b>\n"
            f"Game: {game_name}\n"
            f"Total {synthetic_total} (+{synthetic_positive} -{total_negative})"
        )
        discord_embed = build_discord_embed(
            app_id,
            game_name,
            "Test new review (1)!",
            "Synthetic review alert sent from Steam Dashboard.",
            color=0x5865F2,
            fields=[
                ("Game", game_name, True),
                ("New Reviews", "1", True),
                ("Total Reviews", synthetic_total, True),
                ("Positive Rate", f"{round(synthetic_positive / max(synthetic_total, 1) * 100)}%", True),
                ("Breakdown", f"+{synthetic_positive} / -{total_negative}", True),
            ],
            footer="Steam Dashboard test review alert"
        )
    elif alert_type == "player":
        previous = max(players, 3)
        current = max(previous + 3, int(previous * 1.5) + 1)
        telegram_message = (
            f"\U0001f680 <b>Test player spike!</b>\n"
            f"Game: {game_name}\n"
            f"{previous} -> {current}"
        )
        discord_embed = build_discord_embed(
            app_id,
            game_name,
            "Test player spike!",
            "Synthetic player spike alert sent from Steam Dashboard.",
            color=0xE67E22,
            fields=[
                ("Game", game_name, True),
                ("Previous", previous, True),
                ("Current", current, True),
                ("Session Peak", current, True),
            ],
            footer="Steam Dashboard test player alert"
        )
    else:
        return False, "Unknown alert type. Use startup, sale, wishlist, review, or player."

    notify_channels(tg, dc, telegram_message=telegram_message, discord_embed=discord_embed)
    return True, f"Sent {alert_type} test alert for {game_name}."


# ========== DATA COLLECTOR ==========

class GameState:
    def __init__(self, app_id):
        self.app_id = str(app_id)
        self.last_player_count = 0
        self.last_review_count = 0
        self.last_total_units = 0
        self.last_wishlist_net = 0
        self.last_wishlist_adds_today = 0
        self.last_wishlist_deletes_today = 0
        self.last_wishlist_purchases_today = 0
        self.last_wishlist_gifts_today = 0
        self.peak_players = 0
        self.cached_wishlist = {}
        self.cached_sales_by_country = {}
        self.cached_wishlist_by_country = {}


class DataCollector:
    def __init__(self):
        self.game_states = {}
        self.collection_count = 0
        self.is_first_collection = True
        self._lock = threading.Lock()
        self.financial_diag = None
        self.financial_diag_checked_at = None

    def get_financial_diag(self, financial_key, app_id):
        now = datetime.now()
        stale = self.financial_diag_checked_at is None or (now - self.financial_diag_checked_at) >= timedelta(hours=1)
        if stale:
            prev_status = self.financial_diag.get("status") if self.financial_diag else None
            self.financial_diag = diagnose_financial_key(financial_key, app_id)
            self.financial_diag_checked_at = now
            if not self.financial_diag["ok"] and self.financial_diag.get("message") and self.financial_diag.get("status") != prev_status:
                print(f"  [FINANCIAL WARNING] {self.financial_diag['message']}")
        return self.financial_diag or {"ok": True, "status": "unknown", "message": ""}

    def get_state(self, app_id):
        app_id = str(app_id)
        if app_id not in self.game_states:
            self.game_states[app_id] = GameState(app_id)
        return self.game_states[app_id]

    def collect(self):
        if not has_settings():
            return

        settings = get_all_settings()
        api_key = settings['steam_api_key']
        financial_key = settings['steam_financial_key']
        games = settings.get('games', [])
        tg = settings.get('telegram', {})
        dc = settings.get('discord', {})

        if not games:
            return

        now = datetime.now().strftime('%H:%M:%S')
        print(f"[{now}] Collecting for {len(games)} game(s)...")

        self.collection_count += 1

        for game in games:
            app_id = str(game['app_id'])
            launch_date = game.get('launch_date', '2025-01-01')
            game_name = game.get('name', app_id)
            gs = self.get_state(app_id)
            wishlist_baseline = parse_int(game.get('wishlist_baseline', 0), 0)

            # Players + Reviews
            players = get_current_players(api_key, app_id)
            reviews = get_reviews(app_id)
            save_player_count(app_id, players)

            total_reviews = reviews.get("total_reviews", 0)
            total_positive = reviews.get("total_positive", 0)
            total_negative = reviews.get("total_negative", 0)
            save_review_data(app_id, total_positive, total_negative, total_reviews)

            if players > gs.peak_players:
                gs.peak_players = players

            # Sales
            fin_diag = self.get_financial_diag(financial_key, app_id)
            can_collect_financials = fin_diag.get("ok", False)
            if can_collect_financials and self.is_first_collection:
                existing = get_sales_totals(app_id)
                if existing[0] > 0:
                    print(f"  [{game_name}] Existing data, refreshing recent only...")
                    refresh_recent_sales(financial_key, app_id)
                else:
                    print(f"  [{game_name}] No data, full refresh...")
                    refresh_all_sales(financial_key, app_id, launch_date)
            elif can_collect_financials:
                refresh_recent_sales(financial_key, app_id)
            elif self.is_first_collection:
                print(f"  [{game_name}] Financial data unavailable: {fin_diag.get('message', 'Unknown financial API error.')}")

            totals = get_sales_totals(app_id)
            total_units = totals[0]
            net_revenue = totals[3]
            save_sales_snapshot(app_id, totals[0], totals[1], totals[3])
            today_wishlist = {
                "adds": gs.last_wishlist_adds_today,
                "deletes": gs.last_wishlist_deletes_today,
                "purchases": gs.last_wishlist_purchases_today,
                "gifts": gs.last_wishlist_gifts_today
            }
            if can_collect_financials:
                today_wishlist = fetch_wishlist_for_date(financial_key, app_id, datetime.now().strftime("%Y-%m-%d"))

            # Hourly cadence for expensive scans
            if can_collect_financials and (self.collection_count % 12 == 0 or self.is_first_collection):
                try:
                    gs.cached_sales_by_country = fetch_sales_by_country(financial_key, app_id, launch_date)
                    gs.cached_wishlist_by_country = fetch_wishlist_by_country(financial_key, app_id, launch_date)
                    print(f"  [{game_name}] Countries: {len(gs.cached_sales_by_country)} sales, {len(gs.cached_wishlist_by_country)} wishlist")
                except Exception as e:
                    print(f"  [{game_name}] [COUNTRY ERROR] {e}")

                try:
                    gs.cached_wishlist = fetch_wishlist_totals(financial_key, app_id, launch_date)
                    wl_net = gs.cached_wishlist.get("net", 0)
                    gs.cached_wishlist["display_total"] = max(0, wishlist_baseline + wl_net)
                    gs.cached_wishlist["baseline"] = wishlist_baseline
                    save_wishlist_snapshot(app_id, gs.cached_wishlist["adds"],
                                           gs.cached_wishlist["deletes"],
                                           gs.cached_wishlist["purchases"], wl_net)
                except Exception as e:
                    wl_net = gs.last_wishlist_net
                    print(f"  [{game_name}] [WISHLIST ERROR] {e}")
            else:
                wl_net = gs.last_wishlist_net
                gs.cached_wishlist["display_total"] = max(0, wishlist_baseline + wl_net)
                gs.cached_wishlist["baseline"] = wishlist_baseline

            # Alerts (skip on first collection)
            if self.is_first_collection:
                gs.last_wishlist_net = wl_net
                gs.last_wishlist_adds_today = today_wishlist.get("adds", 0)
                gs.last_wishlist_deletes_today = today_wishlist.get("deletes", 0)
                gs.last_wishlist_purchases_today = today_wishlist.get("purchases", 0)
                gs.last_wishlist_gifts_today = today_wishlist.get("gifts", 0)
                gs.last_player_count = players
                gs.last_review_count = total_reviews
                gs.last_total_units = total_units
                print(f"  [{game_name}] Baseline: units={total_units}, wl={wl_net}, reviews={total_reviews}, players={players}")
                continue

            prefix = f"[{game_name}] " if len(games) > 1 else ""

            # New wishlists
            new_wishlists = max(0, today_wishlist.get("adds", 0) - gs.last_wishlist_adds_today)
            if new_wishlists > 0:
                wishlist_msg = (
                    f"\u2b50 <b>{prefix}New wishlist{'s' if new_wishlists > 1 else ''} +{new_wishlists}!</b>\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"  Adds today: {today_wishlist.get('adds', 0)}\n"
                    f"  Conversions today: {today_wishlist.get('purchases', 0)}\n"
                    f"  Net total: ~{wl_net}"
                )
                wishlist_embed = build_discord_embed(
                    app_id,
                    game_name,
                    f"{prefix}New wishlist{'s' if new_wishlists > 1 else ''} +{new_wishlists}!",
                    "Steam wishlist activity detected.",
                    color=0xC9A84C,
                    fields=[
                        ("Adds This Poll", f"+{new_wishlists}", True),
                        ("Adds Today", today_wishlist.get('adds', 0), True),
                        ("Conversions Today", today_wishlist.get('purchases', 0), True),
                        ("Net Wishlist Total", f"~{wl_net}", True),
                    ],
                    footer="Steam Dashboard wishlist alert"
                )
                notify_channels(tg, dc, telegram_message=wishlist_msg, discord_embed=wishlist_embed)
            gs.last_wishlist_net = wl_net
            gs.last_wishlist_adds_today = today_wishlist.get("adds", 0)
            gs.last_wishlist_deletes_today = today_wishlist.get("deletes", 0)
            gs.last_wishlist_purchases_today = today_wishlist.get("purchases", 0)
            gs.last_wishlist_gifts_today = today_wishlist.get("gifts", 0)

            # Player spike
            if gs.last_player_count > 0 and players > gs.last_player_count * 1.5 and players >= 5:
                spike_msg = f"\U0001f680 <b>{prefix}Player spike!</b>\n{gs.last_player_count} -> {players}"
                spike_embed = build_discord_embed(
                    app_id,
                    game_name,
                    f"{prefix}Player spike!",
                    "Concurrent players jumped sharply.",
                    color=0xE67E22,
                    fields=[
                        ("Previous", gs.last_player_count, True),
                        ("Current", players, True),
                        ("Session Peak", gs.peak_players, True),
                    ],
                    footer="Steam Dashboard player alert"
                )
                notify_channels(tg, dc, telegram_message=spike_msg, discord_embed=spike_embed)

            # New review
            if gs.last_review_count > 0 and total_reviews > gs.last_review_count:
                n = total_reviews - gs.last_review_count
                review_msg = (
                    f"\U0001f4dd <b>{prefix}New review{'s' if n > 1 else ''} ({n})!</b>\n"
                    f"Total {total_reviews} (+{total_positive} -{total_negative})"
                )
                review_embed = build_discord_embed(
                    app_id,
                    game_name,
                    f"{prefix}New review{'s' if n > 1 else ''} ({n})!",
                    "Steam review count increased.",
                    color=0x5865F2,
                    fields=[
                        ("New Reviews", n, True),
                        ("Total Reviews", total_reviews, True),
                        ("Positive Rate", f"{round(total_positive / max(total_reviews, 1) * 100)}%", True),
                        ("Breakdown", f"+{total_positive} / -{total_negative}", True),
                    ],
                    footer="Steam Dashboard review alert"
                )
                notify_channels(tg, dc, telegram_message=review_msg, discord_embed=review_embed)

            # New sale
            if gs.last_total_units > 0 and total_units > gs.last_total_units:
                new_sales = total_units - gs.last_total_units
                country_lines = ""
                top_country_field = "No country breakdown yet."
                if gs.cached_sales_by_country:
                    sorted_countries = sorted(gs.cached_sales_by_country.items(),
                                              key=lambda x: x[1].get("units", 0), reverse=True)
                    top3 = sorted_countries[:3]
                    if top3:
                        lines = [f"  {cc}: {d['units']} units" for cc, d in top3]
                        country_lines = "\n\nTop countries:\n" + "\n".join(lines)
                        top_country_field = "\n".join([f"{cc}: {d['units']} units" for cc, d in top3])
                sale_msg = (
                    f"\U0001f4b0 <b>{prefix}New sale{'s' if new_sales > 1 else ''} +{new_sales}!</b>\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"  Total: {total_units}\n"
                    f"  Net revenue: ${net_revenue:.0f}\n"
                    f"  Players: {players}"
                    f"{country_lines}"
                )
                sale_embed = build_discord_embed(
                    app_id,
                    game_name,
                    f"{prefix}New sale{'s' if new_sales > 1 else ''} +{new_sales}!",
                    "Steam sales count increased.",
                    color=0x57F287,
                    fields=[
                        ("New Sales", f"+{new_sales}", True),
                        ("Total Sales", total_units, True),
                        ("Net Revenue", f"${net_revenue:.0f}", True),
                        ("Players", players, True),
                        ("Top Countries", top_country_field, False),
                    ],
                    footer="Steam Dashboard sales alert"
                )
                notify_channels(tg, dc, telegram_message=sale_msg, discord_embed=sale_embed)

            gs.last_player_count = players
            gs.last_review_count = total_reviews
            gs.last_total_units = total_units

            print(f"  [{game_name}] Players: {players} | Reviews: {total_reviews} | Sales: {total_units} | Peak: {gs.peak_players}")

        self.is_first_collection = False

    def loop(self):
        while True:
            try:
                settings = get_all_settings()
                interval = settings.get('dashboard', {}).get('poll_interval', 300)
            except Exception:
                interval = 300
            try:
                self.collect()
            except Exception as e:
                print(f"[COLLECTOR ERROR] {e}")
            time.sleep(interval)


# ========== SETUP WIZARD HTML ==========

SETUP_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTIiIGZpbGw9IiMxNzFhMjEiLz4KICA8cmVjdCB4PSIxMCIgeT0iMjIiIHdpZHRoPSI0NCIgaGVpZ2h0PSIzMiIgcng9IjMiIGZpbGw9IiMxYjI4MzgiIG9wYWNpdHk9IjAuNiIvPgogIDxyZWN0IHg9IjE0IiB5PSI0MCIgd2lkdGg9IjYiIGhlaWdodD0iMTIiIHJ4PSIxIiBmaWxsPSIjMmE0NzVlIi8+CiAgPHJlY3QgeD0iMjIiIHk9IjM0IiB3aWR0aD0iNiIgaGVpZ2h0PSIxOCIgcng9IjEiIGZpbGw9IiMzZDZjOGUiLz4KICA8cmVjdCB4PSIzMCIgeT0iMjgiIHdpZHRoPSI2IiBoZWlnaHQ9IjI0IiByeD0iMSIgZmlsbD0iIzY2YzBmNCIvPgogIDxyZWN0IHg9IjM4IiB5PSIzMiIgd2lkdGg9IjYiIGhlaWdodD0iMjAiIHJ4PSIxIiBmaWxsPSIjNjZjMGY0Ii8+CiAgPHJlY3QgeD0iNDYiIHk9IjI0IiB3aWR0aD0iNiIgaGVpZ2h0PSIyOCIgcng9IjEiIGZpbGw9IiM2NmMwZjQiLz4KICA8cG9seWxpbmUgcG9pbnRzPSIxNywzOCAyNSwzMiAzMywyNiA0MSwzMCA0OSwyMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjYTRkMDA3IiBzdHJva2Utd2lkdGg9IjIuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgPGNpcmNsZSBjeD0iMTciIGN5PSIzOCIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iMzMiIGN5PSIyNiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iNDkiIGN5PSIyMiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+Cjwvc3ZnPg==">
<title>Steam Dashboard - Setup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&family=Noto+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --steam-dark: #171a21;
  --steam-navy: #1b2838;
  --steam-blue-dark: #2a475e;
  --steam-blue-med: #3d6c8e;
  --steam-blue-light: #66c0f4;
  --steam-green: #5c7e10;
  --steam-green-bright: #a4d007;
  --steam-text: #c7d5e0;
  --steam-text-dim: #8f98a0;
  --steam-text-dark: #556772;
  --font-body: 'Noto Sans KR', 'Noto Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font-body);
  background: var(--steam-dark);
  color: var(--steam-text);
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
.wizard {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 680px;
}
.wizard-header {
  text-align: center;
  margin-bottom: 32px;
  padding-bottom: 20px;
  border-bottom: 1px solid #2a475e;
}
.wizard-header h1 {
  font-family: var(--font-body);
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 8px;
  letter-spacing: -0.01em;
  color: #ffffff;
}
.wizard-header p {
  font-size: 14px;
  color: var(--steam-text-dim);
  line-height: 1.6;
}
/* Steps indicator — Steam tab bar style */
.steps-bar {
  display: flex;
  justify-content: center;
  gap: 4px;
  margin-bottom: 24px;
  background: rgba(0,0,0,0.2);
  border-radius: 4px;
  padding: 3px;
}
.step-dot {
  flex: 1;
  height: 32px;
  border-radius: 2px;
  background: transparent;
  transition: all 0.3s;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: var(--steam-text-dark);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.step-dot.active {
  background: var(--steam-blue-dark);
  color: var(--steam-blue-light);
  box-shadow: 0 0 8px rgba(102,192,244,0.15);
}
.step-dot.done {
  background: rgba(92,126,16,0.2);
  color: var(--steam-green-bright);
}
/* Step panels */
.step-panel {
  display: none;
  animation: fadeIn 0.3s ease;
}
.step-panel.active {
  display: block;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.card {
  background: #16202d;
  border: 1px solid #2a475e;
  border-radius: 4px;
  padding: 24px;
  margin-bottom: 16px;
}
.card h2 {
  font-family: var(--font-body);
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 6px;
  color: #ffffff;
}
.card .hint {
  font-size: 13px;
  color: var(--steam-text-dim);
  margin-bottom: 20px;
  line-height: 1.6;
}
.card .hint a {
  color: var(--steam-blue-light);
  text-decoration: none;
}
.card .hint a:hover {
  text-decoration: underline;
}
.card .divider {
  border: none;
  border-top: 1px solid #2a475e;
  margin: 20px 0;
}
label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--steam-text-dim);
  margin-bottom: 6px;
  margin-top: 16px;
}
label:first-of-type { margin-top: 0; }
input[type="text"], input[type="password"], input[type="number"], input[type="date"], textarea {
  width: 100%;
  padding: 10px 14px;
  background: #32404e;
  border: 1px solid #556772;
  border-radius: 4px;
  color: var(--steam-text);
  font-family: var(--font-mono);
  font-size: 14px;
  transition: all 0.2s;
  outline: none;
}
textarea {
  min-height: 88px;
  resize: vertical;
}
input:focus, textarea:focus {
  border-color: var(--steam-blue-light);
  box-shadow: 0 0 8px rgba(102,192,244,0.3);
}
input::placeholder, textarea::placeholder {
  color: var(--steam-text-dark);
  opacity: 0.8;
}
/* Key status indicators */
.key-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-family: var(--font-mono);
  margin-left: 8px;
}
.key-status.ok { color: var(--steam-green-bright); }
.key-status.fail { color: #c45a5a; }
.key-status.pending { color: var(--steam-text-dark); }
/* Toggle */
.toggle-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.toggle {
  width: 44px;
  height: 24px;
  background: #32404e;
  border-radius: 12px;
  position: relative;
  cursor: pointer;
  transition: background 0.2s;
  flex-shrink: 0;
}
.toggle.on {
  background: var(--steam-green);
}
.toggle::after {
  content: '';
  position: absolute;
  top: 3px;
  left: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: white;
  transition: transform 0.2s;
}
.toggle.on::after {
  transform: translateX(20px);
}
.toggle-label {
  font-size: 14px;
  color: var(--steam-text-dim);
}
.tg-fields {
  display: none;
}
.tg-fields.visible {
  display: block;
}
.dc-fields {
  display: none;
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid rgba(42,71,94,0.5);
}
.dc-fields.visible {
  display: block;
}
/* Game list */
.game-item {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  margin-bottom: 12px;
  padding: 12px;
  background: rgba(0,0,0,0.25);
  border-radius: 4px;
  border: 1px solid rgba(42,71,94,0.4);
}
.game-item .field { flex: 1; }
.game-item .field label { margin-top: 0; }
.game-item .field .field-hint {
  font-size: 11px;
  color: var(--steam-text-dark);
  margin-top: 4px;
}
.game-item .remove-btn {
  background: rgba(196,90,90,0.15);
  border: 1px solid rgba(196,90,90,0.3);
  color: #c45a5a;
  padding: 8px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  margin-bottom: 0;
  height: 38px;
}
.game-item .game-status {
  font-size: 12px;
  font-family: var(--font-mono);
  margin-bottom: 0;
  height: 38px;
  display: flex;
  align-items: center;
  min-width: 20px;
}
.game-item .game-status.ok { color: var(--steam-green-bright); }
.game-item .game-status.fail { color: #c45a5a; }
.add-game-btn {
  background: transparent;
  border: 1px dashed #2a475e;
  color: var(--steam-text-dark);
  padding: 10px;
  width: 100%;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 13px;
  transition: all 0.2s;
}
.add-game-btn:hover {
  border-color: var(--steam-blue-light);
  color: var(--steam-text-dim);
}
/* Accent picker */
.accent-grid {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.accent-swatch {
  width: 40px;
  height: 40px;
  border-radius: 4px;
  cursor: pointer;
  border: 2px solid transparent;
  transition: all 0.2s;
  position: relative;
}
.accent-swatch.selected {
  border-color: #ffffff;
  box-shadow: 0 0 12px rgba(102,192,244,0.3);
}
.accent-swatch.selected::after {
  content: '\\2713';
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-weight: bold;
  font-size: 16px;
  text-shadow: 0 1px 3px rgba(0,0,0,0.5);
}
/* Language selector */
.lang-grid {
  display: flex;
  gap: 12px;
}
.lang-option {
  flex: 1;
  padding: 14px;
  border-radius: 4px;
  border: 2px solid #2a475e;
  cursor: pointer;
  text-align: center;
  font-size: 15px;
  font-weight: 500;
  transition: all 0.2s;
  color: var(--steam-text-dim);
  background: transparent;
}
.lang-option.selected {
  border-color: var(--steam-blue-light);
  color: #ffffff;
  background: rgba(102,192,244,0.08);
}
/* Test button — Steam green */
.test-btn {
  background: linear-gradient(to right, #75b022, #588a1b);
  border: none;
  color: #d2efa9;
  padding: 10px 20px;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 14px;
  font-weight: 600;
  margin-top: 16px;
  transition: all 0.2s;
}
.test-btn:hover {
  background: linear-gradient(to right, #8ecb2a, #6aa020);
  color: #ffffff;
}
.test-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.test-result {
  margin-top: 10px;
  font-size: 13px;
  font-family: var(--font-mono);
  padding: 10px 14px;
  border-radius: 4px;
  display: none;
  line-height: 1.6;
}
.test-result.success {
  display: block;
  background: rgba(92,126,16,0.15);
  border: 1px solid rgba(164,208,7,0.3);
  color: var(--steam-green-bright);
}
.test-result.error {
  display: block;
  background: rgba(196,90,90,0.1);
  border: 1px solid rgba(196,90,90,0.3);
  color: #c45a5a;
}
.test-result.partial {
  display: block;
  background: rgba(201,168,76,0.1);
  border: 1px solid rgba(201,168,76,0.3);
  color: #c9a84c;
}
/* Navigation buttons */
.nav-buttons {
  display: flex;
  justify-content: space-between;
  margin-top: 20px;
}
.nav-btn {
  padding: 12px 28px;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 15px;
  font-weight: 600;
  transition: all 0.2s;
  border: none;
}
.nav-btn.prev {
  background: transparent;
  border: 1px solid #2a475e;
  color: var(--steam-text-dim);
}
.nav-btn.prev:hover {
  border-color: var(--steam-blue-med);
  color: var(--steam-text);
}
.nav-btn.next {
  background: linear-gradient(to right, rgba(102,192,244,0.25), rgba(102,192,244,0.15));
  border: 1px solid rgba(102,192,244,0.4);
  color: var(--steam-blue-light);
}
.nav-btn.next:hover {
  background: linear-gradient(to right, rgba(102,192,244,0.35), rgba(102,192,244,0.25));
  color: #ffffff;
}
.nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  transform: none !important;
}
.nav-btn.start {
  background: linear-gradient(to right, #75b022, #588a1b);
  border: none;
  color: #d2efa9;
  font-size: 16px;
  padding: 14px 36px;
}
.nav-btn.start:hover {
  background: linear-gradient(to right, #8ecb2a, #6aa020);
  color: #ffffff;
}
</style>
</head>
<body>
<div class="wizard">
  <div class="wizard-header">
    <h1 data-i18n="setupTitle">Steam Dashboard Setup</h1>
    <p data-i18n="setupDesc">Real-time sales monitoring for your Steam games. Let's get you set up in a few quick steps.</p>
  </div>

  <div class="steps-bar">
    <div class="step-dot active" data-step="0" data-i18n="stepWelcome">INTRO</div>
    <div class="step-dot" data-step="1" data-i18n="stepConnection">CONNECT</div>
    <div class="step-dot" data-step="2" data-i18n="stepTelegram">ALERTS</div>
    <div class="step-dot" data-step="3" data-i18n="stepPrefs">PREFS</div>
    <div class="step-dot" data-step="4" data-i18n="stepConfirm">GO</div>
  </div>

  <!-- Step 0: Welcome -->
  <div class="step-panel active" data-step="0">
    <div class="card">
      <h2 data-i18n="welcomeTitle">Welcome</h2>
      <div class="hint" data-i18n-html="welcomeHint">
        This dashboard tracks your Steam game's sales, revenue, reviews,
        concurrent players, and wishlists in real-time. It can also send
        you Telegram and Discord alerts when something happens.
        <br><br>
        You'll need:
        <br>&bull; A <a href="https://steamcommunity.com/dev/apikey" target="_blank">Steam Web API Key</a>
        <br>&bull; A <a href="https://partner.steampowered.com/" target="_blank">Steamworks Financial API Key</a> (from Partner site)
        <br>&bull; Your game's App ID
      </div>
    </div>
  </div>

  <!-- Step 1: Steam Connection (API Keys + Games + Test) -->
  <div class="step-panel" data-step="1">
    <div class="card">
      <h2 data-i18n="connectionTitle">Steam Connection</h2>
      <div class="hint" data-i18n-html="connectionHint">
        Enter your API keys and add games to monitor. Test the connection before proceeding.
      </div>

      <label>Steam Web API Key <span class="key-status pending" id="apiKeyStatus"></span></label>
      <input type="password" id="steamApiKey" placeholder="E719B9C8C920A1EB..." />
      <div class="hint" style="margin-bottom:0;margin-top:6px;font-size:11px;" data-i18n-html="apiKeyPath">
        <a href="https://steamcommunity.com/dev/apikey" target="_blank">steamcommunity.com/dev/apikey</a>
      </div>

      <label style="margin-top:18px;">Steam Financial API Key <span class="key-status pending" id="finKeyStatus"></span></label>
      <input type="password" id="steamFinancialKey" placeholder="064E0AB9C952..." />
      <div class="hint" style="margin-bottom:0;margin-top:6px;font-size:11px;" data-i18n-html="finKeyPath">
        Steamworks Partner &rarr; Users &amp; Permissions &rarr; Manage Groups &rarr; [group] &rarr; Web API Key
      </div>

      <hr class="divider">

      <h2 data-i18n="gamesTitle" style="margin-top:0;">Your Games</h2>
      <div class="hint" data-i18n="gamesHint">Add one or more games to monitor. The game name will be fetched automatically.</div>
      <div id="gamesList"></div>
      <button class="add-game-btn" onclick="addGameRow()" data-i18n="addGame">+ Add Another Game</button>

      <button class="test-btn" id="testBtn" onclick="testConnection()" data-i18n="testConnection">Test Connection</button>
      <div class="test-result" id="testResult"></div>
    </div>
  </div>

  <!-- Step 2: Telegram -->
  <div class="step-panel" data-step="2">
    <div class="card">
      <h2 data-i18n="telegramTitle">Alert Channels</h2>
      <div class="hint" data-i18n="telegramHint">Get instant notifications for new sales, reviews, wishlists, and player spikes. This is optional.</div>
      <div class="toggle-row">
        <div class="toggle" id="tgToggle" onclick="toggleTelegram()"></div>
        <span class="toggle-label" data-i18n="enableTelegram">Enable Telegram alerts</span>
      </div>
      <div class="tg-fields" id="tgFields">
        <label data-i18n="botTokenLabel">Bot Token</label>
        <input type="password" id="tgBotToken" placeholder="123456:ABC-DEF..." />
        <label data-i18n="chatIdsLabel">Chat IDs (comma-separated)</label>
        <input type="text" id="tgChatIds" placeholder="7271353545, 8264620489" />
      </div>

      <div class="toggle-row" style="margin-top:18px;">
        <div class="toggle" id="dcToggle" onclick="toggleDiscord()"></div>
        <span class="toggle-label" data-i18n="enableDiscord">Enable Discord webhook alerts</span>
      </div>
      <div class="dc-fields" id="dcFields">
        <label data-i18n="discordWebhookLabel">Webhook URLs (comma or newline separated)</label>
        <textarea id="dcWebhookUrls" placeholder="https://discord.com/api/webhooks/..."></textarea>
        <div class="hint" style="margin-bottom:0;margin-top:8px;font-size:11px;" data-i18n="discordHint">
          Rich embeds are sent for startup reports, sales, reviews, wishlists, and player spikes.
        </div>
      </div>
    </div>
  </div>

  <!-- Step 3: Preferences -->
  <div class="step-panel" data-step="3">
    <div class="card">
      <h2 data-i18n="prefsTitle">Preferences</h2>
      <div class="hint" data-i18n="prefsHint">Customize the look and feel of your dashboard.</div>

      <label data-i18n="languageLabel">Language</label>
      <div class="lang-grid">
        <div class="lang-option selected" data-lang="en" onclick="selectLang('en')">English</div>
      </div>

      <label style="margin-top:20px;" data-i18n="accentLabel">Accent Color</label>
      <div class="accent-grid">
        <div class="accent-swatch selected" data-accent="steam" onclick="selectAccent('steam')" style="background: linear-gradient(135deg, #66c0f4, #2a475e);" title="Steam Blue"></div>
        <div class="accent-swatch" data-accent="emerald" onclick="selectAccent('emerald')" style="background: linear-gradient(135deg, #5c7e10, #3d5a0a);" title="Emerald"></div>
        <div class="accent-swatch" data-accent="amber" onclick="selectAccent('amber')" style="background: linear-gradient(135deg, #c9a84c, #8a7434);" title="Amber"></div>
        <div class="accent-swatch" data-accent="coral" onclick="selectAccent('coral')" style="background: linear-gradient(135deg, #c45a5a, #8a3434);" title="Coral"></div>
        <div class="accent-swatch" data-accent="violet" onclick="selectAccent('violet')" style="background: linear-gradient(135deg, #7a5aaa, #4a3a6a);" title="Violet"></div>
      </div>

      <label style="margin-top:20px;" data-i18n="portLabel">Port</label>
      <input type="number" id="portInput" value="{{PORT}}" min="1024" max="65535" />
    </div>
  </div>

  <!-- Step 4: Confirm -->
  <div class="step-panel" data-step="4">
    <div class="card" style="text-align:center;">
      <h2 data-i18n="readyTitle">Ready to Go</h2>
      <div class="hint" style="margin-bottom:8px;" data-i18n="readyHint">
        Your dashboard will start collecting data immediately after setup.
        The first data collection may take a few minutes depending on how many days since launch.
      </div>
      <div id="setupSummary" style="text-align:left;font-family:var(--font-mono);font-size:13px;color:var(--steam-text-dim);margin:20px 0;padding:16px;background:rgba(0,0,0,0.3);border-radius:4px;border:1px solid #2a475e;"></div>
    </div>
  </div>

  <div class="nav-buttons">
    <button class="nav-btn prev" id="prevBtn" onclick="prevStep()" style="visibility:hidden;" data-i18n="btnBack">Back</button>
    <button class="nav-btn next" id="nextBtn" onclick="nextStep()" data-i18n="btnNext">Next</button>
  </div>
</div>

<script>
(function() {
  var browserLang = (navigator.language || '').startsWith('ko') ? 'ko' : 'en';
  var currentLang = localStorage.getItem('dashLang') || browserLang;

  var i18n = {
    ko: {
      setupTitle: 'Steam \\ub300\\uc2dc\\ubcf4\\ub4dc \\uc124\\uc815',
      setupDesc: 'Steam \\uac8c\\uc784\\uc758 \\ud310\\ub9e4, \\uc218\\uc775, \\ub9ac\\ubdf0, \\ub3d9\\uc811\\uc790, \\uc704\\uc2dc\\ub9ac\\uc2a4\\ud2b8\\ub97c \\uc2e4\\uc2dc\\uac04\\uc73c\\ub85c \\ubaa8\\ub2c8\\ud130\\ub9c1\\ud569\\ub2c8\\ub2e4.',
      stepWelcome: '\\uc18c\\uac1c', stepConnection: '\\uc5f0\\uacb0', stepTelegram: '\\uc54c\\ub9bc', stepPrefs: '\\uc124\\uc815', stepConfirm: '\\uc2dc\\uc791',
      welcomeTitle: '\\ud658\\uc601\\ud569\\ub2c8\\ub2e4',
      welcomeHint: '\\uc774 \\ub300\\uc2dc\\ubcf4\\ub4dc\\ub294 Steam \\uac8c\\uc784\\uc758 \\ud310\\ub9e4, \\uc218\\uc775, \\ub9ac\\ubdf0, \\ub3d9\\uc2dc \\uc811\\uc18d\\uc790, \\uc704\\uc2dc\\ub9ac\\uc2a4\\ud2b8\\ub97c \\uc2e4\\uc2dc\\uac04\\uc73c\\ub85c \\ucd94\\uc801\\ud569\\ub2c8\\ub2e4. \\ud154\\ub808\\uadf8\\ub78c\\uacfc \\ub514\\uc2a4\\ucf54\\ub4dc \\uc54c\\ub9bc\\ub3c4 \\ubcf4\\ub0bc \\uc218 \\uc788\\uc2b5\\ub2c8\\ub2e4.<br><br>\\ud544\\uc694\\ud55c \\uac83:<br>&bull; <a href="https://steamcommunity.com/dev/apikey" target="_blank">Steam Web API \\ud0a4</a><br>&bull; <a href="https://partner.steampowered.com/" target="_blank">Steamworks Financial API \\ud0a4</a> (Partner \\uc0ac\\uc774\\ud2b8\\uc5d0\\uc11c)<br>&bull; \\uac8c\\uc784\\uc758 App ID',
      connectionTitle: 'Steam \\uc5f0\\uacb0',
      connectionHint: 'API \\ud0a4\\ub97c \\uc785\\ub825\\ud558\\uace0 \\ubaa8\\ub2c8\\ud130\\ub9c1\\ud560 \\uac8c\\uc784\\uc744 \\ucd94\\uac00\\ud558\\uc138\\uc694. \\uc9c4\\ud589 \\uc804\\uc5d0 \\uc5f0\\uacb0\\uc744 \\ud14c\\uc2a4\\ud2b8\\ud574\\uc8fc\\uc138\\uc694.',
      apiKeyPath: '<a href="https://steamcommunity.com/dev/apikey" target="_blank">steamcommunity.com/dev/apikey</a>',
      finKeyPath: 'Steamworks Partner &rarr; Users &amp; Permissions &rarr; Manage Groups &rarr; [\\uadf8\\ub8f9] &rarr; Web API Key',
      gamesTitle: '\\uac8c\\uc784 \\ucd94\\uac00',
      gamesHint: '\\ubaa8\\ub2c8\\ud130\\ub9c1\\ud560 \\uac8c\\uc784\\uc744 \\ucd94\\uac00\\ud558\\uc138\\uc694. \\uac8c\\uc784 \\uc774\\ub984\\uc740 \\uc790\\ub3d9\\uc73c\\ub85c \\uac00\\uc838\\uc635\\ub2c8\\ub2e4.',
      launchDateHint: '\\uac8c\\uc784 \\ucd9c\\uc2dc\\uc77c \\ub610\\ub294 EA \\uc2dc\\uc791\\uc77c. \\uc774 \\ub0a0\\uc9dc\\ubd80\\ud130 \\ud310\\ub9e4 \\ub370\\uc774\\ud130\\ub97c \\uc218\\uc9d1\\ud569\\ub2c8\\ub2e4.',
      wishlistBaselineLabel: '\\uc704\\uc2dc\\ub9ac\\uc2a4\\ud2b8 \\ubca0\\uc774\\uc2a4\\ub77c\\uc778',
      wishlistBaselineHint: '\\ud604\\uc7ac Steamworks \\uc704\\uc2dc\\ub9ac\\uc2a4\\ud2b8 \\ucd1d\\ud569\\uacfc \\ub9de\\ucd94\\uae30 \\uc704\\ud55c \\uc624\\ud504\\uc14b\\uc785\\ub2c8\\ub2e4.',
      addGame: '+ \\uac8c\\uc784 \\ucd94\\uac00',
      testConnection: '\\uc5f0\\uacb0 \\ud14c\\uc2a4\\ud2b8',
      telegramTitle: '\\uc54c\\ub9bc \\ucc44\\ub110',
      telegramHint: '\\uc0c8 \\ud310\\ub9e4, \\ub9ac\\ubdf0, \\uc704\\uc2dc\\ub9ac\\uc2a4\\ud2b8, \\ub3d9\\uc811\\uc790 \\uae09\\uc99d \\uc2dc \\uc989\\uc2dc \\uc54c\\ub9bc\\uc744 \\ubc1b\\uc2b5\\ub2c8\\ub2e4. \\uc120\\ud0dd\\uc0ac\\ud56d\\uc785\\ub2c8\\ub2e4.',
      enableTelegram: '\\ud154\\ub808\\uadf8\\ub78c \\uc54c\\ub9bc \\ud65c\\uc131\\ud654',
      enableDiscord: '\\ub514\\uc2a4\\ucf54\\ub4dc \\uc6f9\\ud6c5 \\uc54c\\ub9bc \\ud65c\\uc131\\ud654',
      discordWebhookLabel: '\\uc6f9\\ud6c5 URL (\\uc27c\\ud45c \\ub610\\ub294 \\uc904\\ubc14\\uafc8 \\uad6c\\ubd84)',
      discordHint: '\\uc2dc\\uc791 \\ubcf4\\uace0, \\ud310\\ub9e4, \\ub9ac\\ubdf0, \\uc704\\uc2dc\\ub9ac\\uc2a4\\ud2b8, \\ub3d9\\uc811\\uc790 \\uae09\\uc99d\\uc5d0 \\ub300\\ud55c \\ub9ac\\uce58 \\uc784\\ubca0\\ub4dc\\ub97c \\ubcf4\\ub0c5\\ub2c8\\ub2e4.',
      botTokenLabel: '\\ubd07 \\ud1a0\\ud070',
      chatIdsLabel: '\\ucc44\\ud305 ID (\\uc27c\\ud45c\\ub85c \\uad6c\\ubd84)',
      prefsTitle: '\\ud658\\uacbd \\uc124\\uc815',
      prefsHint: '\\ub300\\uc2dc\\ubcf4\\ub4dc\\uc758 \\uc678\\uad00\\uc744 \\ucee4\\uc2a4\\ud130\\ub9c8\\uc774\\uc988\\ud558\\uc138\\uc694.',
      languageLabel: '\\uc5b8\\uc5b4',
      accentLabel: '\\uc561\\uc13c\\ud2b8 \\uc0c9\\uc0c1',
      portLabel: '\\ud3ec\\ud2b8',
      readyTitle: '\\uc900\\ube44 \\uc644\\ub8cc',
      readyHint: '\\uc124\\uc815 \\uc644\\ub8cc \\ud6c4 \\uc989\\uc2dc \\ub370\\uc774\\ud130 \\uc218\\uc9d1\\uc744 \\uc2dc\\uc791\\ud569\\ub2c8\\ub2e4. \\ucd9c\\uc2dc\\uc77c \\uc774\\ud6c4 \\uacbd\\uacfc \\uc77c\\uc218\\uc5d0 \\ub530\\ub77c \\uccab \\uc218\\uc9d1\\uc5d0 \\uc218\\ubd84\\uc774 \\uc18c\\uc694\\ub420 \\uc218 \\uc788\\uc2b5\\ub2c8\\ub2e4.',
      btnBack: '\\uc774\\uc804',
      btnNext: '\\ub2e4\\uc74c',
      btnStart: '\\ubaa8\\ub2c8\\ud130\\ub9c1 \\uc2dc\\uc791',
      saving: '\\uc800\\uc7a5 \\uc911...',
      testTesting: '\\ud14c\\uc2a4\\ud2b8 \\uc911...',
      testApiOk: 'Web API \\ud0a4 \\ud655\\uc778',
      testFinOk: 'Financial API \\ud0a4 \\ud655\\uc778',
      testFinFail: 'Financial API \\ud0a4 \\uc624\\ub958 (\\ud310\\ub9e4 \\ub370\\uc774\\ud130 \\uc81c\\uc678)',
      testGameOk: '\\ud655\\uc778',
      testGameFail: '\\uc2e4\\ud328',
      testFillFirst: 'API \\ud0a4\\uc640 App ID\\ub97c \\uba3c\\uc800 \\uc785\\ub825\\ud574\\uc8fc\\uc138\\uc694.',
      testMustPass: '\\uc5f0\\uacb0 \\ud14c\\uc2a4\\ud2b8\\ub97c \\ud1b5\\uacfc\\ud574\\uc57c \\ub2e4\\uc74c\\uc73c\\ub85c \\uc9c4\\ud589\\ud560 \\uc218 \\uc788\\uc2b5\\ub2c8\\ub2e4.',
      addGameAlert: '\\uac8c\\uc784\\uc744 \\ucd5c\\uc18c 1\\uac1c \\ucd94\\uac00\\ud574\\uc8fc\\uc138\\uc694.'
    },
    en: {
      setupTitle: 'Steam Dashboard Setup',
      setupDesc: 'Real-time sales monitoring for your Steam games. Let\\'s get you set up in a few quick steps.',
      stepWelcome: 'INTRO', stepConnection: 'CONNECT', stepTelegram: 'ALERTS', stepPrefs: 'PREFS', stepConfirm: 'GO',
      welcomeTitle: 'Welcome',
      welcomeHint: 'This dashboard tracks your Steam game\\'s sales, revenue, reviews, concurrent players, and wishlists in real-time. It can also send you Telegram and Discord alerts when something happens.<br><br>You\\'ll need:<br>&bull; A <a href="https://steamcommunity.com/dev/apikey" target="_blank">Steam Web API Key</a><br>&bull; A <a href="https://partner.steampowered.com/" target="_blank">Steamworks Financial API Key</a> (from Partner site)<br>&bull; Your game\\'s App ID',
      connectionTitle: 'Steam Connection',
      connectionHint: 'Enter your API keys and add games to monitor. Test the connection before proceeding.',
      apiKeyPath: '<a href="https://steamcommunity.com/dev/apikey" target="_blank">steamcommunity.com/dev/apikey</a>',
      finKeyPath: 'Steamworks Partner &rarr; Users &amp; Permissions &rarr; Manage Groups &rarr; [group] &rarr; Web API Key',
      gamesTitle: 'Your Games',
      gamesHint: 'Add one or more games to monitor. The game name will be fetched automatically.',
      launchDateHint: 'Launch date or EA start date. Sales data is collected from this date.',
      wishlistBaselineLabel: 'Wishlist Baseline',
      wishlistBaselineHint: 'Offset this to match the current Steamworks wishlist total more closely.',
      addGame: '+ Add Another Game',
      testConnection: 'Test Connection',
      telegramTitle: 'Alert Channels',
      telegramHint: 'Get instant notifications for new sales, reviews, wishlists, and player spikes. This is optional.',
      enableTelegram: 'Enable Telegram alerts',
      enableDiscord: 'Enable Discord webhook alerts',
      discordWebhookLabel: 'Webhook URLs (comma or newline separated)',
      discordHint: 'Rich embeds are sent for startup reports, sales, reviews, wishlists, and player spikes.',
      botTokenLabel: 'Bot Token',
      chatIdsLabel: 'Chat IDs (comma-separated)',
      prefsTitle: 'Preferences',
      prefsHint: 'Customize the look and feel of your dashboard.',
      languageLabel: 'Language',
      accentLabel: 'Accent Color',
      portLabel: 'Port',
      readyTitle: 'Ready to Go',
      readyHint: 'Your dashboard will start collecting data immediately after setup. The first data collection may take a few minutes depending on how many days since launch.',
      btnBack: 'Back',
      btnNext: 'Next',
      btnStart: 'Start Monitoring',
      saving: 'Saving...',
      testTesting: 'Testing...',
      testApiOk: 'Web API key verified',
      testFinOk: 'Financial API key verified',
      testFinFail: 'Financial API key error (sales data excluded)',
      testGameOk: 'OK',
      testGameFail: 'Failed',
      testFillFirst: 'Please fill in the API key and at least one App ID first.',
      testMustPass: 'Connection test must pass before proceeding.',
      addGameAlert: 'Please add at least one game.'
    }
  };

  function T(key) { return (i18n[currentLang] || i18n.en)[key] || (i18n.en)[key] || key; }

  function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(function(el) {
      var key = el.getAttribute('data-i18n');
      if (el.tagName === 'INPUT') return;
      el.textContent = T(key);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
      el.innerHTML = T(el.getAttribute('data-i18n-html'));
    });
  }

  var currentStep = 0;
  var totalSteps = 5;
  var selectedLang = currentLang;
  var selectedAccent = 'steam';
  var tgEnabled = false;
  var dcEnabled = false;
  var connectionTested = false;

  // Pre-fill if editing settings
  var existingSettings = {{EXISTING_SETTINGS_JSON}};
  if (existingSettings && existingSettings.steam_api_key) {
    connectionTested = true;
    document.getElementById('steamApiKey').value = existingSettings.steam_api_key || '';
    document.getElementById('steamFinancialKey').value = existingSettings.steam_financial_key || '';
    var tg = existingSettings.telegram || {};
    if (tg.enabled) {
      tgEnabled = true;
      document.getElementById('tgToggle').classList.add('on');
      document.getElementById('tgFields').classList.add('visible');
      document.getElementById('tgBotToken').value = tg.bot_token || '';
      document.getElementById('tgChatIds').value = (tg.chat_ids || []).join(', ');
    }
    var dc = existingSettings.discord || {};
    if (dc.enabled) {
      dcEnabled = true;
      document.getElementById('dcToggle').classList.add('on');
      document.getElementById('dcFields').classList.add('visible');
      document.getElementById('dcWebhookUrls').value = (dc.webhook_urls || []).join('\\n');
    }
    var dash = existingSettings.dashboard || {};
    selectedLang = dash.language || currentLang;
    currentLang = selectedLang;
    selectedAccent = dash.accent || 'steam';
    if (dash.port) document.getElementById('portInput').value = dash.port;
  }

  // Initialize games list
  var games = (existingSettings && existingSettings.games && existingSettings.games.length > 0)
    ? existingSettings.games
    : [{ app_id: '', name: '', launch_date: '', wishlist_baseline: 0 }];

  function renderGames() {
    var container = document.getElementById('gamesList');
    container.innerHTML = '';
    games.forEach(function(g, i) {
      var div = document.createElement('div');
      div.className = 'game-item';
      div.innerHTML =
        '<div class="field"><label>App ID</label><input type="text" value="' + (g.app_id || '') + '" onchange="updateGame(' + i + ',\\'app_id\\',this.value)" placeholder="4451370" /></div>' +
        '<div class="field"><label>Launch Date</label><input type="date" value="' + (g.launch_date || '') + '" onchange="updateGame(' + i + ',\\'launch_date\\',this.value)" /><div class="field-hint" data-i18n="launchDateHint">' + T('launchDateHint') + '</div></div>' +
        '<div class="field"><label data-i18n="wishlistBaselineLabel">' + T('wishlistBaselineLabel') + '</label><input type="number" min="0" value="' + (g.wishlist_baseline || 0) + '" onchange="updateGame(' + i + ',\\'wishlist_baseline\\',this.value)" /><div class="field-hint" data-i18n="wishlistBaselineHint">' + T('wishlistBaselineHint') + '</div></div>' +
        '<div class="game-status" id="gameStatus' + i + '"></div>' +
        (games.length > 1 ? '<button class="remove-btn" onclick="removeGame(' + i + ')">X</button>' : '');
      container.appendChild(div);
    });
  }

  window.addGameRow = function() {
    games.push({ app_id: '', name: '', launch_date: '', wishlist_baseline: 0 });
    connectionTested = false;
    renderGames();
  };

  window.removeGame = function(i) {
    games.splice(i, 1);
    connectionTested = false;
    renderGames();
  };

  window.updateGame = function(i, field, value) {
    games[i][field] = value;
    connectionTested = false;
  };

  renderGames();

  window.toggleTelegram = function() {
    tgEnabled = !tgEnabled;
    var el = document.getElementById('tgToggle');
    var fields = document.getElementById('tgFields');
    if (tgEnabled) {
      el.classList.add('on');
      fields.classList.add('visible');
    } else {
      el.classList.remove('on');
      fields.classList.remove('visible');
    }
  };

  window.toggleDiscord = function() {
    dcEnabled = !dcEnabled;
    var el = document.getElementById('dcToggle');
    var fields = document.getElementById('dcFields');
    if (dcEnabled) {
      el.classList.add('on');
      fields.classList.add('visible');
    } else {
      el.classList.remove('on');
      fields.classList.remove('visible');
    }
  };

  window.selectLang = function(lang) {
    selectedLang = lang;
    currentLang = lang;
    localStorage.setItem('dashLang', lang);
    document.querySelectorAll('.lang-option').forEach(function(el) {
      el.classList.toggle('selected', el.getAttribute('data-lang') === lang);
    });
    applyI18n();
  };

  window.selectAccent = function(accent) {
    selectedAccent = accent;
    document.querySelectorAll('.accent-swatch').forEach(function(el) {
      el.classList.toggle('selected', el.getAttribute('data-accent') === accent);
    });
  };

  window.testConnection = function() {
    var apiKey = document.getElementById('steamApiKey').value.trim();
    var financialKey = document.getElementById('steamFinancialKey').value.trim();
    var validGames = games.filter(function(g) { return g.app_id; });
    var resultEl = document.getElementById('testResult');
    var testBtn = document.getElementById('testBtn');

    if (!apiKey || !validGames.length) {
      resultEl.className = 'test-result error';
      resultEl.textContent = T('testFillFirst');
      return;
    }

    testBtn.disabled = true;
    resultEl.className = 'test-result';
    resultEl.style.display = 'block';
    resultEl.style.background = 'rgba(102,192,244,0.1)';
    resultEl.style.borderColor = 'rgba(102,192,244,0.3)';
    resultEl.style.color = '#66c0f4';
    resultEl.textContent = T('testTesting');

    // Reset statuses
    document.getElementById('apiKeyStatus').className = 'key-status pending';
    document.getElementById('apiKeyStatus').textContent = '';
    document.getElementById('finKeyStatus').className = 'key-status pending';
    document.getElementById('finKeyStatus').textContent = '';
    for (var k = 0; k < games.length; k++) {
      var gs = document.getElementById('gameStatus' + k);
      if (gs) { gs.textContent = ''; gs.className = 'game-status'; }
    }

    var appIds = validGames.map(function(g) { return g.app_id; }).join(',');
    var url = '/api/test?api_key=' + encodeURIComponent(apiKey) + '&app_ids=' + encodeURIComponent(appIds);
    if (financialKey) url += '&financial_key=' + encodeURIComponent(financialKey);

    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        testBtn.disabled = false;
        var lines = [];

        // API key status
        var apiSt = document.getElementById('apiKeyStatus');
        if (data.api_key_valid) {
          apiSt.className = 'key-status ok';
          apiSt.textContent = '\\u2713';
          lines.push('\\u2713 ' + T('testApiOk'));
        } else {
          apiSt.className = 'key-status fail';
          apiSt.textContent = '\\u2717';
        }

        // Financial key status
        var finSt = document.getElementById('finKeyStatus');
        if (financialKey) {
          if (data.financial_key_valid) {
            finSt.className = 'key-status ok';
            finSt.textContent = '\\u2713';
            lines.push('\\u2713 ' + T('testFinOk'));
          } else {
            finSt.className = 'key-status fail';
            finSt.textContent = '\\u2717';
            lines.push('\\u2717 ' + (data.financial_key_message || T('testFinFail')));
          }
        }

        // Per-game results
        var gameResults = data.games || [];
        var allOk = data.api_key_valid && (!financialKey || data.financial_key_valid);
        for (var j = 0; j < gameResults.length; j++) {
          var gr = gameResults[j];
          var gsEl = document.getElementById('gameStatus' + j);
          if (gr.success) {
            if (gsEl) { gsEl.className = 'game-status ok'; gsEl.textContent = '\\u2713'; }
            lines.push('\\u2713 ' + gr.app_id + (gr.name ? ' (' + gr.name + ')' : ''));
            if (gr.name && games[j]) games[j].name = gr.name;
          } else {
            allOk = false;
            if (gsEl) { gsEl.className = 'game-status fail'; gsEl.textContent = '\\u2717'; }
            lines.push('\\u2717 ' + gr.app_id + ': ' + (gr.error || 'Error'));
          }
        }

        if (allOk) {
          resultEl.className = 'test-result success';
          connectionTested = true;
        } else if (data.api_key_valid) {
          resultEl.className = 'test-result partial';
          connectionTested = false;
        } else {
          resultEl.className = 'test-result error';
          connectionTested = false;
        }
        resultEl.innerHTML = lines.join('<br>');
      })
      .catch(function(e) {
        testBtn.disabled = false;
        resultEl.className = 'test-result error';
        resultEl.textContent = 'Network error: ' + e.message;
        connectionTested = false;
      });
  };

  function updateStepDots() {
    document.querySelectorAll('.step-dot').forEach(function(dot, i) {
      dot.classList.toggle('active', i === currentStep);
      dot.classList.toggle('done', i < currentStep);
    });
  }

  function showStep(step) {
    document.querySelectorAll('.step-panel').forEach(function(panel) {
      panel.classList.toggle('active', parseInt(panel.getAttribute('data-step')) === step);
    });
    document.getElementById('prevBtn').style.visibility = step === 0 ? 'hidden' : 'visible';
    var nextBtn = document.getElementById('nextBtn');
    if (step === totalSteps - 1) {
      nextBtn.textContent = T('btnStart');
      nextBtn.className = 'nav-btn start';
    } else {
      nextBtn.textContent = T('btnNext');
      nextBtn.className = 'nav-btn next';
    }
    document.getElementById('prevBtn').textContent = T('btnBack');
    updateStepDots();

    // Build summary on last step
    if (step === totalSteps - 1) {
      var lines = [];
      lines.push('Games: ' + games.filter(function(g){return g.app_id;}).map(function(g){return g.app_id + (g.name ? ' (' + g.name + ')' : '');}).join(', '));
      lines.push('Telegram: ' + (tgEnabled ? 'ON' : 'OFF'));
      lines.push('Discord: ' + (dcEnabled ? 'ON' : 'OFF'));
      lines.push('Accent: ' + selectedAccent);
      lines.push('Language: ' + selectedLang);
      lines.push('Port: ' + document.getElementById('portInput').value);
      document.getElementById('setupSummary').innerHTML = lines.join('<br>');
    }
  }

  window.nextStep = function() {
    // Validate step 1: must pass connection test
    if (currentStep === 1 && !connectionTested) {
      var resultEl = document.getElementById('testResult');
      resultEl.className = 'test-result error';
      resultEl.textContent = T('testMustPass');
      return;
    }

    if (currentStep === totalSteps - 1) {
      submitSetup();
      return;
    }
    currentStep++;
    showStep(currentStep);
  };

  window.prevStep = function() {
    if (currentStep > 0) {
      currentStep--;
      showStep(currentStep);
    }
  };

  // Clicking step dots
  document.querySelectorAll('.step-dot').forEach(function(dot) {
    dot.addEventListener('click', function() {
      var step = parseInt(this.getAttribute('data-step'));
      if (step <= currentStep + 1) {
        if (currentStep === 1 && step > 1 && !connectionTested) return;
        currentStep = step;
        showStep(currentStep);
      }
    });
  });

  function submitSetup() {
    var validGames = games.filter(function(g) { return g.app_id; });
    if (!validGames.length) {
      alert(T('addGameAlert'));
      return;
    }

    var chatIdsStr = document.getElementById('tgChatIds').value.trim();
    var chatIds = chatIdsStr ? chatIdsStr.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];
    var webhookUrlsStr = document.getElementById('dcWebhookUrls').value.trim();
    var webhookUrls = webhookUrlsStr ? webhookUrlsStr.split(/\\r?\\n|,/).map(function(s) { return s.trim(); }).filter(Boolean) : [];

    var payload = {
      steam_api_key: document.getElementById('steamApiKey').value.trim(),
      steam_financial_key: document.getElementById('steamFinancialKey').value.trim(),
      games: validGames,
      telegram: {
        enabled: tgEnabled,
        bot_token: document.getElementById('tgBotToken').value.trim(),
        chat_ids: chatIds
      },
      discord: {
        enabled: dcEnabled,
        webhook_urls: webhookUrls
      },
      dashboard: {
        port: parseInt(document.getElementById('portInput').value) || 8081,
        poll_interval: 300,
        language: selectedLang,
        theme: 'dark',
        accent: selectedAccent
      }
    };

    var nextBtn = document.getElementById('nextBtn');
    nextBtn.disabled = true;
    nextBtn.textContent = T('saving');

    var endpoint = existingSettings && existingSettings.steam_api_key ? '/api/settings' : '/api/setup';

    fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (data.success) {
        window.location.href = '/';
      } else {
        nextBtn.disabled = false;
        nextBtn.textContent = T('btnStart');
        alert('Error: ' + (data.error || 'Unknown'));
      }
    }).catch(function(e) {
      nextBtn.disabled = false;
      nextBtn.textContent = T('btnStart');
      alert('Network error: ' + e.message);
    });
  }

  selectLang(selectedLang);
  selectAccent(selectedAccent);
  showStep(currentStep);
  applyI18n();
})();
</script>
</body>
</html>'''


# ========== DASHBOARD HTML ==========

DASHBOARD_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="{{LANGUAGE}}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTIiIGZpbGw9IiMxNzFhMjEiLz4KICA8cmVjdCB4PSIxMCIgeT0iMjIiIHdpZHRoPSI0NCIgaGVpZ2h0PSIzMiIgcng9IjMiIGZpbGw9IiMxYjI4MzgiIG9wYWNpdHk9IjAuNiIvPgogIDxyZWN0IHg9IjE0IiB5PSI0MCIgd2lkdGg9IjYiIGhlaWdodD0iMTIiIHJ4PSIxIiBmaWxsPSIjMmE0NzVlIi8+CiAgPHJlY3QgeD0iMjIiIHk9IjM0IiB3aWR0aD0iNiIgaGVpZ2h0PSIxOCIgcng9IjEiIGZpbGw9IiMzZDZjOGUiLz4KICA8cmVjdCB4PSIzMCIgeT0iMjgiIHdpZHRoPSI2IiBoZWlnaHQ9IjI0IiByeD0iMSIgZmlsbD0iIzY2YzBmNCIvPgogIDxyZWN0IHg9IjM4IiB5PSIzMiIgd2lkdGg9IjYiIGhlaWdodD0iMjAiIHJ4PSIxIiBmaWxsPSIjNjZjMGY0Ii8+CiAgPHJlY3QgeD0iNDYiIHk9IjI0IiB3aWR0aD0iNiIgaGVpZ2h0PSIyOCIgcng9IjEiIGZpbGw9IiM2NmMwZjQiLz4KICA8cG9seWxpbmUgcG9pbnRzPSIxNywzOCAyNSwzMiAzMywyNiA0MSwzMCA0OSwyMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjYTRkMDA3IiBzdHJva2Utd2lkdGg9IjIuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgPGNpcmNsZSBjeD0iMTciIGN5PSIzOCIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iMzMiIGN5PSIyNiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+CiAgPGNpcmNsZSBjeD0iNDkiIGN5PSIyMiIgcj0iMi41IiBmaWxsPSIjYTRkMDA3Ii8+Cjwvc3ZnPg==">
<title>Steam Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&family=Noto+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --font-body: 'Noto Sans KR', 'Noto Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --radius-sm: 4px;
  --radius-md: 4px;
  --ease-out: cubic-bezier(0.25, 0.46, 0.45, 0.94);
}

/* ---- ACCENT COLORS ---- */
:root[data-accent="steam"] {
  --accent: #66c0f4; --accent-dim: #2a475e;
  --accent-glow: rgba(102,192,244,0.2); --accent-fill: rgba(102,192,244,0.08);
}
:root[data-accent="emerald"] {
  --accent: #5c7e10; --accent-dim: #3d5a0a;
  --accent-glow: rgba(92,126,16,0.2); --accent-fill: rgba(92,126,16,0.08);
}
:root[data-accent="amber"] {
  --accent: #c9a84c; --accent-dim: #8a7434;
  --accent-glow: rgba(201,168,76,0.2); --accent-fill: rgba(201,168,76,0.08);
}
:root[data-accent="coral"] {
  --accent: #c45a5a; --accent-dim: #8a3434;
  --accent-glow: rgba(196,90,90,0.2); --accent-fill: rgba(196,90,90,0.08);
}
:root[data-accent="violet"] {
  --accent: #7a5aaa; --accent-dim: #4a3a6a;
  --accent-glow: rgba(122,90,170,0.2); --accent-fill: rgba(122,90,170,0.08);
}

/* ---- DARK THEME (Steam native) ---- */
:root[data-theme="dark"] {
  --bg-black: #171a21; --bg-deep: #1b2838; --bg-mid: #16202d;
  --bg-surface: #1b2838; --bg-elevated: #2a475e;
  --border-color: #2a475e; --border-light: #3d6c8e;
  --text-primary: #c7d5e0; --text-secondary: #8f98a0;
  --text-tertiary: #556772; --text-accent: #c7d5e0;
  --gold: #66c0f4; --gold-bright: #ffffff; --gold-dim: #2a475e;
  --gold-fill: rgba(102,192,244,0.08);
  --green: #5c7e10; --green-bright: #a4d007; --green-dim: #3d5a0a;
  --green-fill: rgba(92,126,16,0.08);
  --red: #c45a5a; --purple: #66c0f4; --purple-fill: rgba(102,192,244,0.08);
  --chart-grid: rgba(42,71,94,0.4); --chart-tick: #556772; --chart-legend: #8f98a0;
  --tooltip-bg: rgba(22,32,45,0.97); --tooltip-border: rgba(102,192,244,0.2);
  --status-bg: rgba(23,26,33,0.95);
  --header-bg: linear-gradient(165deg, #171a21 0%, #1b2838 100%);
  --header-glow: rgba(102,192,244,0.06);
  --shimmer-a: #16202d; --shimmer-b: #2a475e;
  --review-hover: rgba(42,71,94,0.15);
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--font-body);
  background: var(--bg-black);
  color: var(--text-primary);
  min-height: 100vh;
  overflow-x: hidden;
}

.header {
  position: relative;
  background: var(--header-bg);
  padding: 24px 32px;
  display: flex; align-items: center; gap: 24px;
  border-bottom: 1px solid var(--border-color);
  overflow: hidden;
}
.header::before {
  content: '';
  position: absolute; top: -50%; right: -10%;
  width: 400px; height: 400px;
  background: radial-gradient(circle, var(--header-glow) 0%, transparent 70%);
  pointer-events: none;
}
.header-img {
  width: 184px; border-radius: var(--radius-sm);
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  flex-shrink: 0;
}
.header-info { flex: 1; min-width: 0; }
.header-info h1 {
  font-family: var(--font-body); font-size: 28px; font-weight: 700;
  color: #ffffff; letter-spacing: -0.01em; margin-bottom: 4px;
}
.header-info .subtitle { font-size: 13px; color: var(--text-tertiary); margin-bottom: 10px; }
.header-info .price-badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: linear-gradient(135deg, rgba(164,208,7,0.15), rgba(92,126,16,0.1));
  border: 1px solid rgba(164,208,7,0.3);
  color: var(--green-bright); padding: 5px 14px; border-radius: var(--radius-sm);
  font-family: var(--font-mono); font-size: 13px; font-weight: 500;
}
.header-controls {
  margin-left: auto; text-align: right; flex-shrink: 0;
  display: flex; flex-direction: column; align-items: flex-end; gap: 6px;
}
.live-indicator {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--green-bright);
}
.live-dot {
  width: 7px; height: 7px; background: var(--green-bright);
  border-radius: 50%; box-shadow: 0 0 8px rgba(164,208,7,0.5);
  animation: livePulse 2.5s ease-in-out infinite;
}
@keyframes livePulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(164,208,7,0.5); }
  50% { opacity: 0.4; box-shadow: 0 0 4px rgba(164,208,7,0.2); }
}
.update-time { font-size: 12px; color: var(--text-tertiary); font-family: var(--font-mono); }
.poll-info { font-size: 11px; color: var(--text-tertiary); opacity: 0.6; }
.header-buttons {
  display: flex; gap: 6px; align-items: center; margin-top: 4px;
}
.lang-toggle {
  display: inline-flex; gap: 0; border-radius: 2px; overflow: hidden;
  border: 1px solid var(--border-color); font-size: 11px;
}
.lang-toggle button {
  background: transparent; border: none; color: var(--text-tertiary);
  padding: 3px 8px; cursor: pointer; font-family: var(--font-mono);
  font-size: 11px; transition: all 0.2s;
}
.lang-toggle button.active { background: var(--bg-elevated); color: #ffffff; }
.settings-btn {
  background: transparent; border: 1px solid var(--border-color);
  color: var(--text-tertiary); padding: 3px 8px; border-radius: 2px;
  cursor: pointer; font-size: 13px; transition: all 0.2s;
  text-decoration: none; display: inline-flex; align-items: center;
}
.settings-btn:hover { border-color: var(--border-light); color: var(--text-secondary); }
.game-selector {
  display: none; margin-top: 4px;
}
.game-selector.visible { display: flex; gap: 6px; flex-wrap: wrap; }
.game-tab {
  padding: 4px 12px; border-radius: 2px; font-size: 12px;
  font-family: var(--font-mono); cursor: pointer;
  border: 1px solid var(--border-color); background: transparent;
  color: var(--text-tertiary); transition: all 0.2s;
}
.game-tab.active {
  background: var(--bg-elevated); color: #ffffff;
  border-color: var(--border-light);
}

.dashboard { max-width: 1400px; margin: 0 auto; padding: 24px 24px 48px; }

.metrics-grid {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 12px; margin-bottom: 12px;
}
.metric-card {
  position: relative;
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 20px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
  overflow: hidden;
}
.metric-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.metric-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.metric-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-tertiary); margin-bottom: 8px;
}
.metric-value {
  font-family: var(--font-mono); font-size: 30px; font-weight: 700;
  color: var(--text-primary); line-height: 1.1; letter-spacing: -0.02em;
}
.metric-value.gold { color: var(--accent); }
.metric-value.green { color: var(--green-bright); }
.metric-sub {
  font-size: 12px; color: var(--text-tertiary); margin-top: 6px;
  font-family: var(--font-mono); font-weight: 400;
}

.charts-grid { display: grid; grid-template-columns: 1fr; gap: 12px; margin-bottom: 12px; }
.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.chart-card {
  position: relative;
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 20px 22px; overflow: hidden;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.chart-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.chart-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.chart-card h3 {
  font-family: var(--font-body); font-size: 16px; font-weight: 600;
  color: #ffffff; margin-bottom: 16px;
}
.chart-card canvas { width: 100% !important; }

.section-header {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 12px; margin-top: 8px;
}
.section-header h2 {
  font-family: var(--font-body); font-size: 18px; font-weight: 600;
  color: #ffffff;
}
.section-header::after {
  content: ''; flex: 1; height: 1px;
  background: linear-gradient(90deg, var(--border-color), transparent);
}

.country-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.country-card {
  position: relative;
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 20px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.country-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.country-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
}
.country-card > div { overflow-x: auto; }
.country-card h3 {
  font-family: var(--font-body); font-size: 16px; font-weight: 600;
  color: #ffffff; margin-bottom: 14px;
}
.country-table { width: 100%; border-collapse: collapse; }
.country-table tr {
  border-bottom: 1px solid rgba(42,71,94,0.4); transition: background 0.2s;
}
.country-table tr:hover { background: var(--review-hover); }
.country-table td { padding: 7px 0; font-size: 13px; }
.country-table .cc { font-weight: 600; color: var(--text-secondary); width: 100px; }
.country-table .bar-cell { font-family: var(--font-mono); font-size: 11px; color: var(--accent); letter-spacing: -0.05em; }
.country-table .val { text-align: right; font-family: var(--font-mono); font-weight: 500; color: var(--text-primary); width: 60px; }

.reviews-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
.review-card {
  background: var(--bg-mid);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 18px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}
.review-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.review-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.review-thumb {
  font-size: 18px; width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px; flex-shrink: 0;
}
.review-thumb.up { background: rgba(92,126,16,0.2); }
.review-thumb.down { background: rgba(196,90,90,0.2); }
.review-author { font-weight: 600; font-size: 13px; color: var(--text-secondary); }
.review-playtime { margin-left: auto; font-size: 12px; font-family: var(--font-mono); color: var(--text-tertiary); }
.review-text {
  font-size: 13.5px; line-height: 1.7; color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.status-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  background: var(--status-bg);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  padding: 8px 24px;
  display: flex; align-items: center; gap: 20px;
  font-size: 11px; font-family: var(--font-mono); color: var(--text-tertiary);
  border-top: 1px solid var(--border-color); z-index: 100;
}
.status-bar .dot {
  display: inline-block; width: 6px; height: 6px;
  border-radius: 50%; margin-right: 4px; vertical-align: middle;
}
.status-bar .dot.on { background: var(--green-bright); box-shadow: 0 0 4px rgba(164,208,7,0.4); }
.status-bar .dot.off { background: var(--red); }

@keyframes shimmer {
  0% { background-position: -200px 0; }
  100% { background-position: 200px 0; }
}
.metric-value.loading {
  background: linear-gradient(90deg, var(--shimmer-a) 0%, var(--shimmer-b) 40%, var(--shimmer-a) 80%);
  background-size: 400px 100%; animation: shimmer 1.8s ease-in-out infinite;
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}

.metric-card, .chart-card, .country-card, .review-card { animation: fadeUp 0.5s var(--ease-out) both; }
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
.metrics-grid .metric-card:nth-child(1) { animation-delay: 0.05s; }
.metrics-grid .metric-card:nth-child(2) { animation-delay: 0.1s; }
.metrics-grid .metric-card:nth-child(3) { animation-delay: 0.15s; }
.metrics-grid .metric-card:nth-child(4) { animation-delay: 0.2s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(1) { animation-delay: 0.25s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(2) { animation-delay: 0.3s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(3) { animation-delay: 0.35s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(4) { animation-delay: 0.4s; }

@media (max-width: 1024px) {
  .metrics-grid { grid-template-columns: repeat(2, 1fr); }
  .charts-row { grid-template-columns: 1fr; }
  .country-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .header { padding: 20px 20px; gap: 16px; }
  .header-img { width: 140px; }
  .header-info h1 { font-size: 24px; }
  .dashboard { padding: 20px 16px 72px; }
  .chart-card canvas { min-height: 160px; }
  .country-table .cc { width: 70px; font-size: 12px; }
}
@media (max-width: 640px) {
  .header { flex-direction: column; align-items: flex-start; padding: 16px; gap: 14px; }
  .header-img { width: 100%; max-width: none; height: auto; max-height: 160px; object-fit: cover; border-radius: var(--radius-sm); }
  .header-info h1 { font-size: 22px; }
  .header-controls { margin-left: 0; display: flex; align-items: flex-start; gap: 8px; width: 100%; }
  .poll-info { display: none; }
  .dashboard { padding: 14px 10px 72px; }
  .metrics-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .metric-card { padding: 14px 16px; }
  .metric-value { font-size: 24px; }
  .metric-label { font-size: 10px; }
  .metric-sub { font-size: 11px; }
  .chart-card { padding: 16px 14px; }
  .chart-card canvas { min-height: 150px; }
  .section-header { padding: 0 4px; }
  .section-header h2 { font-size: 16px; }
  .review-card { padding: 14px 16px; }
  .status-bar { padding: 6px 12px; gap: 10px; font-size: 10px; }
  .status-bar span:nth-child(1) { display: none; }
}
@media (max-width: 380px) {
  .metrics-grid { grid-template-columns: 1fr; }
  .header-img { max-height: 120px; }
}
</style>
</head>
<body>
<div class="header">
  <img id="headerImg" class="header-img" src="" alt="" />
  <div class="header-info">
    <h1 id="gameName">Loading...</h1>
    <div class="subtitle" id="gameDev"></div>
    <div class="price-badge" id="gamePrice"></div>
  </div>
  <div class="header-controls">
    <div class="live-indicator"><span class="live-dot"></span>LIVE</div>
    <div class="update-time" id="lastUpdate">--</div>
    <div class="poll-info" data-i18n="pollInfo">5min poll</div>
    <div class="header-buttons">
      <a class="settings-btn" href="/settings" title="Settings">\u2699</a>
    </div>
    <div class="game-selector" id="gameSelector"></div>
  </div>
</div>

<div class="dashboard">
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label" data-i18n="totalSales">Total Sales</div>
      <div class="metric-value gold loading" id="totalSales">--</div>
      <div class="metric-sub" id="salesSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="netRevenue">Net Revenue</div>
      <div class="metric-value green loading" id="netRevenue">--</div>
      <div class="metric-sub" id="revenueSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="playersOnline">Players Online</div>
      <div class="metric-value loading" id="currentPlayers">--</div>
      <div class="metric-sub" id="playerChange"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="peakPlayers">Peak Players</div>
      <div class="metric-value loading" id="peakPlayers">--</div>
      <div class="metric-sub" data-i18n="sessionHigh">Session high</div>
    </div>
  </div>
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label" data-i18n="reviews">Reviews</div>
      <div class="metric-value loading" id="totalReviews">--</div>
      <div class="metric-sub" id="reviewRatio"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="positiveRate">Positive Rate</div>
      <div class="metric-value green loading" id="positiveRate">--</div>
      <div class="metric-sub" id="reviewScore"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="wishlists">Wishlists</div>
      <div class="metric-value loading" id="wishlistNet">--</div>
      <div class="metric-sub" id="wishlistSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="refundRate">Refund Rate</div>
      <div class="metric-value loading" id="refundRate">--</div>
      <div class="metric-sub" data-i18n="refundSales">returns / sales</div>
    </div>
  </div>
  <div class="section-header"><h2 data-i18n="salesPerf">Sales Performance</h2></div>
  <div class="charts-grid">
    <div class="chart-card">
      <h3 data-i18n-html="cumSales">Cumulative Sales &amp; Revenue</h3>
      <canvas id="salesTimelineChart" height="180"></canvas>
    </div>
  </div>
  <div class="charts-row">
    <div class="chart-card">
      <h3 data-i18n-html="dailySales">Daily Sales &amp; Revenue</h3>
      <canvas id="salesChart" height="220"></canvas>
    </div>
    <div class="chart-card">
      <h3 data-i18n="playerActivity">Player Activity</h3>
      <canvas id="playerChart" height="220"></canvas>
    </div>
  </div>
  <div class="section-header"><h2 data-i18n="geoBreakdown">Geographic Breakdown</h2></div>
  <div class="country-grid">
    <div class="country-card">
      <h3 data-i18n="salesByCountryLabel">Sales by Country</h3>
      <div id="salesByCountry"></div>
    </div>
    <div class="country-card">
      <h3 data-i18n="wlByCountry">Wishlists by Country</h3>
      <div id="wishlistByCountry"></div>
    </div>
  </div>
  <div class="section-header"><h2 data-i18n="recentReviews">Recent Reviews</h2></div>
  <div class="reviews-grid" id="recentReviews"></div>
</div>

<div class="status-bar">
  <span>App ID: <span id="statusAppId">{{DEFAULT_APP_ID}}</span></span>
  <span>Poll: {{POLL_INTERVAL}}s</span>
  <span>Telegram: <span class="dot" id="tgDot"></span> <span id="tgStatus"></span></span>
  <span>Discord: <span class="dot" id="dcDot"></span> <span id="dcStatus"></span></span>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
(function() {
  var browserLang = (navigator.language || '').startsWith('ko') ? 'ko' : 'en';
  var rootEl = document.documentElement;
  rootEl.setAttribute('data-theme', '{{THEME}}');
  rootEl.setAttribute('data-accent', '{{ACCENT}}');

  var playerChart, salesChart, salesTimelineChart;
  var curLang = localStorage.getItem('dashLang') || browserLang;
  var currentAppId = '{{DEFAULT_APP_ID}}';
  var allGames = {{GAMES_JSON}};

  // Show game selector if multiple games
  if (allGames.length > 1) {
    var sel = document.getElementById('gameSelector');
    sel.classList.add('visible');
    allGames.forEach(function(g) {
      var btn = document.createElement('button');
      btn.className = 'game-tab' + (g.app_id === currentAppId ? ' active' : '');
      btn.textContent = g.name || g.app_id;
      btn.setAttribute('data-appid', g.app_id);
      btn.onclick = function() { switchGame(g.app_id); };
      sel.appendChild(btn);
    });
  }

  function switchGame(appId) {
    currentAppId = appId;
    document.getElementById('statusAppId').textContent = appId;
    document.querySelectorAll('.game-tab').forEach(function(btn) {
      btn.classList.toggle('active', btn.getAttribute('data-appid') === appId);
    });
    document.querySelectorAll('.metric-value').forEach(function(el) { el.classList.add('loading'); });
    fetchData();
  }

  var i18n = {
    ko: {
      totalSales: '\uCD1D \uD310\uB9E4', netRevenue: '\uC21C\uC218\uC775',
      playersOnline: '\uD604\uC7AC \uB3D9\uC811', peakPlayers: '\uD53C\uD06C \uB3D9\uC811',
      sessionHigh: '\uC138\uC158 \uCD5C\uACE0\uCE58', reviews: '\uB9AC\uBDF0',
      positiveRate: '\uAE0D\uC815\uB960', wishlists: '\uC704\uC2DC\uB9AC\uC2A4\uD2B8',
      refundRate: '\uD658\uBD88\uB960', refundSales: '\uD658\uBD88 / \uD310\uB9E4',
      salesPerf: '\uD310\uB9E4 \uD604\uD669',
      cumSales: '\uB204\uC801 \uD310\uB9E4 &amp; \uC218\uC775',
      dailySales: '\uC77C\uBCC4 \uD310\uB9E4 &amp; \uC218\uC775',
      playerActivity: '\uB3D9\uC811\uC790 \uCD94\uC774',
      geoBreakdown: '\uAD6D\uAC00\uBCC4 \uD604\uD669',
      salesByCountryLabel: '\uAD6D\uAC00\uBCC4 \uD310\uB9E4',
      wlByCountry: '\uAD6D\uAC00\uBCC4 \uC704\uC2DC\uB9AC\uC2A4\uD2B8',
      recentReviews: '\uCD5C\uADFC \uB9AC\uBDF0',
      pollInfo: '5\uBD84 \uD3F4\uB9C1 \u00B7 30\uCD08 \uAC31\uC2E0',
      collecting: '\uB370\uC774\uD130 \uC218\uC9D1 \uC911...',
      noChange: '\u2014 \uBCC0\uB3D9 \uC5C6\uC74C',
      refunds: '\uD658\uBD88', grossLabel: '\uCD1D\uB9E4\uCD9C',
      beforeFees: '\uC218\uC218\uB8CC \uC804', conversion: '\uAD6C\uB9E4\uC804\uD658',
      hours: '\uC2DC\uAC04', unitSuffix: '\uAC74',
      chartCumSales: '\uB204\uC801 \uD310\uB9E4 (\uAC74)',
      chartCumRev: '\uB204\uC801 \uC21C\uC218\uC775 ($)',
      chartSales: '\uD310\uB9E4 (\uAC74)', chartRefunds: '\uD658\uBD88',
      chartNetRev: '\uC21C\uC218\uC775 ($)', chartUnits: '\uAC74\uC218',
      chartRevenue: '\uC218\uC775 ($)', chartPlayers: '\uB3D9\uC811',
      chartSalesAxis: '\uD310\uB9E4 (\uAC74)', chartRevenueAxis: '\uC218\uC775 ($)'
    },
    en: {
      totalSales: 'Total Sales', netRevenue: 'Net Revenue',
      playersOnline: 'Players Online', peakPlayers: 'Peak Players',
      sessionHigh: 'Session high', reviews: 'Reviews',
      positiveRate: 'Positive Rate', wishlists: 'Wishlists',
      refundRate: 'Refund Rate', refundSales: 'returns / sales',
      salesPerf: 'Sales Performance',
      cumSales: 'Cumulative Sales &amp; Revenue',
      dailySales: 'Daily Sales &amp; Revenue',
      playerActivity: 'Player Activity',
      geoBreakdown: 'Geographic Breakdown',
      salesByCountryLabel: 'Sales by Country',
      wlByCountry: 'Wishlists by Country',
      recentReviews: 'Recent Reviews',
      pollInfo: '5min poll \u00B7 30s refresh',
      collecting: 'Collecting data...',
      noChange: '\u2014 no change',
      refunds: 'refunds', grossLabel: 'gross',
      beforeFees: 'before fees', conversion: 'conv.',
      hours: 'h', unitSuffix: '',
      chartCumSales: 'Cumulative Sales', chartCumRev: 'Net Revenue ($)',
      chartSales: 'Sales', chartRefunds: 'Refunds',
      chartNetRev: 'Net Revenue ($)', chartUnits: 'Units',
      chartRevenue: 'Revenue ($)', chartPlayers: 'Players',
      chartSalesAxis: 'Sales', chartRevenueAxis: 'Revenue ($)'
    }
  };

  function T(key) { return (i18n[curLang] || i18n.en)[key] || key; }

  function applyStaticLabels() {
    document.querySelectorAll('[data-i18n]').forEach(function(el) {
      el.textContent = T(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
      el.innerHTML = T(el.getAttribute('data-i18n-html'));
    });
  }

  function updateToggleButtons() {
    return;
  }

  window.setLang = function(lang) {
    curLang = lang;
    localStorage.setItem('dashLang', lang);
    applyStaticLabels();
    updateToggleButtons();
    rebuildCharts();
    fetchData();
  };

  function getChartColors() {
    var cs = getComputedStyle(rootEl);
    return {
      gold: cs.getPropertyValue('--accent').trim() || '#66c0f4',
      goldFill: cs.getPropertyValue('--accent-fill').trim() || 'rgba(102,192,244,0.08)',
      green: cs.getPropertyValue('--green-bright').trim() || '#a4d007',
      greenFill: cs.getPropertyValue('--green-fill').trim() || 'rgba(92,126,16,0.08)',
      red: cs.getPropertyValue('--red').trim() || '#c45a5a',
      purple: '#66c0f4',
      purpleFill: 'rgba(102,192,244,0.08)',
      grid: cs.getPropertyValue('--chart-grid').trim() || 'rgba(42,71,94,0.4)',
      tick: cs.getPropertyValue('--chart-tick').trim() || '#556772',
      legend: cs.getPropertyValue('--chart-legend').trim() || '#8f98a0',
      tooltipBg: cs.getPropertyValue('--tooltip-bg').trim() || 'rgba(22,32,45,0.97)',
      tooltipBorder: cs.getPropertyValue('--tooltip-border').trim() || 'rgba(102,192,244,0.2)'
    };
  }

  function rebuildCharts() {
    if (salesTimelineChart) salesTimelineChart.destroy();
    if (salesChart) salesChart.destroy();
    if (playerChart) playerChart.destroy();
    initCharts();
  }

  function initCharts() {
    var cc = getChartColors();
    var isMobile = window.innerWidth <= 768;
    var pr = isMobile ? 2 : 4;
    var phr = isMobile ? 3 : 6;
    var baseScaleX = {
      ticks: { color: cc.tick, maxTicksLimit: 12, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }
    };
    var baseScaleY = {
      ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }, beginAtZero: true
    };
    var baseTooltip = {
      backgroundColor: cc.tooltipBg, borderColor: cc.tooltipBorder, borderWidth: 1,
      titleFont: { family: "'Noto Sans', 'Noto Sans KR'", weight: '600' },
      bodyFont: { family: "'JetBrains Mono'", size: 12 },
      padding: 12, cornerRadius: 4, displayColors: true, boxPadding: 4
    };
    var baseOpts = {
      responsive: true,
      animation: { duration: 500, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false }
    };
    var legendCfg = { display: true, labels: { color: cc.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 12 } } };

    salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
      type: 'line',
      data: { labels: [], datasets: [
        { label: T('chartCumSales'), data: [], borderColor: cc.gold, backgroundColor: cc.goldFill, fill: true, tension: 0.35, pointRadius: pr, pointHoverRadius: phr, pointBackgroundColor: cc.gold, pointBorderColor: 'transparent', borderWidth: 2.5, yAxisID: 'y' },
        { label: T('chartCumRev'), data: [], borderColor: cc.green, backgroundColor: 'transparent', borderDash: [6, 4], tension: 0.35, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', borderWidth: 2, yAxisID: 'y1' }
      ]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: legendCfg, tooltip: baseTooltip },
        scales: {
          x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
          y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: T('chartSalesAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } }),
          y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } })
        }
      })
    });

    salesChart = new Chart(document.getElementById('salesChart'), {
      type: 'bar',
      data: { labels: [], datasets: [
        { label: T('chartSales'), data: [], backgroundColor: cc.gold, borderRadius: 2, yAxisID: 'y', order: 2, barPercentage: 0.7 },
        { label: T('chartRefunds'), data: [], backgroundColor: cc.red, borderRadius: 2, yAxisID: 'y', order: 3, barPercentage: 0.7 },
        { label: T('chartNetRev'), data: [], type: 'line', borderColor: cc.green, backgroundColor: 'transparent', borderWidth: 2, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
      ]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: legendCfg, tooltip: baseTooltip },
        scales: {
          x: baseScaleX,
          y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: T('chartUnits'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } }),
          y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } })
        }
      })
    });

    playerChart = new Chart(document.getElementById('playerChart'), {
      type: 'line',
      data: { labels: [], datasets: [{
        label: T('chartPlayers'), data: [],
        borderColor: cc.purple, backgroundColor: cc.purpleFill,
        fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5, pointHoverRadius: isMobile ? 2 : 4,
        pointBackgroundColor: cc.purple, pointBorderColor: 'transparent', borderWidth: 2
      }]},
      options: Object.assign({}, baseOpts, {
        plugins: { legend: { display: false }, tooltip: baseTooltip },
        scales: { x: baseScaleX, y: baseScaleY }
      })
    });
  }

  function fetchData() {
    var url = '/api/data?app_id=' + encodeURIComponent(currentAppId);
    fetch(url).then(function(resp) { return resp.json(); }).then(function(data) {
      if (data.app_details) {
        var d = data.app_details;
        document.getElementById('gameName').textContent = d.name || '';
        document.getElementById('gameDev').textContent = (d.developers || []).join(', ') + ' \u00B7 ' + (d.publishers || []).join(', ');
        document.getElementById('headerImg').src = d.header_image || '';
        if (d.price_overview) document.getElementById('gamePrice').textContent = d.price_overview.final_formatted || '';
      }
      document.querySelectorAll('.metric-value.loading').forEach(function(el) { el.classList.remove('loading'); });

      var s = data.sales_totals || {};
      var suffix = T('unitSuffix');
      document.getElementById('totalSales').textContent = (s.units || 0).toLocaleString();
      document.getElementById('salesSub').textContent = T('refunds') + ' ' + (s.returns || 0) + suffix + ' \u00B7 ' + T('grossLabel') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('netRevenue').textContent = '$' + (s.net || 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
      document.getElementById('revenueSub').textContent = T('beforeFees') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('refundRate').textContent = (s.units > 0 ? ((s.returns / s.units) * 100).toFixed(1) : '0') + '%';

      var dailyForCum = (data.daily_sales || []).filter(function(r) { return r[1] !== 0 || r[2] !== 0 || r[4] !== 0; });
      var cumUnits = 0, cumNet = 0;
      var cumLabels = [], cumUnitsData = [], cumNetData = [];
      dailyForCum.forEach(function(r) {
        cumUnits += r[1];
        cumNet += r[4];
        cumLabels.push(r[0].substring(5));
        cumUnitsData.push(cumUnits);
        cumNetData.push(Math.round(cumNet * 100) / 100);
      });
      salesTimelineChart.data.labels = cumLabels;
      salesTimelineChart.data.datasets[0].data = cumUnitsData;
      salesTimelineChart.data.datasets[1].data = cumNetData;
      salesTimelineChart.update('none');

      var dailyRaw = data.daily_sales || [];
      var daily = dailyRaw.filter(function(r) { return r[1] !== 0 || r[2] !== 0 || r[4] !== 0; });
      salesChart.data.labels = daily.map(function(r) { return r[0].substring(5); });
      salesChart.data.datasets[0].data = daily.map(function(r) { return r[1]; });
      salesChart.data.datasets[1].data = daily.map(function(r) { return -r[2]; });
      salesChart.data.datasets[2].data = daily.map(function(r) { return r[4]; });
      salesChart.update('none');

      var players = data.current_players || 0;
      document.getElementById('currentPlayers').textContent = players.toLocaleString();
      document.getElementById('peakPlayers').textContent = (data.peak_players || 0).toLocaleString();

      var hist = data.player_history || [];
      if (hist.length > 1) {
        var prev = hist[hist.length - 2][1];
        var diff = players - prev;
        var el = document.getElementById('playerChange');
        el.textContent = diff > 0 ? '\u25B2 +' + diff : diff < 0 ? '\u25BC ' + diff : T('noChange');
        el.style.color = diff > 0 ? 'var(--green-bright)' : diff < 0 ? 'var(--red)' : 'var(--text-tertiary)';
      }

      playerChart.data.labels = hist.map(function(r) { var d = new Date(r[0]); return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0'); });
      playerChart.data.datasets[0].data = hist.map(function(r) { return r[1]; });
      playerChart.update('none');

      var rev = data.reviews || {};
      var total = rev.total_reviews || 0, pos = rev.total_positive || 0, neg = rev.total_negative || 0;
      document.getElementById('totalReviews').textContent = total;
      document.getElementById('reviewRatio').innerHTML = String.fromCodePoint(0x1F44D) + ' ' + pos + ' / ' + String.fromCodePoint(0x1F44E) + ' ' + neg;
      document.getElementById('positiveRate').textContent = total > 0 ? Math.round(pos/total*100) + '%' : '--';
      document.getElementById('reviewScore').textContent = rev.review_score_desc || '';

      var wl = data.wishlist || {};
      var wlDisplay = (typeof wl.display_total === 'number') ? wl.display_total : (wl.net || 0);
      var wlPrefix = (typeof wl.display_total === 'number') ? '' : '~';
      document.getElementById('wishlistNet').textContent = wlPrefix + wlDisplay.toLocaleString();
      document.getElementById('wishlistSub').textContent = '+' + (wl.adds||0) + ' / -' + (wl.deletes||0) + ' / ' + T('conversion') + ' ' + (wl.purchases||0);

      var esc = function(str) { var d = document.createElement('div'); d.textContent = String(str); return d.innerHTML; };
      var renderCountryTable = function(obj, valFn) {
        var entries = Object.entries(obj).slice(0, 15);
        if (!entries.length) return '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0;">' + T('collecting') + '</div>';
        var maxVal = Math.max(1, valFn(entries[0][1]));
        return '<table class="country-table">' + entries.map(function(entry) {
          var cc = esc(entry[0]); var d = entry[1]; var val = valFn(d);
          var pct = Math.round(val / maxVal * 100);
          return '<tr><td class="cc">' + cc + '</td><td class="bar-cell"><div style="background:linear-gradient(90deg, var(--accent), var(--accent-dim));width:' + pct + '%;height:7px;border-radius:2px;min-width:6px;box-shadow:0 0 6px var(--accent-glow);"></div></td><td class="val">' + val + '</td></tr>';
        }).join('') + '</table>';
      };
      document.getElementById('salesByCountry').innerHTML = renderCountryTable(data.sales_by_country || {}, function(d) { return d.units || 0; });
      document.getElementById('wishlistByCountry').innerHTML = renderCountryTable(data.wishlist_by_country || {}, function(d) { return d.adds || 0; });

      var recent = data.recent_reviews || [];
      document.getElementById('recentReviews').innerHTML = recent.map(function(r) {
        var isUp = r.voted_up;
        var thumb = isUp ? String.fromCodePoint(0x1F44D) : String.fromCodePoint(0x1F44E);
        var thumbClass = isUp ? 'up' : 'down';
        var playtime = Math.round((r.author && r.author.playtime_forever || 0) / 60 * 10) / 10;
        var text = esc(r.review || '');
        return '<div class="review-card"><div class="review-header"><span class="review-thumb ' + thumbClass + '">' + thumb + '</span><span class="review-author">' + esc(r.author && r.author.personaname || 'Anonymous') + '</span><span class="review-playtime">' + playtime + T('hours') + '</span></div><div class="review-text">' + text + '</div></div>';
      }).join('');

      document.getElementById('tgDot').className = 'dot ' + (data.telegram_active ? 'on' : 'off');
      document.getElementById('tgStatus').textContent = data.telegram_active ? 'ON' : 'OFF';
      document.getElementById('dcDot').className = 'dot ' + (data.discord_active ? 'on' : 'off');
      document.getElementById('dcStatus').textContent = data.discord_active ? 'ON' : 'OFF';
      document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
      fetchFailCount = 0;
    }).catch(function(e) { console.error('Fetch error:', e); fetchFailCount++; });
  }

  applyStaticLabels();
  updateToggleButtons();
  initCharts();

  var fetchFailCount = 0;
  function fetchWithBackoff() {
    fetchData();
    var delay = Math.min(30000 * Math.pow(1.5, fetchFailCount), 300000);
    setTimeout(fetchWithBackoff, delay);
  }
  fetchWithBackoff();
})();
</script>
</body>
</html>'''


# ========== HTTP SERVER ==========

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 0:
            return self.rfile.read(length)
        return b''

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _html_response(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path in ('/', '/dashboard'):
            if not has_settings():
                html = SETUP_HTML_TEMPLATE.replace('{{EXISTING_SETTINGS_JSON}}', 'null')
                html = html.replace('{{PORT}}', '8081')
                self._html_response(html)
            else:
                self._html_response(self.server.dashboard_html)

        elif parsed.path == '/settings':
            settings = get_all_settings()
            html = SETUP_HTML_TEMPLATE.replace(
                '{{EXISTING_SETTINGS_JSON}}',
                json.dumps(settings, ensure_ascii=False)
            )
            dash = settings.get('dashboard', {})
            html = html.replace('{{PORT}}', str(dash.get('port', 8081)))
            self._html_response(html)

        elif parsed.path == '/api/test':
            api_key = params.get('api_key', [''])[0]
            financial_key = params.get('financial_key', [''])[0]
            app_ids_raw = params.get('app_ids', [''])[0]
            app_ids = [a.strip() for a in app_ids_raw.split(',') if a.strip()] if app_ids_raw else []

            if not api_key:
                self._json_response({'success': False, 'error': 'Missing api_key'})
                return
            if not app_ids:
                self._json_response({'success': False, 'error': 'Missing app_ids'})
                return

            results = []
            api_key_valid = False
            financial_key_valid = False
            financial_key_status = "missing"
            financial_key_message = ""

            # Test each game with the regular API key
            for app_id in app_ids:
                try:
                    player_data = fetch_json(
                        f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?key={api_key}&appid={app_id}",
                        "test_api"
                    )
                    if player_data and "response" in player_data:
                        api_key_valid = True
                        # Get game name from store API (public, no key needed)
                        details = fetch_json(
                            f"https://store.steampowered.com/api/appdetails?appids={app_id}",
                            "test_details"
                        )
                        name = ""
                        if details and str(app_id) in details and details[str(app_id)].get("success"):
                            name = details[str(app_id)]["data"].get("name", "")
                        results.append({"app_id": app_id, "name": name, "success": True})
                    else:
                        results.append({"app_id": app_id, "name": "", "success": False, "error": "API key invalid or app not found"})
                except Exception as e:
                    results.append({"app_id": app_id, "name": "", "success": False, "error": str(e)})

            # Test financial key if provided
            if financial_key:
                diag_app_id = app_ids[0] if app_ids else None
                fin_diag = diagnose_financial_key(financial_key, diag_app_id)
                financial_key_valid = fin_diag["ok"]
                financial_key_status = fin_diag["status"]
                financial_key_message = fin_diag["message"]

            all_games_ok = all(r["success"] for r in results)
            self._json_response({
                'success': all_games_ok and api_key_valid and (not financial_key or financial_key_valid),
                'api_key_valid': api_key_valid,
                'financial_key_valid': financial_key_valid,
                'financial_key_status': financial_key_status,
                'financial_key_message': financial_key_message,
                'games': results
            })

        elif parsed.path == '/api/data':
            if not has_settings():
                self._json_response({'error': 'Not configured'}, 503)
                return

            settings = get_all_settings()
            games = settings.get('games', [])
            req_app_id = params.get('app_id', [''])[0]

            if not req_app_id and games:
                req_app_id = str(games[0]['app_id'])

            collector = self.server.collector
            gs = collector.get_state(req_app_id)
            game_cfg = get_game_from_settings(settings, req_app_id) or {'wishlist_baseline': 0}
            wishlist_payload = dict(gs.cached_wishlist or {})
            wishlist_payload["baseline"] = parse_int(game_cfg.get("wishlist_baseline", 0), 0)
            wishlist_payload["display_total"] = get_wishlist_display_total(game_cfg, wishlist_payload)

            players = get_current_players(settings['steam_api_key'], req_app_id)
            reviews = get_reviews(req_app_id)
            recent = get_recent_reviews(req_app_id)
            app_details = get_app_details(req_app_id)
            p_history = get_player_history(req_app_id)
            daily = get_all_daily_sales(req_app_id)
            timeline = get_sales_snapshots(req_app_id)
            totals = get_sales_totals(req_app_id)
            wl_history = get_wishlist_history(req_app_id)

            tg = settings.get('telegram', {})
            dc = settings.get('discord', {})

            payload = {
                "current_players": players,
                "peak_players": gs.peak_players,
                "reviews": reviews,
                "recent_reviews": recent,
                "app_details": app_details,
                "player_history": p_history,
                "daily_sales": daily,
                "sales_timeline": timeline,
                "sales_totals": {
                    "units": totals[0], "returns": totals[1],
                    "gross": totals[2], "net": totals[3]
                },
                "wishlist": wishlist_payload,
                "wishlist_history": wl_history,
                "sales_by_country": gs.cached_sales_by_country,
                "wishlist_by_country": gs.cached_wishlist_by_country,
                "telegram_active": telegram_enabled(tg),
                "discord_active": discord_enabled(dc),
                "timestamp": datetime.now().isoformat()
            }
            self._json_response(payload)

        elif parsed.path == '/api/test-alert':
            if not has_settings():
                self._json_response({'success': False, 'error': 'Not configured'}, 503)
                return

            settings = get_all_settings()
            alert_type = params.get('type', [''])[0]
            app_id = params.get('app_id', [''])[0]
            game = get_game_from_settings(settings, app_id)
            if not game:
                self._json_response({'success': False, 'error': 'No configured games found'}, 400)
                return

            success, message = send_test_alert(settings, game, alert_type)
            status = 200 if success else 400
            self._json_response({
                'success': success,
                'message': message,
                'app_id': str(game['app_id']),
                'type': alert_type
            }, status=status)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path in ('/api/setup', '/api/settings'):
            body = self._read_body()
            try:
                data = json.loads(body.decode('utf-8'))
            except Exception:
                self._json_response({'success': False, 'error': 'Invalid JSON'}, 400)
                return

            # Auto-fetch game names for any game missing a name
            games = data.get('games', [])
            for g in games:
                if g.get('app_id') and not g.get('name'):
                    g['name'] = get_game_name_from_api(g['app_id'])

            data['games'] = [normalize_game(g) for g in games]
            save_all_settings(data)

            # Rebuild dashboard HTML
            self.server.dashboard_html = build_dashboard_html()

            # Signal collector to re-read settings on next cycle
            print("[SETTINGS] Updated and saved.")

            self._json_response({'success': True})
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


# ========== HTML BUILDERS ==========

def build_dashboard_html():
    settings = get_all_settings()
    dash = settings.get('dashboard', {})
    games = settings.get('games', [])
    default_app_id = str(games[0]['app_id']) if games else ''

    html = DASHBOARD_HTML_TEMPLATE
    html = html.replace('{{THEME}}', dash.get('theme', 'dark'))
    html = html.replace('{{ACCENT}}', dash.get('accent', 'steam'))
    html = html.replace('{{LANGUAGE}}', dash.get('language', 'en'))
    html = html.replace('{{POLL_INTERVAL}}', str(dash.get('poll_interval', 300)))
    html = html.replace('{{DEFAULT_APP_ID}}', default_app_id)
    html = html.replace('{{GAMES_JSON}}', json.dumps(games, ensure_ascii=False))
    return html


# ========== MAIN ==========

def main():
    init_db()

    port = 8081
    configured = has_settings()

    if configured:
        settings = get_all_settings()
        dash = settings.get('dashboard', {})
        port = dash.get('port', 8081)
        games = settings.get('games', [])
        tg = settings.get('telegram', {})
        dc = settings.get('discord', {})
        tg_on = telegram_enabled(tg)
        dc_on = discord_enabled(dc)
        tg_count = len(tg.get('chat_ids', [])) if tg_on else 0
        dc_count = len(dc.get('webhook_urls', [])) if dc_on else 0

        # Fetch first game name for banner
        game_name = games[0].get('name', games[0]['app_id']) if games else 'No games'
        game_count = len(games)

        print("=" * 50)
        print(f"  Steam Dashboard v{VERSION}")
        print("=" * 50)
        print(f"  Game:       {game_name}" + (f" (+{game_count - 1} more)" if game_count > 1 else ""))
        print(f"  App ID:     {games[0]['app_id']}" if games else "  App ID:     N/A")
        print(f"  Dashboard:  http://localhost:{port}")
        print(f"  Polling:    {dash.get('poll_interval', 300) // 60}min")
        print(f"  Telegram:   {'ON (' + str(tg_count) + ' recipients)' if tg_on else 'OFF'}")
        print(f"  Discord:    {'ON (' + str(dc_count) + ' webhooks)' if dc_on else 'OFF'}")
        print(f"  Theme:      {dash.get('theme', 'dark')} / {dash.get('accent', 'steam')}")
        print(f"  Language:   {dash.get('language', 'en')}")
        print("=" * 50)
    else:
        print("=" * 50)
        print(f"  Steam Dashboard v{VERSION}")
        print("=" * 50)
        print(f"  No config found. Starting setup wizard...")
        print(f"  Open http://localhost:{port} to configure.")
        print("=" * 50)

    # Create collector
    collector = DataCollector()

    # Build HTML
    dashboard_html = build_dashboard_html() if configured else ''

    # Start HTTP server
    server = ReusableHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.collector = collector
    server.dashboard_html = dashboard_html

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"\n[READY] Dashboard at http://localhost:{port}")

    if configured:
        settings = get_all_settings()
        tg = settings.get('telegram', {})
        dc = settings.get('discord', {})
        alerts_on = telegram_enabled(tg) or discord_enabled(dc)

        if alerts_on:
            for game in settings.get('games', []):
                print(f"[INIT] Sending startup report for {game.get('name', game['app_id'])}...")
                send_startup_report(settings, game)

        # Start collector
        collector_thread = threading.Thread(target=collector.loop, daemon=True)
        collector_thread.start()
    else:
        # Wait for setup, then start collector
        def wait_for_setup():
            while not has_settings():
                time.sleep(2)
            print("\n[SETUP] Configuration saved! Starting data collection...")
            server.dashboard_html = build_dashboard_html()
            settings = get_all_settings()
            tg = settings.get('telegram', {})
            dc = settings.get('discord', {})
            alerts_on = telegram_enabled(tg) or discord_enabled(dc)
            if alerts_on:
                for game in settings.get('games', []):
                    send_startup_report(settings, game)
            collector.loop()

        setup_waiter = threading.Thread(target=wait_for_setup, daemon=True)
        setup_waiter.start()

    # Keep main thread alive
    try:
        server_thread.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
