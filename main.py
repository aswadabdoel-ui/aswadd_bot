import os
import re
import time
import datetime
import threading
import requests
from flask import Flask, request as flask_request
import telebot

# ==================== KONFIGURASI FINAL ====================
CHAT_ID        = os.environ.get("CHAT_ID", "971243017")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
COOLDOWN_MIN   = int(os.environ.get("COOLDOWN_MIN", "10"))
MIN_SCORE_BASE = float(os.environ.get("MIN_SCORE_BASE", "5.0"))

EXPIRY_MINUTES = 5
TIMEFRAME_MINUTES = 5

TIMEZONE_OFFSET_HOURS = 8
TIMEZONE_LABEL = "WITA"

EARLY_WARNING_SECONDS = 60

ASSETS = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
}

LONDON_OPEN_UTC   = 7
LONDON_CLOSE_UTC  = 16
NY_OPEN_UTC       = 12
NY_CLOSE_UTC      = 21
OVERLAP_START_UTC = 13
OVERLAP_END_UTC   = 16

HIGH_IMPACT_NEWS_HOURS_UTC = [12, 13, 14]

# ==================== HELPER ====================
def html_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ==================== FLASK APP ====================
app = Flask(__name__)
bot = None
cooldown_tracker = {}
last_signal_scores = {}
pending_signals = {}

def get_bot():
    global bot
    if bot is None:
        token = os.environ.get("BOT_TOKEN", "")
        if not token or token == "MASUKKAN_TOKEN_DI_RENDER":
            raise ValueError("BOT_TOKEN tidak ditemukan!")
        bot = telebot.TeleBot(token)
        setup_handlers(bot)
    return bot

def setup_handlers(b):
    @b.message_handler(commands=["start"])
    def cmd_start(message):
        asset_list = "\n".join([f"  • {html_escape(v)}" for v in ASSETS.values()])
        b.reply_to(message,
            f"🤖 <b>Aswadd Bot Multi-Asset Final v3.3</b>\n\n"
            f"🔕 <b>MODE SENYAP AKTIF</b>\n"
            f"Bot hanya kirim notifikasi saat ada sinyal:\n"
            f"  🔔 Early Warning (persiapan)\n"
            f"  🚨 Entry Signal (eksekusi)\n"
            f"  ⚠️ Cancelled (jika re-check gagal)\n\n"
            f"<b>Spesifikasi:</b>\n"
            f"• Market:\n{asset_list}\n"
            f"• TF: {TIMEFRAME_MINUTES} Menit\n"
            f"• Expiry: {EXPIRY_MINUTES} Menit (MAX PLATFORM)\n"
            f"• Entry: 1 menit setelah warning + re-check\n"
            f"• Min Score: {MIN_SCORE_BASE}/8 (diperketat)\n"
            f"• Cooldown: {COOLDOWN_MIN} menit/aset\n\n"
            f"<b>Filter Ketat Expiry 5m:</b>\n"
            f"• EMA 9/21/55 + Cross\n"
            f"• RSI(14) Zone 35-65\n"
            f"• MACD(8,17,9)\n"
            f"• Bollinger Bands(20,2)\n"
            f"• ADX(14) ≥ 22\n"
            f"• Volume Spike ≥ 1.3x\n"
            f"• Candlestick Pattern WAJIB\n"
            f"• Wick Filter (max 70% body)\n"
            f"• ATR Filter + News Hours Block\n\n"
            f"<b>Jam Terbaik:</b> 21:00-00:00 {TIMEZONE_LABEL}\n"
            f"(London+NY Overlap)\n\n"
            f"⚠️ <i>Data: Yahoo Finance (delay ~15-30 detik)</i>\n\n"
            f"/status /score /backtest /scan /debug",
            parse_mode="HTML")

    @b.message_handler(commands=["status"])
    def cmd_status(message):
        utc_now = datetime.datetime.utcnow()
        hour = utc_now.hour
        session = get_session_name(hour)
        active_assets = [v for k, v in ASSETS.items()]
        b.reply_to(message,
            f"✅ <b>Bot Aktif — Mode Senyap v3.3</b>\n"
            f"Aset: {html_escape(', '.join(active_assets))}\n"
            f"Min skor: {MIN_SCORE_BASE}/8\n"
            f"Expiry: {EXPIRY_MINUTES}m (fixed)\n"
            f"Early warning: {EARLY_WARNING_SECONDS // 60} menit\n"
            f"Cooldown: {COOLDOWN_MIN} menit/aset\n"
            f"Pending confirm: {len(pending_signals)}\n"
            f"Sesi sekarang: {html_escape(session)}\n"
            f"Update tiap {TIMEFRAME_MINUTES} menit\n\n"
            f"🔕 <i>Tidak ada market update rutin.\nNotifikasi hanya saat sinyal muncul.</i>\n\n"
            f"💡 <i>Gunakan /scan untuk cek kondisi market manual anytime.\nGunakan /debug untuk diagnosa backtest.</i>",
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

    @b.message_handler(commands=["scan"])
    def cmd_scan(message):
        b.reply_to(message, "🔍 <i>Menjalankan scan manual...</i>", parse_mode="HTML")
        results = []
        for symbol, name in ASSETS.items():
            result = analyze(symbol)
            if result:
                sig = result.get("signal", "WAIT")
                score = result.get("score", 0)
                raw = result.get("raw_score", 0)
                reason = result.get("reasons", [""])[0] if result.get("reasons") else ""
                emoji_sig = "🟢" if sig == "CALL" else ("🔴" if sig == "PUT" else "⏳")
                results.append(
                    f"{emoji_sig} <b>{html_escape(name)}</b>: {score}/8 (raw {raw}/7)\n"
                    f"   <i>{html_escape(reason[:50])}</i>"
                )
        if not results:
            results.append("<i>Data tidak tersedia</i>")
        msg = "📊 <b>HASIL SCAN MANUAL</b>\n━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(results)
        b.reply_to(message, msg, parse_mode="HTML")

    @b.message_handler(commands=["backtest"])
    def cmd_backtest(message):
        b.reply_to(message, "⏳ <i>Menjalankan backtest 7 hari terakhir...</i>", parse_mode="HTML")
        result = run_backtest()
        msg = (
            f"📈 <b>HASIL BACKTEST 7 HARI</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Total sinyal: {result['total_signals']}\n"
            f"Win (estimasi): {result['wins']}\n"
            f"Loss (estimasi): {result['losses']}\n"
            f"Win Rate: {result['win_rate']:.1f}%\n"
            f"Rata-rata/hari: {result['avg_per_day']:.1f} sinyal\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Simulasi expiry 5m, filter diperlonggar untuk backtest\n(ADX≥18, Vol≥1.1x, pattern opsional)</i>"
        )
        b.reply_to(message, msg, parse_mode="HTML")

    @b.message_handler(commands=["debug"])
    def cmd_debug(message):
        b.reply_to(message, "🔧 <i>Menjalankan diagnosa...</i>", parse_mode="HTML")
        lines = ["🔧 <b>DIAGNOSA BACKTEST</b>", "━━━━━━━━━━━━━━━━━━"]
        
        for symbol, name in ASSETS.items():
            closes, highs, lows, opens, volumes = fetch_prices(symbol, days=7)
            
            if not closes:
                lines.append(f"\n❌ <b>{html_escape(name)}</b>: Data gagal diambil")
                continue
            
            lines.append(f"\n📊 <b>{html_escape(name)}</b> ({html_escape(symbol)})")
            lines.append(f"   Candle didapat: {len(closes)}")
            
            if len(closes) < 200:
                lines.append(f"   ⚠️ Kurang dari 200 candle → backtest skip")
                lines.append(f"   Harga terakhir: {closes[-1]:.5f}")
                continue
            
            cross_count = 0
            adx_pass = 0
            vol_pass = 0
            both_pass = 0
            
            for i in range(100, len(closes) - 1):
                window_closes = closes[:i+1]
                window_highs = highs[:i+1]
                window_lows = lows[:i+1]
                window_volumes = volumes[:i+1]
                
                ema9_s = calc_ema_series(window_closes, 9)
                ema21_s = calc_ema_series(window_closes, 21)
                if len(ema9_s) < 3 or len(ema21_s) < 3:
                    continue
                
                prev_diff = ema9_s[-3] - ema21_s[-3]
                curr_diff = ema9_s[-1] - ema21_s[-1]
                golden_cross = prev_diff <= 0 and curr_diff > 0
                death_cross = prev_diff >= 0 and curr_diff < 0
                
                if golden_cross or death_cross:
                    cross_count += 1
                    
                    adx_val, _, _ = calc_adx(window_highs, window_lows, window_closes)
                    vol_ratio = calc_volume_ratio(window_volumes)
                    
                    if adx_val >= 18:
                        adx_pass += 1
                    if vol_ratio >= 1.1:
                        vol_pass += 1
                    if adx_val >= 18 and vol_ratio >= 1.1:
                        both_pass += 1
            
            lines.append(f"   EMA Cross terdeteksi: {cross_count}")
            lines.append(f"   Lolos ADX≥18: {adx_pass}")
            lines.append(f"   Lolos Vol≥1.1x: {vol_pass}")
            lines.append(f"   Lolos keduanya: {both_pass}")
            lines.append(f"   Harga terakhir: {closes[-1]:.5f}")
            
            if len(closes) > 0:
                adx_now, _, _ = calc_adx(highs, lows, closes)
                vol_now = calc_volume_ratio(volumes)
                lines.append(f"   ADX sekarang: {adx_now}")
                lines.append(f"   Vol sekarang: {vol_now}x")
        
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append("<i>Jika 'Candle didapat' < 200 → masalah data Yahoo</i>")
        lines.append("<i>Jika 'EMA Cross' = 0 → market sideways 7 hari</i>")
        lines.append("<i>Jika 'Lolos keduanya' = 0 → filter masih terlalu ketat</i>")
        
        b.reply_to(message, "\n".join(lines), parse_mode="HTML")

# ==================== SESSION DETECTION ====================
def get_session_name(hour_utc):
    in_london = LONDON_OPEN_UTC <= hour_utc < LONDON_CLOSE_UTC
    in_ny = NY_OPEN_UTC <= hour_utc < NY_CLOSE_UTC
    in_overlap = OVERLAP_START_UTC <= hour_utc < OVERLAP_END_UTC
    if in_overlap:
        return "🔥 Overlap London+NY (TERBAIK)"
    elif in_london and in_ny:
        return "✅ London + New York"
    elif in_london:
        return "✅ London"
    elif in_ny:
        return "✅ New York"
    else:
        return "⚠️ Off-Hours (market sepi)"

def is_active_session(hour_utc):
    return (LONDON_OPEN_UTC <= hour_utc < LONDON_CLOSE_UTC) or \
           (NY_OPEN_UTC <= hour_utc < NY_CLOSE_UTC)

def is_news_hour(hour_utc):
    return hour_utc in HIGH_IMPACT_NEWS_HOURS_UTC

# ==================== DATA FETCHING ====================
def fetch_prices(symbol, days=3):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"range": f"{days}d", "interval": "5m"}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        result = data["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]
        closes  = [c for c in quotes["close"] if c is not None]
        highs   = [h for h in quotes["high"]  if h is not None]
        lows    = [l for l in quotes["low"]   if l is not None]
        opens   = [o for o in quotes["open"]  if o is not None]
        volumes = [v for v in quotes.get("volume", []) if v is not None]
        min_len = min(len(closes), len(highs), len(lows), len(opens), len(volumes))
        if min_len < 80:
            return None, None, None, None, None
        return (closes[-min_len:], highs[-min_len:], lows[-min_len:],
                opens[-min_len:], volumes[-min_len:])
    except Exception as e:
        print(f"[FETCH ERROR] {symbol}: {e}")
        return None, None, None, None, None

# ==================== INDIKATOR ====================
def calc_ema(data, period):
    if len(data) < period:
        return data[-1] if data else 0
    k = 2 / (period + 1)
    ema = data[0]
    for price in data[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calc_ema_series(data, period):
    if len(data) < period:
        return []
    k = 2 / (period + 1)
    ema = data[0]
    series = [ema]
    for price in data[1:]:
        ema = price * k + ema * (1 - k)
        series.append(ema)
    return series

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:]) / period

def calc_atr_series(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return []
    atr_values = []
    for end in range(period + 1, len(closes) + 1):
        trs = []
        for i in range(end - period, end):
            if i < 1:
                continue
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i] - closes[i-1]))
            trs.append(tr)
        if trs:
            atr_values.append(sum(trs) / len(trs))
    return atr_values

def calc_macd(closes, fast=8, slow=17, signal=9):
    if len(closes) < slow + signal:
        return 0, 0, 0
    ema_fast = calc_ema_series(closes, fast)
    ema_slow = calc_ema_series(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len-i)] - ema_slow[-(min_len-i)] for i in range(min_len)]
    if len(macd_line) < signal:
        return 0, 0, 0
    signal_line = calc_ema_series(macd_line, signal)
    if not signal_line:
        return 0, 0, 0
    return round(macd_line[-1], 8), round(signal_line[-1], 8), round(macd_line[-1] - signal_line[-1], 8)

def calc_bollinger(closes, period=20, std_dev=2):
    if len(closes) < period:
        return 0, 0, 0
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = variance ** 0.5
    return round(sma + std_dev * std, 8), round(sma, 8), round(sma - std_dev * std, 8)

def calc_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return 0, 0, 0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if len(tr_list) < period:
        return 0, 0, 0
    atr_val = sum(tr_list[-period:]) / period
    if atr_val == 0:
        return 0, 0, 0
    plus_di = 100 * (sum(plus_dm[-period:]) / period) / atr_val
    minus_di = 100 * (sum(minus_dm[-period:]) / period) / atr_val
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return 0, 0, 0
    dx = 100 * abs(plus_di - minus_di) / di_sum
    return round(dx, 2), round(plus_di, 2), round(minus_di, 2)

def calc_volume_ratio(volumes, period=20):
    if len(volumes) < period + 1:
        return 1.0
    avg_vol = sum(volumes[-(period+1):-1]) / period
    if avg_vol == 0:
        return 1.0
    return round(volumes[-1] / avg_vol, 2)

def detect_candle_pattern(opens, highs, lows, closes):
    if len(closes) < 3:
        return "none"
    o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]
    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)
    upper_shadow2 = h2 - max(o2, c2)
    lower_shadow2 = min(o2, c2) - l2
    if c1 < o1 and c2 > o2 and c2 > o1 and o2 < c1 and body2 > body1 * 1.2:
        return "bullish_engulfing"
    if c1 > o1 and c2 < o2 and c2 < o1 and o2 > c1 and body2 > body1 * 1.2:
        return "bearish_engulfing"
    if lower_shadow2 > body2 * 2 and upper_shadow2 < body2 * 0.5 and body2 > 0 and c2 > o2:
        return "hammer"
    if upper_shadow2 > body2 * 2 and lower_shadow2 < body2 * 0.5 and body2 > 0 and c2 < o2:
        return "shooting_star"
    return "none"

def check_wick_filter(opens, highs, lows, closes):
    if len(closes) < 2:
        return True, "data kurang"
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    body = abs(c - o)
    total_range = h - l
    if total_range == 0:
        return True, "range=0"
    wick_ratio = (total_range - body) / total_range
    if wick_ratio > 0.7:
        return False, f"Wick {wick_ratio:.0%} > 70% (indecision)"
    return True, f"Wick {wick_ratio:.0%} OK"

# ==================== ANTI-NOISE: ATR FILTER ====================
def check_atr_filter(highs, lows, closes):
    atr_series = calc_atr_series(highs, lows, closes, 14)
    if len(atr_series) < 20:
        return True, "ATR data kurang"
    current_atr = atr_series[-1]
    avg_atr = sum(atr_series[-20:]) / 20
    if avg_atr == 0:
        return True, "ATR avg=0"
    atr_ratio = current_atr / avg_atr
    if atr_ratio < 0.25:
        return False, f"Market mati ({atr_ratio:.2f}x avg)"
    if atr_ratio > 3.5:
        return False, f"Market chaos ({atr_ratio:.2f}x avg)"
    return True, f"ATR {atr_ratio:.2f}x normal"

# ==================== ANALISIS 8-LAYER + FILTER KETAT ====================
def analyze(symbol):
    closes, highs, lows, opens, volumes = fetch_prices(symbol)
    if not closes or len(closes) < 80:
        return None

    price = closes[-1]
    rsi = calc_rsi(closes)
    atr = calc_atr(highs, lows, closes)
    adx_val, plus_di, minus_di = calc_adx(highs, lows, closes)
    vol_ratio = calc_volume_ratio(volumes)
    candle_pat = detect_candle_pattern(opens, highs, lows, closes)
    macd_val, macd_sig, macd_hist = calc_macd(closes)
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes)

    ema9_s  = calc_ema_series(closes, 9)
    ema21_s = calc_ema_series(closes, 21)
    ema55   = calc_ema(closes, 55)

    basic = {
        "price": price, "rsi": rsi, "atr": atr, "adx": adx_val,
        "ema9": ema9_s[-1] if ema9_s else 0,
        "ema21": ema21_s[-1] if ema21_s else 0,
        "ema55": ema55,
        "macd_hist": macd_hist, "vol_ratio": vol_ratio,
        "candle_pattern": candle_pat,
    }

    if len(ema9_s) < 3 or len(ema21_s) < 3:
        basic["signal"] = "WAIT"
        basic["score"] = 0
        basic["raw_score"] = 0
        basic["reasons"] = ["Data EMA kurang"]
        basic["trend"] = "FLAT"
        return basic

    prev_diff = ema9_s[-3] - ema21_s[-3]
    curr_diff = ema9_s[-1] - ema21_s[-1]
    golden_cross = prev_diff <= 0 and curr_diff > 0
    death_cross  = prev_diff >= 0 and curr_diff < 0

    if curr_diff > 0:
        basic["trend"] = "UP"
    elif curr_diff < 0:
        basic["trend"] = "DOWN"
    else:
        basic["trend"] = "FLAT"

    if not golden_cross and not death_cross:
        basic["signal"] = "WAIT"
        basic["score"] = 0
        basic["raw_score"] = 0
        basic["reasons"] = [f"Trend {basic['trend']} | Tidak ada cross"]
        return basic

    atr_ok, atr_reason = check_atr_filter(highs, lows, closes)
    if not atr_ok:
        basic["signal"] = "WAIT"
        basic["score"] = 0
        basic["raw_score"] = 0
        basic["reasons"] = [atr_reason]
        basic["filtered"] = True
        return basic

    wick_ok, wick_reason = check_wick_filter(opens, highs, lows, closes)
    if not wick_ok:
        basic["signal"] = "WAIT"
        basic["score"] = 0
        basic["raw_score"] = 0
        basic["reasons"] = [wick_reason]
        basic["filtered"] = True
        return basic

    direction = "CALL" if golden_cross else "PUT"
    score = 0.0
    reasons = []

    score += 1.5
    reasons.append(f"EMA9/21 {'⬆' if golden_cross else '⬇'} (+1.5)")

    if golden_cross and price > ema55:
        score += 1.0
        reasons.append("> EMA55 (+1.0)")
    elif death_cross and price < ema55:
        score += 1.0
        reasons.append("< EMA55 (+1.0)")

    if 35 <= rsi <= 65:
        score += 1.0
        reasons.append(f"RSI {rsi} (+1.0)")

    if golden_cross and macd_hist > 0:
        score += 1.0
        reasons.append(f"MACD + (+1.0)")
    elif death_cross and macd_hist < 0:
        score += 1.0
        reasons.append(f"MACD - (+1.0)")

    if bb_lower < price < bb_upper:
        score += 0.5
        reasons.append("Dalam BB (+0.5)")

    if adx_val > 20:
        score += 1.0
        reasons.append(f"ADX {adx_val} kuat (+1.0)")
    elif adx_val > 15:
        score += 0.5
        reasons.append(f"ADX {adx_val} sedang (+0.5)")

    if vol_ratio > 1.2:
        score += 0.5
        reasons.append(f"Vol {vol_ratio}x (+0.5)")
    elif vol_ratio > 0.8:
        score += 0.25
        reasons.append(f"Vol {vol_ratio}x (+0.25)")

    if golden_cross and candle_pat in ("bullish_engulfing", "hammer"):
        score += 0.5
        reasons.append(f"{candle_pat.replace('_',' ').title()} (+0.5)")
    elif death_cross and candle_pat in ("bearish_engulfing", "shooting_star"):
        score += 0.5
        reasons.append(f"{candle_pat.replace('_',' ').title()} (+0.5)")

    score_norm = round(score / 7.0 * 8.0, 1)
    basic["score"] = score_norm
    basic["raw_score"] = round(score, 1)
    basic["reasons"] = reasons
    basic["direction"] = direction

    if EXPIRY_MINUTES <= TIMEFRAME_MINUTES:
        if adx_val < 22:
            basic["signal"] = "WAIT"
            basic["filtered"] = True
            basic["reasons"] = [f"ADX {adx_val} < 22 (terlalu lemah untuk 1 candle)"]
            return basic

        if vol_ratio < 1.3:
            basic["signal"] = "WAIT"
            basic["filtered"] = True
            basic["reasons"] = [f"Volume {vol_ratio}x < 1.3x (momentum kurang)"]
            return basic

        valid_patterns = ("bullish_engulfing", "bearish_engulfing", "hammer", "shooting_star")
        if candle_pat not in valid_patterns:
            basic["signal"] = "WAIT"
            basic["filtered"] = True
            basic["reasons"] = ["Tidak ada candle pattern konfirmasi (wajib expiry 5m)"]
            return basic

        effective_min_score = max(MIN_SCORE_BASE, 5.0)
    else:
        effective_min_score = MIN_SCORE_BASE

    if score_norm < effective_min_score:
        basic["signal"] = "WAIT"
        basic["reasons"] = [f"Skor {score_norm}/8 < {effective_min_score} (filter ketat)"]
        return basic

    if direction == "CALL":
        basic["sl"] = round(price - 1.5 * atr, 5)
        basic["tp"] = round(price + 2.5 * atr, 5)
    else:
        basic["sl"] = round(price + 1.5 * atr, 5)
        basic["tp"] = round(price - 2.5 * atr, 5)

    basic["signal"] = direction
    return basic

# ==================== COOLDOWN ====================
def is_on_cooldown(symbol):
    last = cooldown_tracker.get(symbol, 0)
    return (time.time() - last) < (COOLDOWN_MIN * 60)

def set_cooldown(symbol):
    cooldown_tracker[symbol] = time.time()

# ==================== FORMAT: EARLY WARNING (BERDERING) ====================
def format_early_warning(name, symbol, result):
    sig = result["signal"]
    emoji = "🟢" if sig == "CALL" else "🔴"
    score = result["score"]
    raw = result.get("raw_score", 0)
    p = result["price"]
    session = get_session_name(datetime.datetime.utcnow().hour)

    score_int = int(score)
    score_bar = ""
    for i in range(8):
        if i < score_int:
            score_bar += "🟢"
        elif i < score:
            score_bar += "🟡"
        else:
            score_bar += "⚫"

    entry_instruction = "🟢 PERSIAPAN BELI NAIK (CALL)" if sig == "CALL" else "🔴 PERSIAPAN BELI TURUN (PUT)"

    lines = [
        f"🔔🔔 {emoji} <b>EARLY WARNING — {html_escape(name)}</b>",
        f"📊 <code>{html_escape(symbol)}</code> | TF: {TIMEFRAME_MINUTES}m | Expiry: {EXPIRY_MINUTES}m",
        "━━━━━━━━━━━━━━━━━━━",
        entry_instruction,
        f"⏳ <b>Entry dalam {EARLY_WARNING_SECONDS // 60} menit — siapkan platform!</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>Skor: {score}/8</b> (raw {raw}/7) {score_bar}",
        f"🕐 {html_escape(session)}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💰 Harga saat ini: <code>{p:.5f}</code>",
        f"🎯 TP: <code>{result.get('tp', 'N/A')}</code>",
        f"🛑 SL: <code>{result.get('sl', 'N/A')}</code>",
        "━━━━━━━━━━━━━━━━━━━",
        "📋 <i>Checklist persiapan:</i>",
        "  1. Buka Stockity",
        "  2. Pilih aset yang sesuai",
        "  3. Set expiry 5 menit",
        "  4. Tunggu pesan ENTRY berikutnya",
        "━━━━━━━━━━━━━━━━━━━",
        "💰 <i>Rekomendasi stake: 1-2% saldo</i>",
        "🛑 <i>Max loss/hari: 5% saldo</i>",
    ]
    return "\n".join(lines)

# ==================== FORMAT: SIGNAL ALERT (BERDERING) ====================
def format_signal_alert(name, symbol, result):
    sig = result["signal"]
    emoji = "🟢" if sig == "CALL" else "🔴"
    score = result["score"]
    raw = result.get("raw_score", 0)
    p = result["price"]
    session = get_session_name(datetime.datetime.utcnow().hour)

    score_int = int(score)
    score_bar = ""
    for i in range(8):
        if i < score_int:
            score_bar += "🟢"
        elif i < score:
            score_bar += "🟡"
        else:
            score_bar += "⚫"

    if score >= 7:
        strength = "🔥 SANGAT KUAT"
    elif score >= 5.5:
        strength = "💪 KUAT"
    else:
        strength = "✅ STANDAR (filter ketat lolos)"

    entry_instruction = "🟢 LANGSUNG BELI NAIK (CALL)" if sig == "CALL" else "🔴 LANGSUNG BELI TURUN (PUT)"

    lines = [
        f"🚨 {emoji} <b>{sig} SIGNAL — {html_escape(name)}</b>",
        f"📊 <code>{html_escape(symbol)}</code> | TF: {TIMEFRAME_MINUTES}m | Expiry: {EXPIRY_MINUTES}m",
        "━━━━━━━━━━━━━━━━━━━",
        entry_instruction,
        f"⚡ <b>ENTRY SEKARANG — JANGAN TUNDA!</b>",
        f"🎯 <b>Expiry: {EXPIRY_MINUTES} Menit (MAX PLATFORM)</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>Skor: {score}/8</b> (raw {raw}/7) {score_bar}",
        f"{strength}",
        f"🕐 {html_escape(session)}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💰 Entry: <code>{p:.5f}</code>",
        f"🎯 TP: <code>{result.get('tp', 'N/A')}</code>",
        f"🛑 SL: <code>{result.get('sl', 'N/A')}</code>",
        f"📈 RR: 1:1.67",
        "━━━━━━━━━━━━━━━━━━━",
        "✅ <i>Re-validasi lolos — kondisi masih valid</i>",
        "ℹ️ <i>Filter: ADX≥22, Vol≥1.3x, Pattern wajib, Wick OK</i>",
        "━━━━━━━━━━━━━━━━━━━",
        "💰 <i>Stake: 1-2% saldo</i>",
        "🛑 <i>Max loss/hari: 5% saldo</i>",
    ]
    return "\n".join(lines)

# ==================== FORMAT: CANCELLED (BERDERING) ====================
def format_cancelled(name, symbol, reason):
    lines = [
        f"⚠️ <b>SINYAL DIBATALKAN — {html_escape(name)}</b>",
        f"📊 <code>{html_escape(symbol)}</code>",
        "━━━━━━━━━━━━━━━━━━━",
        f"❌ Alasan: {html_escape(reason)}",
        "━━━━━━━━━━━━━━━━━━━",
        "<i>Jangan entry — tunggu sinyal berikutnya</i>",
    ]
    return "\n".join(lines)

# ==================== BACKTEST (FILTER DIPERLONGGAR) ====================
def run_backtest():
    total_signals = 0
    wins = 0
    losses = 0

    for symbol in ASSETS.keys():
        closes, highs, lows, opens, volumes = fetch_prices(symbol, days=7)
        if not closes or len(closes) < 200:
            continue

        for i in range(100, len(closes) - 1):
            window_closes = closes[:i+1]
            window_highs = highs[:i+1]
            window_lows = lows[:i+1]
            window_opens = opens[:i+1]
            window_volumes = volumes[:i+1]

            if len(window_closes) < 80:
                continue

            ema9_s = calc_ema_series(window_closes, 9)
            ema21_s = calc_ema_series(window_closes, 21)
            if len(ema9_s) < 3 or len(ema21_s) < 3:
                continue

            prev_diff = ema9_s[-3] - ema21_s[-3]
            curr_diff = ema9_s[-1] - ema21_s[-1]
            golden_cross = prev_diff <= 0 and curr_diff > 0
            death_cross = prev_diff >= 0 and curr_diff < 0

            if not golden_cross and not death_cross:
                continue

            adx_val, _, _ = calc_adx(window_highs, window_lows, window_closes)
            vol_ratio = calc_volume_ratio(window_volumes)

            if adx_val < 18 or vol_ratio < 1.1:
                continue

            atr = calc_atr(window_highs, window_lows, window_closes)
            entry_price = window_closes[-1]
            next_price = closes[i+1]

            total_signals += 1
            if golden_cross and next_price > entry_price:
                wins += 1
            elif death_cross and next_price < entry_price:
                wins += 1
            else:
                losses += 1

    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    avg_per_day = total / 7.0 if total > 0 else 0

    return {
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_per_day": avg_per_day
    }

# ==================== MAIN LOOP ====================
def process_signal_with_delay(b, symbol, name, result):
    try:
        print(f"[SIGNAL START] {symbol} {result['signal']} score={result['score']}")

        warning_msg = format_early_warning(name, symbol, result)
        b.send_message(CHAT_ID, warning_msg, parse_mode="HTML")
        print(f"[WARNING SENT] {symbol}")

        pending_signals[symbol] = result
        time.sleep(EARLY_WARNING_SECONDS)

        recheck = analyze(symbol)
        if not recheck or recheck.get("signal") != result["signal"]:
            print(f"[RECHECK FAILED] {symbol} - cancelled")
            cancel_msg = format_cancelled(name, symbol, "Kondisi berubah setelah re-validasi")
            b.send_message(CHAT_ID, cancel_msg, parse_mode="HTML")
            pending_signals.pop(symbol, None)
            return

        print(f"[RECHECK PASSED] {symbol} - sending entry")
        alert_msg = format_signal_alert(name, symbol, recheck)
        b.send_message(CHAT_ID, alert_msg, parse_mode="HTML")
        set_cooldown(symbol)
        pending_signals.pop(symbol, None)

    except Exception as e:
        print(f"[SIGNAL ERROR] {symbol}: {e}")
        pending_signals.pop(symbol, None)

def scan_loop():
    while True:
        try:
            b = get_bot()
            utc_hour = datetime.datetime.utcnow().hour

            if not is_active_session(utc_hour):
                time.sleep(CHECK_INTERVAL)
                continue

            if is_news_hour(utc_hour):
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol, name in ASSETS.items():
                if is_on_cooldown(symbol):
                    continue
                if symbol in pending_signals:
                    continue

                result = analyze(symbol)
                if not result:
                    continue

                sig = result.get("signal", "WAIT")

                if sig in ("CALL", "PUT"):
                    last_signal_scores[symbol] = f"{result.get('score', 0)}/8 (raw {result.get('raw_score', 0)}/7) ✅ SIGNAL"
                    t = threading.Thread(target=process_signal_with_delay, args=(b, symbol, name, result), daemon=True)
                    t.start()
                else:
                    reason = result.get('reasons', [''])[0] if result.get('reasons') else 'WAIT'
                    last_signal_scores[symbol] = f"{result.get('score', 0)}/8 (raw {result.get('raw_score', 0)}/7) ❌ {reason[:30]}"

        except Exception as e:
            print(f"[SCAN ERROR] {e}")

        time.sleep(CHECK_INTERVAL)

# ==================== FLASK ROUTE ====================
@app.route("/")
def index():
    return "Aswadd Bot Multi-Asset Final v3.3 is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    b = get_bot()
    json_str = flask_request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    b.process_new_updates([update])
    return "OK", 200

# ==================== START ====================
if __name__ == "__main__":
    scanner = threading.Thread(target=scan_loop, daemon=True)
    scanner.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
