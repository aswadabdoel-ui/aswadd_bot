"""
================================================================================
ASWADD BOT MULTI-ASSET v3.5.5 LITE EDITION - STABILIZED
================================================================================
ROLE: Lead Developer & Partner Trading
PLATFORM: Railway Trial (RAM 512MB-1GB STRICT LIMIT)
REPO: GitHub aswadd_bot (Branch: main)
STATUS: Active | yfinance | Dynamic Filter | Memory Optimized

⚠️ CRITICAL RULES (JANGAN LANGGAR):
1. MEMORY: gc.collect() WAJIB setelah fetch & di except block. del df segera.
2. DATETIME: ZERO TOLERANCE untuk utcnow(). Gunakan datetime.now(timezone.utc).
3. FETCH: Timeout max 10s. Sequential. Skip aset jika error, jangan crash loop.
4. FILTER: ADX>=20 & Vol>=1.2x = Hard filter tapi beri poin kecil jika gagal.
5. SCORE: Min 5.0 (Normal) / 6.5 (Low Vol). Cooldown dinamis 5-12 menit.
6. SESSION: London (15:00-00:00 WITA) & NY (20:00-05:00 WITA) ONLY.
7. NEWS: Block ±30 menit sekitar high-impact events.
8. DEPLOY: Hapus deployment lama sebelum redeploy. Cek log utk utcnow warning.
9. STRATEGY: Tetap v3.5.x. DILARANG upgrade arsitektur v4.0 tanpa bukti empiris.
10. FEEDBACK: /scan WAJIB balas meski market sepi/data kosong.

ASSETS: EURUSD=X, GBPUSD=X, USDJPY=X
EXPIRY: 5 menit | TF: 5 menit
SPREADSHEET: Tracking manual (Tanggal, Jam, Aset, Sinyal, Skor, Hasil)

RIWAYAT MASALAH TERSELESAIKAN:
- Memory leak yfinance → Sequential fetch + gc.collect() agresif
- utcnow deprecation → Full timezone.utc migration
- Fetch timeout/blokir → Timeout ketat + error handling per aset
- Bot diam off-hours → Feedback wajib + dynamic cooldown/news block
- Filter terlalu ketat → Poin kecil untuk ADX/Vol gagal (bukan reject mutlak)

TERAKHIR UPDATE: 20 Agustus 2026
================================================================================
"""

import gc
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
import requests
import yfinance as yf
from flask import Flask, request

# === KONFIGURASI ENVIRONMENT ===
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MIN_SCORE_BASE = float(os.getenv("MIN_SCORE_BASE", "5.0"))
MIN_SCORE_LOW_VOL = float(os.getenv("MIN_SCORE_LOW_VOL", "6.5"))
ASSETS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X"]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# === DAFTAR HIGH IMPACT EVENTS (UPDATE MINGGUAN) ===
# Format: (datetime UTC, deskripsi singkat)
HIGH_IMPACT_EVENTS: List[Tuple[datetime, str]] = [
    # CONTOH: (datetime(2026, 8, 25, 14, 30, tzinfo=timezone.utc), "US Core PCE"),
    # TODO: Update setiap Minggu malam sesuai kalender ekonomi minggu depan
]

# === STATE MANAGEMENT (IN-MEMORY, RINGAN) ===
last_signal_time: Dict[str, datetime] = {}
fetch_error_count: Dict[str, int] = {}


# ===================== HELPER FUNCTIONS =====================

def html_escape(text: str) -> str:
    """Escape karakter khusus untuk Telegram HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_cooldown_minutes(score: float) -> int:
    """Cooldown dinamis berdasarkan skor sinyal (5-12 menit)."""
    if score >= 7.5:
        return 5
    elif score >= 6.5:
        return 8
    else:
        return 12


def is_news_blocked() -> bool:
    """Cek apakah saat ini berada dalam window ±30 menit dari high-impact event."""
    now = datetime.now(timezone.utc)
    for event_time, desc in HIGH_IMPACT_EVENTS:
        delta = abs((now - event_time).total_seconds())
        if delta <= 1800:  # 30 menit = 1800 detik
            logger.info(f"[NEWS BLOCK] {desc} dalam {int(delta/60)} menit")
            return True
    return False


def get_session_name() -> str:
    """Return nama sesi trading aktif berdasarkan waktu UTC."""
    hour = datetime.now(timezone.utc).hour
    # London: 07:00-16:00 UTC | NY: 12:00-21:00 UTC
    if 7 <= hour < 16:
        return "London"
    elif 12 <= hour < 21:
        return "New York"
    elif 7 <= hour < 21:
        return "London/NY Overlap"
    else:
        return "Off-Session"


def is_active_session() -> bool:
    """Cek apakah saat ini dalam sesi London atau NY (termasuk overlap)."""
    session = get_session_name()
    return session in ["London", "New York", "London/NY Overlap"]


# ===================== DATA FETCHING (MEMORY SAFE) =====================

def fetch_prices(symbol: str, period: str = "5d", interval: str = "5m") -> Optional[List[dict]]:
    """
    Fetch data OHLCV dari yfinance dengan manajemen memori ketat.
    Return list of dict (bukan DataFrame) untuk menghindari memory leak.
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, timeout=10)
        
        if df.empty or len(df) < 60:
            logger.warning(f"[FETCH] Data tidak cukup untuk {symbol}: {len(df)} bars")
            fetch_error_count[symbol] = fetch_error_count.get(symbol, 0) + 1
            return None
        
        # Konversi ke list of dict SEBELUM menghapus DataFrame
        data = []
        for idx, row in df.iterrows():
            data.append({
                'timestamp': idx.to_pydatetime(),
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': float(row['Volume'])
            })
        
        # Hapus DataFrame dan paksa garbage collection
        del df
        gc.collect()
        
        fetch_error_count[symbol] = 0  # Reset error count jika berhasil
        return data
        
    except Exception as e:
        logger.error(f"[FETCH ERROR] {symbol}: {str(e)}")
        fetch_error_count[symbol] = fetch_error_count.get(symbol, 0) + 1
        gc.collect()  # Wajib collect di except block
        return None


# ===================== INDIKATOR TEKNIKAL =====================

def calc_ema(data: List[float], period: int) -> List[float]:
    """Hitung EMA dari list harga."""
    if len(data) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(data[:period]) / period]
    for price in data[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Hitung RSI terakhir dari list close price."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """Hitung ADX terakhir (simplified version untuk efisiensi memori)."""
    if len(closes) < period * 2:
        return None
    
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []
    
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i-1])
        low_close = abs(lows[i] - closes[i-1])
        tr = max(high_low, high_close, low_close)
        
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
        
        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    
    if len(tr_list) < period:
        return None
    
    atr = sum(tr_list[-period:]) / period
    plus_di = (sum(plus_dm_list[-period:]) / period) / atr * 100 if atr != 0 else 0
    minus_di = (sum(minus_dm_list[-period:]) / period) / atr * 100 if atr != 0 else 0
    
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) != 0 else 0
    return dx  # Simplified: return DX sebagai proxy ADX untuk efisiensi


def calc_volume_ratio(volumes: List[float], lookback: int = 20) -> Optional[float]:
    """Rasio volume candle terakhir vs rata-rata 20 candle sebelumnya."""
    if len(volumes) < lookback + 1:
        return None
    avg_vol = sum(volumes[-(lookback+1):-1]) / lookback
    return volumes[-1] / avg_vol if avg_vol > 0 else 0


def detect_candle_pattern(opens: List[float], closes: List[float], highs: List[float], lows: List[float]) -> str:
    """Deteksi pola candle dasar (bullish/bearish engulfing, doji)."""
    if len(opens) < 2:
        return "none"
    
    prev_body = closes[-2] - opens[-2]
    curr_body = closes[-1] - opens[-1]
    body_size = abs(curr_body)
    wick_upper = highs[-1] - max(opens[-1], closes[-1])
    wick_lower = min(opens[-1], closes[-1]) - lows[-1]
    total_range = highs[-1] - lows[-1]
    
    if total_range == 0:
        return "none"
    
    # Doji
    if body_size / total_range < 0.1:
        return "doji"
    # Bullish Engulfing
    if prev_body < 0 and curr_body > 0 and abs(curr_body) > abs(prev_body):
        return "bullish_engulfing"
    # Bearish Engulfing
    if prev_body > 0 and curr_body < 0 and abs(curr_body) > abs(prev_body):
        return "bearish_engulfing"
    
    return "none"


def check_wick_filter(highs: List[float], lows: List[float], closes: List[float], opens: List[float], max_wick_pct: float = 0.7) -> bool:
    """Return True jika wick TIDAK melebihi threshold (lolos filter)."""
    if len(highs) < 1:
        return False
    total_range = highs[-1] - lows[-1]
    if total_range == 0:
        return True
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    lower_wick = min(opens[-1], closes[-1]) - lows[-1]
    max_wick = max(upper_wick, lower_wick)
    return (max_wick / total_range) <= max_wick_pct


def check_atr_filter(highs: List[float], lows: List[float], closes: List[float], 
                     min_mult: float = 0.25, max_mult: float = 3.5) -> bool:
    """Cek apakah range candle terakhir dalam batas ATR normal."""
    if len(closes) < 15:
        return False
    tr_list = []
    for i in range(-14, 0):
        h_l = highs[i] - lows[i]
        h_c = abs(highs[i] - closes[i-1])
        l_c = abs(lows[i] - closes[i-1])
        tr_list.append(max(h_l, h_c, l_c))
    avg_tr = sum(tr_list) / len(tr_list)
    current_range = highs[-1] - lows[-1]
    ratio = current_range / avg_tr if avg_tr > 0 else 0
    return min_mult <= ratio <= max_mult


# ===================== ANALISIS 8-LAYER =====================

def analyze(symbol: str, data: List[dict]) -> Optional[Dict]:
    """
    Analisis 8-layer dengan deteksi Low Volatility & dynamic threshold.
    Return dict sinyal atau None jika tidak memenuhi kriteria.
    """
    if len(data) < 60:
        return None
    
    closes = [d['close'] for d in data]
    highs = [d['high'] for d in data]
    lows = [d['low'] for d in data]
    opens = [d['open'] for d in data]
    volumes = [d['volume'] for d in data]
    
    # Layer 1: EMA Cross + Alignment
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema55 = calc_ema(closes, 55)
    if not ema9 or not ema21 or not ema55:
        return None
    
    bullish_cross = ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]
    bearish_cross = ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]
    ema_aligned_bull = ema9[-1] > ema21[-1] > ema55[-1]
    ema_aligned_bear = ema9[-1] < ema21[-1] < ema55[-1]
    
    direction = None
    if bullish_cross and ema_aligned_bull:
        direction = "BUY"
    elif bearish_cross and ema_aligned_bear:
        direction = "SELL"
    else:
        return None  # Tidak ada cross valid
    
    # Layer 2: RSI Zone
    rsi = calc_rsi(closes)
    if rsi is None or not (35 <= rsi <= 65):
        return None
    
    # Layer 3: ADX (Hard filter + poin kecil)
    adx = calc_adx(highs, lows, closes)
    adx_pass = adx is not None and adx >= 20
    
    # Layer 4: Volume Ratio (Hard filter + poin kecil)
    vol_ratio = calc_volume_ratio(volumes)
    vol_pass = vol_ratio is not None and vol_ratio >= 1.2
    
    # Layer 5: Wick Filter
    wick_pass = check_wick_filter(highs, lows, closes, opens)
    if not wick_pass:
        return None
    
    # Layer 6: ATR Filter
    atr_pass = check_atr_filter(highs, lows, closes)
    if not atr_pass:
        return None
    
    # Layer 7: Candle Pattern (bonus poin)
    pattern = detect_candle_pattern(opens, closes, highs, lows)
    pattern_bonus = 0.5 if pattern in ["bullish_engulfing", "bearish_engulfing"] else 0
    
    # Layer 8: Scoring & Dynamic Threshold
    score = 0.0
    score += 2.0  # EMA cross + alignment (wajib)
    score += 1.0  # RSI in zone (wajib)
    score += 1.0 if adx_pass else 0.3  # ADX
    score += 1.0 if vol_pass else 0.3  # Volume
    score += 1.0  # Wick pass (wajib)
    score += 1.0  # ATR pass (wajib)
    score += pattern_bonus
    
    # Deteksi Low Volatility Mode
    recent_ranges = [highs[i] - lows[i] for i in range(-20, 0)]
    avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
    is_low_vol = avg_range < (sum([h-l for h,l in zip(highs[-60:], lows[-60:])]) / 60 * 0.6)
    
    min_threshold = MIN_SCORE_LOW_VOL if is_low_vol else MIN_SCORE_BASE
    
    if score < min_threshold:
        return None
    
    return {
        'symbol': symbol,
        'direction': direction,
        'score': round(score, 2),
        'rsi': round(rsi, 2),
        'adx': round(adx, 2) if adx else 0,
        'vol_ratio': round(vol_ratio, 2) if vol_ratio else 0,
        'pattern': pattern,
        'is_low_vol': is_low_vol,
        'min_threshold': min_threshold,
        'timestamp': datetime.now(timezone.utc)
    }


# ===================== FORMAT NOTIFIKASI =====================

def format_signal_alert(signal: Dict) -> str:
    """Format pesan alert sinyal untuk Telegram."""
    emoji = "🟢" if signal['direction'] == "BUY" else "🔴"
    vol_status = "✅" if signal['vol_ratio'] >= 1.2 else "⚠️"
    adx_status = "✅" if signal['adx'] >= 20 else "⚠️"
    vol_mode = "🌙 LOW VOL" if signal['is_low_vol'] else "☀️ NORMAL"
    
    return (
        f"{emoji} <b>SINYAL {signal['direction']}</b> | {html_escape(signal['symbol'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Skor: <b>{signal['score']}/{8}</b> (Min: {signal['min_threshold']})\n"
        f"📈 RSI: {signal['rsi']} | ADX: {signal['adx']} {adx_status}\n"
        f"📦 Vol: {signal['vol_ratio']}x {vol_status} | Pola: {signal['pattern']}\n"
        f"⏱️ Expiry: 5 menit | TF: 5m | Mode: {vol_mode}\n"
        f"🕒 {signal['timestamp'].strftime('%H:%M:%S')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )


def format_cancelled(symbol: str, reason: str) -> str:
    """Format pesan pembatalan sinyal."""
    return f"❌ <b>DIBATALKAN</b> | {html_escape(symbol)}\nAlasan: {html_escape(reason)}"


# ===================== TELEGRAM HANDLERS =====================

def send_telegram(text: str):
    """Kirim pesan ke Telegram dengan error handling."""
    if not CHAT_ID or not BOT_TOKEN:
        logger.error("[TG] CHAT_ID atau BOT_TOKEN tidak diset!")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"[TG] Gagal kirim: {resp.text}")
    except Exception as e:
        logger.error(f"[TG ERROR] {e}")


def handle_command(command: str, args: str = "") -> str:
    """Handler untuk command Telegram."""
    cmd = command.lower().strip()
    
    if cmd == "/start":
        return "🤖 <b>Aswadd Bot v3.5.5 Lite</b>\nBot aktif & monitoring...\nGunakan /status, /scan, /score"
    
    elif cmd == "/status":
        session = get_session_name()
        active = "✅ Aktif" if is_active_session() else "💤 Off-Session"
        news = "🚫 Blocked" if is_news_blocked() else "✅ Clear"
        errors = sum(fetch_error_count.values())
        return (
            f"📡 <b>STATUS BOT</b>\n"
            f"Sesi: {session} ({active})\n"
            f"News: {news}\n"
            f"Fetch Errors: {errors}\n"
            f"Waktu: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
    
    elif cmd == "/scan":
        if not is_active_session():
            return "💤 <b>Market Sepi</b>\nDi luar sesi London/NY. Bot istirahat."
        if is_news_blocked():
            return "🚫 <b>News Block Aktif</b>\nMenunggu 30 menit pasca-event high-impact."
        
        results = []
        for symbol in ASSETS:
            data = fetch_prices(symbol)
            if data is None:
                results.append(f"⚠️ {symbol}: Data kosong/fetch gagal")
                continue
            signal = analyze(symbol, data)
            del data
            gc.collect()
            if signal:
                results.append(format_signal_alert(signal))
            else:
                results.append(f"➖ {symbol}: Tidak ada sinyal valid")
        
        if not results:
            return "📭 <b>Data Kosong</b>\nSemua aset gagal di-fetch. Cek koneksi/API."
        return "\n\n".join(results)
    
    elif cmd == "/score":
        return (
            f"🎯 <b>KONFIGURASI SKOR</b>\n"
            f"Normal Min: {MIN_SCORE_BASE}\n"
            f"Low Vol Min: {MIN_SCORE_LOW_VOL}\n"
            f"Cooldown: 5-12 menit (dinamis)\n"
            f"Asets: {', '.join(ASSETS)}"
        )
    
    else:
        return "❓ Command tidak dikenal. Gunakan /start, /status, /scan, /score"


# ===================== MAIN LOOP =====================

def scan_loop():
    """Loop utama scanning dengan sequential fetch & memory safety."""
    logger.info("[LOOP] Scan loop dimulai")
    while True:
        try:
            if not is_active_session():
                logger.info(f"[LOOP] Off-session ({get_session_name()}), tidur 60s")
                time.sleep(60)
                continue
            
            if is_news_blocked():
                logger.info("[LOOP] News block aktif, tidur 60s")
                time.sleep(60)
                continue
            
            for symbol in ASSETS:
                # Cek cooldown
                last_time = last_signal_time.get(symbol)
                if last_time:
                    elapsed = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
                    min_cooldown = get_cooldown_minutes(MIN_SCORE_BASE)
                    if elapsed < min_cooldown:
                        continue
                
                # Fetch & Analyze
                data = fetch_prices(symbol)
                if data is None:
                    continue
                
                signal = analyze(symbol, data)
                del data
                gc.collect()
                
                if signal:
                    msg = format_signal_alert(signal)
                    send_telegram(msg)
                    last_signal_time[symbol] = datetime.now(timezone.utc)
                    logger.info(f"[SIGNAL] {symbol} {signal['direction']} Score={signal['score']}")
            
            # Sleep antar cycle
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"[LOOP ERROR] {e}")
            gc.collect()
            time.sleep(30)


# ===================== FLASK APP (WEBHOOK ENTRY POINT) =====================

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health_check():
    return {"status": "ok", "version": "v3.5.5-lite"}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    if "message" in update and "text" in update["message"]:
        text = update["message"]["text"]
        parts = text.split(" ", 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        reply = handle_command(cmd, args)
        send_telegram(reply)
    return "OK", 200


# ===================== ENTRY POINT =====================

if __name__ == "__main__":
    import threading
    
    # Validasi env vars
    if not CHAT_ID or not BOT_TOKEN:
        logger.critical("TELEGRAM_CHAT_ID atau TELEGRAM_BOT_TOKEN tidak diset! Exiting.")
        exit(1)
    
    # Start scan loop di background thread
    scanner_thread = threading.Thread(target=scan_loop, daemon=True)
    scanner_thread.start()
    logger.info("[MAIN] Scanner thread started")
    
    # Start Flask server
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
