import os
import time
import datetime
import threading
import requests
from flask import Flask, request as flask_request
import telebot

# ==================== KONFIGURASI FINAL ====================
CHAT_ID        = os.environ.get("CHAT_ID", "971243017")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # 5 menit
COOLDOWN_MIN   = int(os.environ.get("COOLDOWN_MIN", "10"))
MIN_SCORE_BASE = float(os.environ.get("MIN_SCORE_BASE", "5.0"))  # Naik dari 3 → 5

# FIXED EXPIRY - SESUAI BATAS PLATFORM STOCKITY
EXPIRY_MINUTES = 5
TIMEFRAME_MINUTES = 5

# MULTI-ASSET SUPPORT
ASSETS = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
}

# Session hours in UTC
LONDON_OPEN_UTC   = 7
LONDON_CLOSE_UTC  = 16
NY_OPEN_UTC       = 12
NY_CLOSE_UTC      = 21
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
        asset_list = "\n".join([f"  • {v}" for v in ASSETS.values()])
        b.reply_to(message,
            f"\U0001f916 *Aswadd Bot Multi-Asset Final*\n\n"
            f"\U0001f4ca Update TIAP {TIMEFRAME_MINUTES} MENIT\n"
            f"\U0001f6a8 Sinyal CALL/PUT saat skor tinggi\n\n"
            f"*Spesifikasi:*\n"
            f"\u2022 Market:\n{asset_list}\n"
            f"\u2022 TF: {TIMEFRAME_MINUTES} Menit\n"
            f"\u2022 Expiry: {EXPIRY_MINUTES} Menit (MAX PLATFORM)\n"
            f"\u2022 Entry: LANGSUNG saat sinyal muncul\n"
            f"\u2022 Min Score: {MIN_SCORE_BASE}/8 (diperketat)\n"
            f"\u2022 Cooldown: {COOLDOWN_MIN} menit/aset\n\n"
            f"*8 Layer + Filter Ketat Expiry 5m:*\n"
            f"\u2022 EMA 9/21/55\n"
            f"\u2022 RSI(14) Zone 35-65\n"
            f"\u2022 MACD(8,17,9)\n"
            f"\u2022 Bollinger Bands(20,2)\n"
            f"\u2022 ADX(14) ≥ 22\n"
            f"\u2022 Volume Spike ≥ 1.3x\n"
            f"\u2022 Candlestick Pattern WAJIB\n"
            f"\u2022 ATR Filter + 2x Confirm\n\n"
            f"*Jam Terbaik:* 20:00-23:00 WIB\n"
            f"(London+NY Overlap)\n\n"
            f"/status /score",
            parse_mode="Markdown")

    @b.message_handler(commands=["status"])
    def cmd_status(message):
        utc_now = datetime.datetime.utcnow()
        hour = utc_now.hour
        session = get_session_name(hour)
        active_assets = [v for k, v in ASSETS.items()]
        b.reply_to(message,
            f"\u2705 *Bot Aktif!*\n"
            f"Aset: {', '.join(active_assets)}\n"
            f"Min skor: {MIN_SCORE_BASE}/8\n"
            f"Expiry: {EXPIRY_MINUTES}m (fixed)\n"
            f"Cooldown: {COOLDOWN_MIN} menit/aset\n"
            f"Pending confirm: {len(pending_signals)}\n"
            f"Sesi sekarang: {session}\n"
            f"Update tiap {TIMEFRAME_MINUTES} menit",
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

# ==================== ANTI-NOISE: ATR VOLATILITY FILTER ====================
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

# ==================== ANALISIS 8-LAYER + FILTER KETAT EXPIRY 5M ====================
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
        basic["reasons"] = [f"Trend {basic['trend']} | Tidak ada cross"]
        return basic

    # ANTI-NOISE: ATR Filter
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

    # Layer 1: EMA 9/21 Crossover — 1.5pt
    score += 1.5
    reasons.append(f"EMA9/21 {'⬆' if golden_cross else '⬇'} (+1.5)")

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

    # Normalize to /8 scale
    score_norm = round(score / 7.0 * 8.0, 1)
    basic["score"] = score_norm
    basic["reasons"] = reasons
    basic["direction"] = direction

    # ============================================================
    # FILTER KETAT KHUSUS EXPIRY 5 MENIT (1 CANDLE)
    # ============================================================
    if EXPIRY_MINUTES <= TIMEFRAME_MINUTES:
        # Gate 1: ADX harus ≥ 22
        if adx_val < 22:
            basic["signal"] = "WAIT"
            basic["filtered"] = True
            basic["reasons"] = [f"ADX {adx_val} < 22 (terlalu lemah untuk 1 candle)"]
            return basic

        # Gate 2: Volume harus ≥ 1.3x
        if vol_ratio < 1.3:
            basic["signal"] = "WAIT"
            basic["filtered"] = True
            basic["reasons"] = [f"Volume {vol_ratio}x < 1.3x (momentum kurang untuk 1 candle)"]
            return basic

        # Gate 3: Candle pattern WAJIB ada
        valid_patterns = ("bullish_engulfing", "bearish_engulfing", "hammer", "shooting_star")
        if candle_pat not in valid_patterns:
            basic["signal"] = "WAIT"
            basic["filtered"] = True
            basic["reasons"] = [f"Tidak ada candle pattern konfirmasi (wajib untuk expiry 5m)"]
            return basic

        # Gate 4: Minimum score lebih tinggi
        effective_min_score = max(MIN_SCORE_BASE, 5.0)
    else:
        effective_min_score = MIN_SCORE_BASE

    if score_norm < effective_min_score:
        basic["signal"] = "WAIT"
        basic["reasons"] = [f"Skor {score_norm} < {effective_min_score} (filter ketat expiry 5m)"]
        return basic

    # SL/TP based on ATR
    if direction == "CALL":
        basic["sl"] = round(price - 1.5 * atr, 5)
        basic["tp"] = round(price + 2.5 * atr, 5)
    else:
        basic["sl"] = round(price + 1.5 * atr, 5)
        basic["tp"] = round(price - 2.5 * atr, 5)

    basic["signal"] = direction
    return basic

# ==================== COOLDOWN PER ASET ====================
def is_on_cooldown(symbol):
    last = cooldown_tracker.get(symbol, 0)
    return (time.time() - last) < (COOLDOWN_MIN * 60)

def set_cooldown(symbol):
    cooldown_tracker[symbol] = time.time()

# ==================== FORMAT: MARKET UPDATE ====================
def format_market_update(symbol, result):
    name = ASSETS.get(symbol, symbol)
    now_wib = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%H:%M")
    hour_utc = datetime.datetime.utcnow().hour
    session = get_session_name(hour_utc)

    if not result:
        return f"\U0001f4ca *{name} UPDATE* | {now_wib} WIB\n_Data error_"

    price = result.get("price", 0)
    rsi = result.get("rsi", 0)
    score = result.get("score", 0)
    sig = result.get("signal", "WAIT")
    trend = result.get("trend", "FLAT")
    adx = result.get("adx", 0)
    vol = result.get("vol_ratio", 0)
    candle = result.get("candle_pattern", "none")

    rsi_e = "\U0001f534" if rsi > 65 else ("\U0001f7e2" if rsi < 35 else "\u26aa")
    sig_e = "\U0001f7e2" if sig == "CALL" else ("\U0001f534" if sig == "PUT" else "\u23f3")
    t_arrow = "\u2b06\ufe0f" if trend == "UP" else ("\u2b07\ufe0f" if trend == "DOWN" else "\u27a1\ufe0f")
    price_str = f"{price:.5f}"

    ema21 = result.get("ema21", 0)
    pip_str = f"{(price - ema21) * 10000:+.1f} pip dari EMA21" if ema21 > 0 else ""

    lines = [
        f"\U0001f4ca *{name} LIVE UPDATE*",
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

    lines += ["", f"\U0001f550 Jam terbaik: 20:00-23:00 WIB", "_@Aswadd_bot Multi-Asset Final_"]
    return "\n".join(lines)

# ==================== FORMAT: SIGNAL ALERT (TEGAS) ====================
def format_signal_alert(name, symbol, result):
    sig   = result["signal"]
    emoji = "\U0001f7e2" if sig == "CALL" else "\U0001f534"
    score = result["score"]
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
        strength = "\U0001f5
