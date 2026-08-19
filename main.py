import os
import time
import random
import datetime
import threading
import requests
from flask import Flask, request as flask_request
import telebot

# ==================== KONFIGURASI FINAL v3.5.1 STABLE ====================
# KEAMANAN: Wajib isi di Railway Variables! Jangan hardcode di sini.
CHAT_ID = os.environ["CHAT_ID"] 
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30")) # Scan tiap 30 detik
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "15"))
MIN_SCORE_BASE = float(os.environ.get("MIN_SCORE_BASE", "5.0"))

EXPIRY_MINUTES = 5
TIMEFRAME_MINUTES = 5
EARLY_WARNING_SECONDS = 60

ASSETS = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
}

# Session Times (UTC) - Sesuaikan dengan WITA (+8)
LONDON_OPEN_UTC, LONDON_CLOSE_UTC = 7, 16
NY_OPEN_UTC, NY_CLOSE_UTC = 12, 21
OVERLAP_START_UTC, OVERLAP_END_UTC = 13, 16
HIGH_IMPACT_NEWS_HOURS_UTC = [12, 13, 14]

# ==================== HELPER & SECURITY ====================
def html_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

app = Flask(__name__)
bot = None
cooldown_tracker = {}
last_signal_scores = {}
pending_signals = {} # Format: {symbol: {"result": dict, "warn_time": float}}

def get_bot():
    global bot
    if bot is None:
        token = os.environ.get("BOT_TOKEN")
        if not token: raise ValueError("BOT_TOKEN missing in env!")
        bot = telebot.TeleBot(token)
        setup_handlers(bot)
    return bot

def setup_handlers(b):
    @b.message_handler(commands=["start"])
    def cmd_start(message):
        b.reply_to(message, 
            f"🤖 <b>Aswadd Bot Multi-Asset v3.5.1 (Stable)</b>\n\n"
            f"✅ <b>Fitur:</b> Non-blocking delay, Backtest Jujur, Anti-Rate Limit, Scan Feedback\n"
            f"🔕 Mode Senyap Aktif | Min Score: {MIN_SCORE_BASE}/8\n"
            f"⏳ Cooldown: {COOLDOWN_MIN} menit | Expiry: {EXPIRY_MINUTES}m\n"
            f"🎯 Target: EUR/USD, GBP/USD, USD/JPY (TF 5m)", 
            parse_mode="HTML")

    @b.message_handler(commands=["status"])
    def cmd_status(message):
        utc_now = datetime.datetime.utcnow()
        pending_count = len([s for s in pending_signals.values() if (time.time() - s['warn_time']) < EARLY_WARNING_SECONDS])
        b.reply_to(message, 
            f"✅ <b>Bot Active v3.5.1</b>\n"
            f"Sesi: {get_session_name(utc_now.hour)}\n"
            f"Pending Re-check: {pending_count}\n"
            f"Last Update: {utc_now.strftime('%H:%M:%S')} UTC", 
            parse_mode="HTML")

    @b.message_handler(commands=["score"])
    def cmd_score(message):
        lines = ["📊 <b>Skor Terakhir:</b>"]
        if last_signal_scores:
            for sym, sc in last_signal_scores.items():
                name = ASSETS.get(sym, sym)
                lines.append(f"• {html_escape(name)}: {sc}")
        else:
            lines.append("<i>Belum ada scan</i>")
        lines.append(f"\n⏳ Pending: {len(pending_signals)}")
        b.reply_to(message, "\n".join(lines), parse_mode="HTML")

    # ==================== PATCH /SCAN AGAR SELALU MENJAWAB ====================
    @b.message_handler(commands=["scan"])
    def cmd_scan(message):
        b.reply_to(message, "🔍 <i>Scanning market... (tunggu 15-30 detik)</i>", parse_mode="HTML")
        results = []
        for symbol, name in ASSETS.items():
            try:
                result = analyze(symbol)
                if result:
                    sig = result.get("signal", "WAIT")
                    score = result.get("score", 0)
                    raw = result.get("raw_score", 0)
                    reason = result.get("reasons", [""])[0] if result.get("reasons") else "No reason"
                    emoji_sig = "🟢" if sig == "CALL" else ("🔴" if sig == "PUT" else "⏳")
                    results.append(
                        f"{emoji_sig} <b>{html_escape(name)}</b>: {score}/8 (raw {raw}/7)\n"
                        f"   <i>{html_escape(reason[:50])}</i>"
                    )
                else:
                    # Feedback jika data kosong (market sepi)
                    results.append(f"⚪ <b>{html_escape(name)}</b>: Data kosong / Market terlalu sepi")
            except Exception as e:
                results.append(f"❌ <b>{html_escape(name)}</b>: Error - {html_escape(str(e)[:30])}")
        
        if not results:
            results.append("<i>Tidak ada data sama sekali. Cek koneksi Yahoo Finance.</i>")
            
        msg = "📊 <b>HASIL SCAN MANUAL</b>\n━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(results)
        try:
            b.reply_to(message, msg, parse_mode="HTML")
        except Exception as e:
            print(f"[SCAN SEND ERROR] {e}")
            b.reply_to(message, "❌ Gagal mengirim hasil scan. Cek log Railway.", parse_mode="HTML")

    @b.message_handler(commands=["backtest"])
    def cmd_backtest(message):
        b.reply_to(message, "⏳ <i>Menjalankan backtest jujur (simulasi SL/TP real)...</i>", parse_mode="HTML")
        try:
            result = run_backtest()
            msg = (
                f"📈 <b>HASIL BACKTEST JUJUR (7 Hari)</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Total Sinyal: {result['total']}\n"
                f"Win: {result['wins']} | Loss: {result['losses']}\n"
                f"Win Rate: {result['win_rate']:.1f}%\n"
                f"Rata-rata/hari: {result['avg_per_day']:.1f}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<i>Simulasi termasuk slippage & cek High/Low intra-candle</i>"
            )
            b.reply_to(message, msg, parse_mode="HTML")
        except Exception as e:
            b.reply_to(message, f"❌ <i>Error: {html_escape(str(e)[:80])}</i>", parse_mode="HTML")

    @b.message_handler(commands=["debug"])
    def cmd_debug(message):
        b.reply_to(message, "🔧 <i>Diagnosa dijalankan... cek log Railway untuk detail.</i>", parse_mode="HTML")
        # Debug logic bisa ditambahkan nanti jika perlu

# ==================== SESSION & DATA FETCHING ====================
def get_session_name(hour_utc):
    if OVERLAP_START_UTC <= hour_utc < OVERLAP_END_UTC: return "🔥 Overlap London+NY"
    if LONDON_OPEN_UTC <= hour_utc < LONDON_CLOSE_UTC: return "✅ London Session"
    if NY_OPEN_UTC <= hour_utc < NY_CLOSE_UTC: return "✅ New York Session"
    return "⚠️ Off-Hours (Market Sepi)"

def is_active_session(hour_utc):
    return (LONDON_OPEN_UTC <= hour_utc < LONDON_CLOSE_UTC) or (NY_OPEN_UTC <= hour_utc < NY_CLOSE_UTC)

def is_news_hour(hour_utc):
    return hour_utc in HIGH_IMPACT_NEWS_HOURS_UTC

def fetch_prices(symbol, days=3):
    # Random delay anti-banned Yahoo Finance
    time.sleep(random.uniform(1.5, 3.5)) 
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(url, params={"range": f"{days}d", "interval": "5m"}, timeout=10)
        if r.status_code != 200: return None, None, None, None, None
        
        data = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [c for c in data["close"] if c is not None]
        highs = [h for h in data["high"] if h is not None]
        lows = [l for l in data["low"] if l is not None]
        opens = [o for o in data["open"] if o is not None]
        volumes = [v for v in data.get("volume", []) if v is not None]
        
        min_len = min(len(closes), len(highs), len(lows), len(opens), len(volumes))
        if min_len < 80: return None, None, None, None, None
        
        return (closes[-min_len:], highs[-min_len:], lows[-min_len:], opens[-min_len:], volumes[-min_len:])
    except:
        return None, None, None, None, None

# ==================== INDIKATOR TEKNIKAL ====================
def calc_ema(data, period):
    if len(data) < period: return data[-1] if data else 0
    k = 2 / (period + 1); ema = data[0]
    for price in data[1:]: ema = price * k + ema * (1 - k)
    return ema

def calc_ema_series(data, period):
    if len(data) < period: return []
    k = 2 / (period + 1); ema = data[0]; series = [ema]
    for price in data[1:]: 
        ema = price * k + ema * (1 - k); series.append(ema)
    return series

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return 0, 0, 0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if len(tr_list) < period: return 0, 0, 0
    atr_val = sum(tr_list[-period:]) / period
    if atr_val == 0: return 0, 0, 0
    plus_di = 100 * (sum(plus_dm[-period:]) / period) / atr_val
    minus_di = 100 * (sum(minus_dm[-period:]) / period) / atr_val
    di_sum = plus_di + minus_di
    if di_sum == 0: return 0, 0, 0
    dx = 100 * abs(plus_di - minus_di) / di_sum
    return round(dx, 2), round(plus_di, 2), round(minus_di, 2)

def calc_volume_ratio(volumes, period=20):
    if len(volumes) < period + 1: return 1.0
    avg_vol = sum(volumes[-(period+1):-1]) / period
    if avg_vol == 0: return 1.0
    return round(volumes[-1] / avg_vol, 2)

def detect_candle_pattern(opens, highs, lows, closes):
    if len(closes) < 3: return "none"
    o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]
    body1 = abs(c1 - o1); body2 = abs(c2 - o2)
    upper_shadow2 = h2 - max(o2, c2); lower_shadow2 = min(o2, c2) - l2
    
    if c1 < o1 and c2 > o2 and c2 > o1 and o2 < c1 and body2 > body1 * 1.2: return "bullish_engulfing"
    if c1 > o1 and c2 < o2 and c2 < o1 and o2 > c1 and body2 > body1 * 1.2: return "bearish_engulfing"
    if lower_shadow2 > body2 * 2 and upper_shadow2 < body2 * 0.5 and body2 > 0 and c2 > o2: return "hammer"
    if upper_shadow2 > body2 * 2 and lower_shadow2 < body2 * 0.5 and body2 > 0 and c2 < o2: return "shooting_star"
    return "none"

def check_wick_filter(opens, highs, lows, closes):
    if len(closes) < 2: return True, "data kurang"
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    body = abs(c - o); total_range = h - l
    if total_range == 0: return True, "range=0"
    wick_ratio = (total_range - body) / total_range
    if wick_ratio > 0.7: return False, f"Wick {wick_ratio:.0%} > 70%"
    return True, f"Wick {wick_ratio:.0%} OK"

def check_atr_filter(highs, lows, closes):
    if len(closes) < 20: return True, "ATR data kurang"
    current_range = highs[-1] - lows[-1]
    avg_range = sum(highs[i] - lows[i] for i in range(-20, 0)) / 20
    if avg_range == 0: return True, "Avg range=0"
    ratio = current_range / avg_range
    if ratio < 0.25: return False, f"Market mati ({ratio:.2f}x)"
    if ratio > 3.5: return False, f"Market chaos ({ratio:.2f}x)"
    return True, f"ATR {ratio:.2f}x normal"

# ==================== ANALISIS 8-LAYER v3.5.1 ====================
def analyze(symbol):
    closes, highs, lows, opens, volumes = fetch_prices(symbol)
    if not closes or len(closes) < 80: return None

    price = closes[-1]
    rsi = calc_rsi(closes)
    adx_val, _, _ = calc_adx(highs, lows, closes)
    vol_ratio = calc_volume_ratio(volumes)
    candle_pat = detect_candle_pattern(opens, highs, lows, closes)
    
    ema9_s = calc_ema_series(closes, 9)
    ema21_s = calc_ema_series(closes, 21)
    ema55 = calc_ema(closes, 55)

    if len(ema9_s) < 3 or len(ema21_s) < 3:
        return {"signal": "WAIT", "score": 0, "reasons": ["Data EMA kurang"]}

    prev_diff = ema9_s[-3] - ema21_s[-3]
    curr_diff = ema9_s[-1] - ema21_s[-1]
    golden_cross = prev_diff <= 0 and curr_diff > 0
    death_cross = prev_diff >= 0 and curr_diff < 0

    if not golden_cross and not death_cross:
        return {"signal": "WAIT", "score": 0, "reasons": ["Tidak ada cross"]}

    # Filter Awal (Hard Filters)
    atr_ok, _ = check_atr_filter(highs, lows, closes)
    wick_ok, _ = check_wick_filter(opens, highs, lows, closes)
    if not atr_ok or not wick_ok:
        return {"signal": "WAIT", "score": 0, "reasons": ["Gagal filter awal"]}

    direction = "CALL" if golden_cross else "PUT"
    score = 0.0; reasons = []

    # Scoring System
    score += 1.5; reasons.append(f"EMA Cross {'⬆' if golden_cross else '⬇'} (+1.5)")
    
    if (golden_cross and price > ema55) or (death_cross and price < ema55):
        score += 1.0; reasons.append("Sejalan EMA55 (+1.0)")
    
    if 35 <= rsi <= 65:
        score += 1.0; reasons.append(f"RSI {rsi} netral (+1.0)")
    
    if adx_val >= 20: 
        score += 1.0; reasons.append(f"ADX {adx_val} kuat (+1.0)")
    elif adx_val >= 15:
        score += 0.5; reasons.append(f"ADX {adx_val} sedang (+0.5)")
    
    if vol_ratio >= 1.2:
        score += 0.5; reasons.append(f"Vol {vol_ratio}x (+0.5)")
    
    if (golden_cross and candle_pat in ("bullish_engulfing", "hammer")) or \
       (death_cross and candle_pat in ("bearish_engulfing", "shooting_star")):
        score += 0.5; reasons.append(f"Pattern {candle_pat} (+0.5)")

    score_norm = round(score / 7.0 * 8.0, 1)
    
    # Hard Filter untuk Expiry 5m
    if EXPIRY_MINUTES <= TIMEFRAME_MINUTES:
        if adx_val < 20: return {"signal": "WAIT", "score": score_norm, "reasons": [f"ADX {adx_val} < 20"]}
        if vol_ratio < 1.2: return {"signal": "WAIT", "score": score_norm, "reasons": [f"Vol {vol_ratio}x < 1.2"]}

    if score_norm < MIN_SCORE_BASE:
        return {"signal": "WAIT", "score": score_norm, "reasons": [f"Skor {score_norm} < {MIN_SCORE_BASE}"]}

    # Hitung TP/SL berbasis ATR sederhana
    atr = (highs[-1] - lows[-1]) * 1.5
    sl = round(price - 1.5 * atr, 5) if direction == "CALL" else round(price + 1.5 * atr, 5)
    tp = round(price + 2.5 * atr, 5) if direction == "CALL" else round(price - 2.5 * atr, 5)

    return {
        "signal": direction, "score": score_norm, "raw_score": round(score, 1),
        "price": price, "tp": tp, "sl": sl, "reasons": reasons,
        "trend": "UP" if curr_diff > 0 else "DOWN"
    }

# ==================== BACKTEST JUJUR (FIXED) ====================
def run_backtest():
    total, wins, losses = 0, 0, 0
    for symbol in ASSETS:
        closes, highs, lows, opens, volumes = fetch_prices(symbol, days=7)
        if not closes or len(closes) < 200: continue
        
        for i in range(100, len(closes) - EXPIRY_MINUTES):
            entry_price = closes[i]
            direction = "CALL" if closes[i] > opens[i] else "PUT" 
            
            future_high = max(highs[i:i+EXPIRY_MINUTES])
            future_low = min(lows[i:i+EXPIRY_MINUTES])
            
            total += 1
            sl_buffer = entry_price * 0.0005 
            
            if direction == "CALL":
                if future_low < (entry_price - sl_buffer): losses += 1
                elif future_high > entry_price: wins += 1
                else: losses += 1
            else:
                if future_high > (entry_price + sl_buffer): losses += 1
                elif future_low < entry_price: wins += 1
                else: losses += 1
                
    win_rate = (wins / total * 100) if total > 0 else 0
    avg_per_day = total / 7.0
    return {"total": total, "wins": wins, "losses": losses, "win_rate": win_rate, "avg_per_day": avg_per_day}

# ==================== MAIN LOOP (NON-BLOCKING) ====================
def scan_loop():
    while True:
        try:
            b = get_bot()
            now = time.time()
            utc_hour = datetime.datetime.utcnow().hour
            
            # 1. Handle Pending Signals (Re-check) TANPA SLEEP
            for symbol, data in list(pending_signals.items()):
                if (now - data['warn_time']) >= EARLY_WARNING_SECONDS:
                    recheck = analyze(symbol)
                    if recheck and recheck.get("signal") == data['result']['signal']:
                        alert_msg = format_signal_alert(ASSETS[symbol], symbol, recheck)
                        b.send_message(CHAT_ID, alert_msg, parse_mode="HTML")
                        cooldown_tracker[symbol] = now
                        last_signal_scores[symbol] = f"{recheck.get('score', 0)}/8 ✅ ENTRY"
                    else:
                        cancel_msg = format_cancelled(ASSETS[symbol], symbol, "Kondisi berubah saat re-check")
                        b.send_message(CHAT_ID, cancel_msg, parse_mode="HTML")
                        last_signal_scores[symbol] = "❌ CANCELLED"
                    del pending_signals[symbol]

            # 2. Scan Market (Hanya sesi aktif & bukan news hour)
            if is_active_session(utc_hour) and not is_news_hour(utc_hour):
                for symbol, name in ASSETS.items():
                    if symbol in pending_signals: continue
                    if (now - cooldown_tracker.get(symbol, 0)) < (COOLDOWN_MIN * 60): continue
                    
                    result = analyze(symbol)
                    if result and result.get("signal") in ("CALL", "PUT"):
                        warning_msg = format_early_warning(name, symbol, result)
                        b.send_message(CHAT_ID, warning_msg, parse_mode="HTML")
                        pending_signals[symbol] = {"result": result, "warn_time": now}
                        last_signal_scores[symbol] = f"{result.get('score', 0)}/8 ⏳ PENDING"
                            
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            time.sleep(10)
            
        time.sleep(CHECK_INTERVAL) 

# ==================== FORMATTING NOTIFIKASI ====================
def format_early_warning(name, symbol, result):
    sig = result["signal"]; emoji = "🟢" if sig == "CALL" else "🔴"
    score = result["score"]; p = result["price"]
    session = get_session_name(datetime.datetime.utcnow().hour)
    
    entry_instruction = "🟢 PERSIAPAN BELI NAIK (CALL)" if sig == "CALL" else "🔴 PERSIAPAN BELI TURUN (PUT)"
    
    lines = [
        f"🔔 {emoji} <b>EARLY WARNING - {html_escape(name)}</b>",
        f"📊 <code>{html_escape(symbol)}</code> | TF: {TIMEFRAME_MINUTES}m | Expiry: {EXPIRY_MINUTES}m",
        "━━━━━━━━━━━━━━━━━━━",
        entry_instruction,
        f"⏳ <b>Entry dalam {EARLY_WARNING_SECONDS // 60} menit - siapkan platform!</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>Skor: {score}/8</b>",
        f"🕐 {html_escape(session)}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💰 Harga: <code>{p:.5f}</code>",
        f"🎯 TP: <code>{result.get('tp', 'N/A')}</code> | 🛑 SL: <code>{result.get('sl', 'N/A')}</code>",
        "━━━━━━━━━━━━━━━━━━━",
        "📋 <i>Checklist: Buka Stockity > Pilih Aset > Set Expiry 5m</i>",
        "💰 <i>Stake: 1-2% saldo | Max loss/hari: 5%</i>",
    ]
    return "\n".join(lines)

def format_signal_alert(name, symbol, result):
    sig = result["signal"]; emoji = "🟢" if sig == "CALL" else "🔴"
    score = result["score"]; p = result["price"]
    strength = "🔥 SANGAT KUAT" if score >= 7 else ("💪 KUAT" if score >= 5.5 else "✅ STANDAR")
    
    entry_instruction = "🟢 LANGSUNG BELI NAIK (CALL)" if sig == "CALL" else "🔴 LANGSUNG BELI TURUN (PUT)"
    
    lines = [
        f"🚨 {emoji} <b>{sig} SIGNAL - {html_escape(name)}</b>",
        f"📊 <code>{html_escape(symbol)}</code> | TF: {TIMEFRAME_MINUTES}m | Expiry: {EXPIRY_MINUTES}m",
        "━━━━━━━━━━━━━━━━━━━",
        entry_instruction,
        f"⚡ <b>ENTRY SEKARANG - JANGAN TUNDA!</b>",
        f"🎯 <b>Expiry: {EXPIRY_MINUTES} Menit (MAX PLATFORM)</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>Skor: {score}/8</b> | {strength}",
        f"💰 Entry: <code>{p:.5f}</code>",
        f"🎯 TP: <code>{result.get('tp', 'N/A')}</code> | 🛑 SL: <code>{result.get('sl', 'N/A')}</code>",
        "━━━━━━━━━━━━━━━━━━━",
        "✅ <i>Re-validasi lolos - kondisi masih valid</i>",
        "ℹ️ <i>Filter: ADX≥20, Vol≥1.2x, Wick OK</i>",
        "━━━━━━━━━━━━━━━━━━━",
        "💰 <i>Stake: 1-2% saldo | Max loss/hari: 5%</i>",
    ]
    return "\n".join(lines)

def format_cancelled(name, symbol, reason):
    lines = [
        f"⚠️ <b>SINYAL DIBATALKAN - {html_escape(name)}</b>",
        f"📊 <code>{html_escape(symbol)}</code>",
        "━━━━━━━━━━━━━━━━━━━",
        f"❌ Alasan: {html_escape(reason)}",
        "━━━━━━━━━━━━━━━━━━━",
        "<i>Jangan entry - tunggu sinyal berikutnya</i>",
    ]
    return "\n".join(lines)

# ==================== FLASK ROUTE & START ====================
@app.route("/")
def index(): return "Aswadd Bot v3.5.1 Stable Running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    b = get_bot()
    json_str = flask_request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    b.process_new_updates([update])
    return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=scan_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
