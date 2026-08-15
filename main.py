import os
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
MIN_SCORE      = float(os.environ.get("MIN_SCORE", "3"))

# HANYA EUR/USD — fokus 1 market
ASSETS = {
    "EURUSD=X": "EUR/USD",
}

# Session hours in UTC
# London: 07:00-16:00 UTC | New York: 12:00-21:00 UTC
# Overlap (BEST): 13:00-16:00 UTC = 20:00-23:00 WIB
# London active: 07:00-16:00 UTC = 14:00-23:00 WIB
LONDON_OPEN_UTC  = 7
LONDON_CLOSE_UTC = 16
NY_OPEN_UTC      = 12
NY_CLOSE_UTC     = 21
OVERLAP_START_UTC = 13
OVERLAP_END_UTC   = 16

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
        b.reply_to(message,
            "\U0001f916 *Aswadd Bot EUR/USD Final*\n\n"
            "\U0001f4ca Update TIAP 5 MENIT\n"
            "\U0001f6a8 Sinyal CALL/PUT saat skor tinggi\n\n"
            "*Spesifikasi:*\n"
            "\u2022 Market: EUR/USD saja\n"
            "\u2022 TF: 5 Menit\n"
            "\u2022 Expiry: 5-15 Menit\n"
            "\u2022 Entry: LANGSUNG saat sinyal\n"
            f"\u2022 Min Score: {MIN_SCORE}/8\n"
            f"\u2022 Cooldown: {COOLDOWN_MIN} menit\n\n"
            "*8 Layer + Anti-Noise:*\n"
            "\u2022 EMA 9/21/55\n"
            "\u2022 RSI(14) Zone 35-65\n"
            "\u2022 MACD(8,17,9)\n"
            "\u2022 Bollinger Bands(20,2)\n"
            "\u2022 ADX(14)\n"
            "\u2022 Volume Spike\n"
            "\u2022 Candlestick Pattern\n"
            "\u2022 ATR Filter + 2x Confirm\n\n"
            "*Jam Terbaik:* 20:00-23:00 WIB\n"
            "(London+NY Overlap)\n\n"
            "/status /score",
            parse_mode="Markdown")

    @b.message_handler(commands=["status"])
    def cmd_status(message):
        utc_now = datetime.datetime.utcnow()
        hour = utc_now.hour
        session = get_session_name(hour)
        b.reply_to(message,
            f"\u2705 *EUR/USD Bot Aktif!*\n"
            f"Min skor: {MIN_SCORE}/8\n"
            f"Cooldown: {COOLDOWN_MIN} menit\n"
            f"Pending confirm: {len(pending_signals)}\n"
            f"Sesi sekarang: {session}\n"
            "Update tiap 5 menit",
            parse_mode="Markdown")

    @b.message_handler(commands=["score"])
    def cmd_score(message):
        lines = ["\U0001f4ca *Skor Terakhir:*"]
        if last_signal_scores:
            for sym, sc in last_signal_scores.items():
                name = ASSETS.get(sym, sym)
                lines.append(f"\u2022 {name}: {sc}/8")
        else:
            lines.append("_Belum ada scan_")
        lines.append(f"\n\u23f3 Pending: {len(pending_signals)}")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

# ==================== SESSION DETECTION ====================
def get_session_name(hour_utc):
    in_london = LONDON_OPEN_UTC <= hour_utc < LONDON_CLOSE_UTC
    in_ny = NY_OPEN_UTC <= hour_utc < NY_CLOSE_UTC
    in_overlap = OVERLAP_START_UTC <= hour_utc < OVERLAP_END_UTC
    if in_overlap:
        return "\U0001f525 Overlap London+NY (TERBAIK)"
    elif in_london and in_ny:
        return "\u2705 London + New York"
    elif in_london:
        return "\u2705 London"
    elif in_ny:
        return "\u2705 New York"
    else:
        return "\u26a0\ufe0f Off-Hours (market sepi)"

def is_active_session(hour_utc):
    return (LONDON_OPEN_UTC <= hour_utc < LONDON_CLOSE_UTC) or \
           (NY_OPEN_UTC <= hour_utc < NY_CLOSE_UTC)

def is_best_session(hour_utc):
    return OVERLAP_START_UTC <= hour_utc < OVERLAP_END_UTC

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
        if min_len < 60:
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
    """MACD 8-17-9: optimized for 5-minute charts"""
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

# ==================== ANTI-NOISE 1: ATR VOLATILITY FILTER ====================
def check_atr_filter(highs, lows, closes):
    """EUR/USD 5m ATR avg ~3-8 pips (0.0003-0.0008)"""
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

# ==================== ANALISIS 8-LAYER WEIGHTED ====================
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

    # Basic info for market update
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
        basic["reasons"] = ["Data EMA kurang"]
        basic["trend"] = "FLAT"
        return basic

    prev_diff = ema9_s[-3] - ema21_s[-3]
    curr_diff = ema9_s[-1] - ema21_s[-1]
    golden_cross = prev_diff <= 0 and curr_diff > 0
    death_cross  = prev_diff >= 0 and curr_diff < 0

    # Trend direction for display
    if curr_diff > 0:
        basic["trend"] = "UP"
    elif curr_diff < 0:
        basic["trend"] = "DOWN"
    else:
        basic["trend"] = "FLAT"

    if not golden_cross and not death_cross:
        basic["signal"] = "WAIT"
        basic["score"] = 0
        basic["reasons"] = [f"Trend {basic['trend']} | Tidak ada cross"]
        return basic

    # ANTI-NOISE 1: ATR Filter
    atr_ok, atr_reason = check_atr_filter(highs, lows, closes)
    if not atr_ok:
        basic["signal"] = "WAIT"
        basic["score"] = 0
        basic["reasons"] = [atr_reason]
        basic["filtered"] = True
        return basic

    direction = "CALL" if golden_cross else "PUT"
    score = 0.0
    reasons = []

    # Layer 1: EMA 9/21 Crossover — 1.5pt (TERPENTING)
    score += 1.5
    reasons.append(f"EMA9/21 {'\u2b06' if golden_cross else '\u2b07'} (+1.5)")

    # Layer 2: EMA 55 Trend — 1.0pt
    if golden_cross and price > ema55:
        score += 1.0
        reasons.append("> EMA55 (+1.0)")
    elif death_cross and price < ema55:
        score += 1.0
        reasons.append("< EMA55 (+1.0)")

    # Layer 3: RSI Zone 35-65 — 1.0pt
    if 35 <= rsi <= 65:
        score += 1.0
        reasons.append(f"RSI {rsi} (+1.0)")

    # Layer 4: MACD(8,17,9) — 1.0pt
    if golden_cross and macd_hist > 0:
        score += 1.0
        reasons.append(f"MACD + (+1.0)")
    elif death_cross and macd_hist < 0:
        score += 1.0
        reasons.append(f"MACD - (+1.0)")

    # Layer 5: Bollinger Bands — 0.5pt
    if bb_lower < price < bb_upper:
        score += 0.5
        reasons.append("Dalam BB (+0.5)")

    # Layer 6: ADX Trend Strength — 1.0pt
    if adx_val > 20:
        score += 1.0
        reasons.append(f"ADX {adx_val} kuat (+1.0)")
    elif adx_val > 15:
        score += 0.5
        reasons.append(f"ADX {adx_val} sedang (+0.5)")

    # Layer 7: Volume Spike — 0.5pt
    if vol_ratio > 1.2:
        score += 0.5
        reasons.append(f"Vol {vol_ratio}x (+0.5)")
    elif vol_ratio > 0.8:
        score += 0.25
        reasons.append(f"Vol {vol_ratio}x (+0.25)")

    # Layer 8: Candlestick Pattern — 0.5pt
    if golden_cross and candle_pat in ("bullish_engulfing", "hammer"):
        score += 0.5
        reasons.append(f"{candle_pat.replace('_',' ').title()} (+0.5)")
    elif death_cross and candle_pat in ("bearish_engulfing", "shooting_star"):
        score += 0.5
        reasons.append(f"{candle_pat.replace('_',' ').title()} (+0.5)")

    # Normalize to /8 scale (max raw = 7.0)
    score_norm = round(score / 7.0 * 8.0, 1)
    basic["score"] = score_norm
    basic["reasons"] = reasons
    basic["direction"] = direction

    if score_norm < MIN_SCORE:
        basic["signal"] = "WAIT"
        return basic

    # SL/TP based on ATR
    # EUR/USD 5m ATR ~0.0003-0.0008 (3-8 pips)
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

# ==================== FORMAT: MARKET UPDATE (tiap 5 menit) ====================
def format_market_update(result):
    now_wib = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%H:%M")
    hour_utc = datetime.datetime.utcnow().hour
    session = get_session_name(hour_utc)

    if not result:
        return f"\U0001f4ca *EUR/USD UPDATE* | {now_wib} WIB\n_Data error_"

    price = result.get("price", 0)
    rsi = result.get("rsi", 0)
    score = result.get("score", 0)
    sig = result.get("signal", "WAIT")
    trend = result.get("trend", "FLAT")
    adx = result.get("adx", 0)
    vol = result.get("vol_ratio", 0)
    candle = result.get("candle_pattern", "none")

    # RSI emoji
    if rsi > 65:
        rsi_e = "\U0001f534"
    elif rsi < 35:
        rsi_e = "\U0001f7e2"
    else:
        rsi_e = "\u26aa"

    # Signal emoji
    if sig == "CALL":
        sig_e = "\U0001f7e2"
    elif sig == "PUT":
        sig_e = "\U0001f534"
    else:
        sig_e = "\u23f3"

    # Trend arrow
    if trend == "UP":
        t_arrow = "\u2b06\ufe0f"
    elif trend == "DOWN":
        t_arrow = "\u2b07\ufe0f"
    else:
        t_arrow = "\u27a1\ufe0f"

    # Price format (EUR/USD = 5 decimal)
    price_str = f"{price:.5f}"

    # Pip movement from EMA21
    ema21 = result.get("ema21", 0)
    if ema21 > 0:
        pip_diff = (price - ema21) * 10000
        pip_str = f"{pip_diff:+.1f} pip dari EMA21"
    else:
        pip_str = ""

    lines = [
        f"\U0001f4ca *EUR/USD LIVE UPDATE*",
        f"\u23f0 {now_wib} WIB | {session}",
        "\u2501" * 18,
        f"{sig_e} *Harga:* `{price_str}` {t_arrow}",
        f"\U0001f4cf {pip_str}",
        "",
        f"\U0001f4c9 RSI(14): {rsi_e} `{rsi}`",
        f"\U0001f4ca ADX(14): `{adx}`",
        f"\U0001f4e6 Volume: `{vol}x`",
        f"\U0001f56f Pattern: `{candle.replace('_',' ').title()}`",
        f"\U0001f3af Skor: `{score}/8`",
        "\u2501" * 18,
    ]

    if sig in ("CALL", "PUT"):
        lines.append(f"\U0001f6a8 *SINYAL {sig} TERKIRIM!*")
    else:
        reason = result.get("reasons", [""])[0] if result.get("reasons") else "Menunggu..."
        lines.append(f"\u23f3 {reason}")

    lines += [
        "",
        f"\U0001f550 Jam terbaik: 20:00-23:00 WIB",
        "_@Aswadd_bot EUR/USD Final_"
    ]
    return "\n".join(lines)

# ==================== FORMAT: SIGNAL ALERT (langsung entry) ====================
def format_signal_alert(name, symbol, result):
    sig   = result["signal"]
    emoji = "\U0001f7e2" if sig == "CALL" else "\U0001f534"
    score = result["score"]
    now_wib = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
    p     = result["price"]
    hour_utc = datetime.datetime.utcnow().hour
    session = get_session_name(hour_utc)

    score_int = int(score)
    score_bar = ""
    for i in range(8):
        if i < score_int:
            score_bar += "\U0001f7e2"
        elif i < score:
            score_bar += "\U0001f7e1"
        else:
            score_bar += "\u26ab"

    if score >= 7:
        strength = "\U0001f525 *SANGAT KUAT*"
    elif score >= 5.5:
        strength = "\U0001f4aa *KUAT*"
    elif score >= 4:
        strength = "\u2705 *STANDAR*"
    else:
        strength = "\u26a0\ufe0f *MINIMAL — hati-hati*"

    # Entry instruction
    if sig == "CALL":
        entry_instruction = "\U0001f7e2 *LANGSUNG BELI NAIK (CALL)*"
    else:
        entry_instruction = "\U0001f534 *LANGSUNG BELI TURUN (PUT)*"

    lines = [
        f"\U0001f6a8 {emoji} *{sig} SIGNAL \u2014 {name}*",
        f"\U0001f4ca `{symbol}` | TF: 5 Menit",
        "\u2501" * 19,
        entry_instruction,
        f"\U0001f3af *Expiry: 5-15 Menit*",
        f"\u26a1 *Entry: SEKARANG \u2014 jangan tunggu!*",
        "\u2501" * 19,
        f"*Skor: {score}/8* {score_bar}",
        f"{strength}",
        f"\U0001f550 {session}",
        "\u2501" * 19,
        f"\U0001f4b0 Entry: `{p:.5f}`",
        f"\U0001f3af TP: `{result.get('tp', 'N/A')}`",
        f"\U0001f6d1 SL: `{result.get('sl', 'N/A')}`",
        f"\U0001f4c8 RR: 1:1.67",
        "\u2501" * 19,
        f"\U0001f4c9 RSI: `{result['rsi']}` | ADX: `{result['adx']}`",
        f"\U0001f4c8 EMA9: `{result['ema9']:.5f}`",
        f"\U0001f4c9 EMA21: `{result['ema21']:.5f}`",
        f"\U0001f4ca EMA55: `{result['ema55']:.5f}`",
        f"\U0001f4cf MACD(8,17,9): `{result['macd_hist']}`",
        f"\U0001f4e6 Vol: `{result['vol_ratio']}x`",
        f"\U0001f56f Pattern: `{result['candle_pattern'].replace('_',' ').title()}`",
        "\u2501" * 19,
        "*\u2705 Konfirmasi:*",
    ]
    for r in result.get("reasons", []):
        lines.append(f"\u2022 {r}")
    lines += [
        "\u2501" * 19,
        "*\U0001f4cc Cara Entry:*",
        "1. Buka Stockity",
        f"2. Pilih EUR/USD",
        f"3. Pilih {sig} ({'\u2b06 Naik' if sig == 'CALL' else '\u2b07 Turun'})",
        "4. Expiry: 5-15 menit",
        "5. Klik BUY \u2014 SEKARANG!",
        "\u2501" * 19,
        f"\u23f0 {now_wib} WIB",
        "_@Aswadd_bot EUR/USD Final_"
    ]
    return "\n".join(lines)

# ==================== BACKGROUND THREAD ====================
def analysis_worker():
    b = get_bot()
    print(f"[EUR/USD FINAL] Started | Interval={CHECK_INTERVAL}s | MinScore={MIN_SCORE}")
    try:
        b.send_message(
            CHAT_ID,
            f"\U0001f916 *Aswadd Bot EUR/USD FINAL Aktif!*\n\n"
            f"\U0001f4ca Update TIAP 5 MENIT\n"
            f"\U0001f6a8 Sinyal CALL/PUT langsung entry\n\n"
            f"*Spesifikasi:*\n"
            f"\u2022 Market: EUR/USD\n"
            f"\u2022 TF: 5 Menit\n"
            f"\u2022 Expiry: 5-15 Menit\n"
            f"\u2022 Min Score: {MIN_SCORE}/8\n"
            f"\u2022 Cooldown: {COOLDOWN_MIN} menit\n\n"
            f"*8 Layer + Anti-Noise:*\n"
            f"1\u20e3 EMA 9/21 Cross (1.5pt)\n"
            f"2\u20e3 EMA 55 Trend (1.0pt)\n"
            f"3\u20e3 RSI(14) 35-65 (1.0pt)\n"
            f"4\u20e3 MACD(8,17,9) (1.0pt)\n"
            f"5\u20e3 Bollinger Bands (0.5pt)\n"
            f"6\u20e3 ADX(14) (1.0pt)\n"
            f"7\u20e3 Volume Spike (0.5pt)\n"
            f"8\u20e3 Candlestick (0.5pt)\n"
            f"+ ATR Filter + 2x Confirm\n\n"
            f"*\U0001f550 Jam Terbaik:* 20:00-23:00 WIB\n"
            f"(London+NY Overlap)\n\n"
            f"_Estimasi: 4-8 sinyal/hari_",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[STARTUP MSG ERROR] {e}")

    while True:
        result = None
        signal_to_send = None

        for symbol, name in ASSETS.items():
            try:
                result = analyze(symbol)
                if result is None:
                    continue
                last_signal_scores[symbol] = result.get("score", 0)

                sig = result.get("signal", "WAIT")
                if sig in ("CALL", "PUT") and not is_on_cooldown(symbol):
                    # ANTI-NOISE 2: Consecutive Confirmation
                    pending_key = f"{symbol}_{sig}"
                    if pending_key in pending_signals:
                        signal_to_send = (symbol, name, result)
                        del pending_signals[pending_key]
                    else:
                        pending_signals[pending_key] = {
                            "time": time.time(),
                            "score": result["score"],
                        }
            except Exception as e:
                print(f"[ERROR] {symbol}: {e}")

        # 1. ALWAYS send market update every 5 minutes
        try:
            update_msg = format_market_update(result)
            b.send_message(CHAT_ID, update_msg, parse_mode="Markdown")
            now = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] \U0001f4ca Market update sent")
        except Exception as e:
            print(f"[UPDATE ERROR] {e}")

        # 2. Send confirmed signal (langsung entry)
        if signal_to_send:
            symbol, name, sig_result = signal_to_send
            try:
                alert_msg = format_signal_alert(name, symbol, sig_result)
                b.send_message(CHAT_ID, alert_msg, parse_mode="Markdown")
                set_cooldown(symbol)
                now = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{now}] \U0001f6a8 {sig_result['signal']} CONFIRMED: {name} | Score: {sig_result['score']}/8")
            except Exception as e:
                print(f"[SIGNAL ERROR] {e}")

        # Clean old pending (older than 15 minutes)
        cutoff = time.time() - 900
        for key in list(pending_signals.keys()):
            if pending_signals[key]["time"] < cutoff:
                del pending_signals[key]

        time.sleep(CHECK_INTERVAL)

# ==================== FLASK ROUTES ====================
@app.route("/")
def health():
    return {"status": "alive", "bot": "@Aswadd_bot EUR/USD Final", "market": "EUR/USD"}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        json_str = flask_request.get_data().decode("UTF-8")
        update = telebot.types.Update.de_json(json_str)
        get_bot().process_new_updates([update])
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
    return "OK", 200

# ==================== STARTUP ====================
if __name__ == "__main__":
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if railway_url:
        webhook_url = f"https://{railway_url}/webhook"
        try:
            b = get_bot()
            b.remove_webhook()
            time.sleep(1)
            b.set_webhook(url=webhook_url)
            print(f"[WEBHOOK] Set to: {webhook_url}")
        except Exception as e:
            print(f"[WEBHOOK ERROR] {e}")
    else:
        print("[WARN] RAILWAY_PUBLIC_DOMAIN not set, using polling fallback")
        t_poll = threading.Thread(target=lambda: get_bot().infinity_polling(), daemon=True)
        t_poll.start()

    t = threading.Thread(target=analysis_worker, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"[FLASK] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
