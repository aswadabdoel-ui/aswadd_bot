import os
import time
import datetime
import threading
import requests
from flask import Flask, request as flask_request
import telebot

# ==================== KONFIGURASI ====================
CHAT_ID        = os.environ.get("CHAT_ID", "971243017")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
COOLDOWN_MIN   = int(os.environ.get("COOLDOWN_MIN", "15"))

ASSETS = {
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
    "SOL-USD":  "Solana",
    "EURUSD=X": "EUR/USD",
    "CADJPY=X": "CAD/JPY",
}

# ==================== FLASK APP ====================
app = Flask(__name__)
bot = None
cooldown_tracker = {}

def get_bot():
    global bot
    if bot is None:
        token = os.environ.get("BOT_TOKEN", "")
        if not token or token == "MASUKKAN_TOKEN_DI_RENDER":
            raise ValueError("BOT_TOKEN tidak ditemukan di Environment Variables!")
        bot = telebot.TeleBot(token)
        setup_handlers(bot)
    return bot

# ==================== TELEGRAM HANDLERS ====================
def setup_handlers(b):
    @b.message_handler(commands=["start"])
    def cmd_start(message):
        b.reply_to(message,
            "\U0001f916 *Aswadd Signal Bot*\n\n"
            "Bot aktif 24/7 di cloud!\n"
            "TF: 5 Menit | Strategy: EMA Cross + RSI + Trend\n\n"
            "/status - Cek status bot\n"
            "/assets - List aset yang dipantau",
            parse_mode="Markdown")

    @b.message_handler(commands=["status"])
    def cmd_status(message):
        b.reply_to(message, "\u2705 Bot aktif dan scanning market tiap 5 menit...")

    @b.message_handler(commands=["assets"])
    def cmd_assets(message):
        assets_list = "\n".join([f"\u2022 {name} (`{sym}`)" for sym, name in ASSETS.items()])
        b.reply_to(message, f"\U0001f4ca *Aset Dipantau:*\n{assets_list}", parse_mode="Markdown")

# ==================== DATA FETCHING ====================
def fetch_prices(symbol, days=2):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"range": f"{days}d", "interval": "5m"}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        result = data["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]
        closes = [c for c in quotes["close"] if c is not None]
        highs  = [h for h in quotes["high"]  if h is not None]
        lows   = [l for l in quotes["low"]   if l is not None]
        volumes= [v for v in quotes.get("volume", []) if v is not None]
        return closes, highs, lows, volumes
    except Exception as e:
        print(f"[FETCH ERROR] {symbol}: {e}")
        return None, None, None, None

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
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        trs.append(tr)
    return sum(trs[-period:]) / period

# ==================== ANALISIS SINYAL ====================
def analyze(symbol):
    closes, highs, lows, volumes = fetch_prices(symbol)
    if not closes or len(closes) < 60:
        return None

    price = closes[-1]
    rsi   = calc_rsi(closes)
    atr   = calc_atr(highs, lows, closes)

    ema9_series  = calc_ema_series(closes, 9)
    ema21_series = calc_ema_series(closes, 21)
    ema50        = calc_ema(closes, 50)

    if len(ema9_series) < 3 or len(ema21_series) < 3:
        return None

    prev_diff = ema9_series[-3] - ema21_series[-3]
    curr_diff = ema9_series[-1] - ema21_series[-1]

    golden_cross = prev_diff <= 0 and curr_diff > 0
    death_cross  = prev_diff >= 0 and curr_diff < 0

    signal = None
    reasons = []

    if golden_cross and 40 <= rsi <= 70 and price > ema50:
        signal = "CALL"
        reasons = [
            "EMA9 cross ke ATAS EMA21",
            f"RSI {rsi} (zona aman 40-70)",
            "Harga di atas EMA50 (trend naik)",
        ]
    elif death_cross and 30 <= rsi <= 60 and price < ema50:
        signal = "PUT"
        reasons = [
            "EMA9 cross ke BAWAH EMA21",
            f"RSI {rsi} (zona aman 30-60)",
            "Harga di bawah EMA50 (trend turun)",
        ]

    if not signal:
        return {
            "price": price, "rsi": rsi, "atr": atr,
            "ema9": ema9_series[-1], "ema21": ema21_series[-1], "ema50": ema50,
            "signal": "WAIT", "reasons": []
        }

    if signal == "CALL":
        sl = round(price - 1.5 * atr, 6)
        tp = round(price + 3.0 * atr, 6)
    else:
        sl = round(price + 1.5 * atr, 6)
        tp = round(price - 3.0 * atr, 6)

    return {
        "price": price, "rsi": rsi, "atr": round(atr, 6),
        "ema9": round(ema9_series[-1], 6),
        "ema21": round(ema21_series[-1], 6),
        "ema50": round(ema50, 6),
        "sl": sl, "tp": tp,
        "signal": signal, "reasons": reasons
    }

# ==================== COOLDOWN ====================
def is_on_cooldown(symbol):
    last = cooldown_tracker.get(symbol, 0)
    return (time.time() - last) < (COOLDOWN_MIN * 60)

def set_cooldown(symbol):
    cooldown_tracker[symbol] = time.time()

# ==================== FORMAT PESAN ====================
def format_signal(name, symbol, result):
    sig   = result["signal"]
    emoji = "\U0001f7e2" if sig == "CALL" else "\U0001f534"
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p     = result["price"]
    lines = [
        f"{emoji} *{sig} SIGNAL* \u2014 {name}",
        f"\U0001f4ca `{symbol}` | TF: 5 Menit",
        "\u2501" * 19,
        f"\U0001f4b0 Entry: `{p}`",
        f"\U0001f3af TP: `{result['tp']}`",
        f"\U0001f6d1 SL: `{result['sl']}`",
        f"\U0001f4c8 RR: 1:2",
        "\u2501" * 19,
        f"\U0001f4c9 RSI(14): `{result['rsi']}`",
        f"\U0001f4c8 EMA9: `{result['ema9']}`",
        f"\U0001f4c9 EMA21: `{result['ema21']}`",
        f"\U0001f4ca EMA50: `{result['ema50']}`",
        f"\U0001f4cf ATR(14): `{result['atr']}`",
        "\u2501" * 19,
        "*\u2705 Konfirmasi:*",
    ]
    for r in result["reasons"]:
        lines.append(f"\u2022 {r}")
    lines += ["\u2501" * 19, f"\u23f0 {now}", "_@Aswadd_bot_"]
    return "\n".join(lines)

# ==================== BACKGROUND THREAD ====================
def analysis_worker():
    b = get_bot()
    print(f"[ANALYSIS] Started | Interval={CHECK_INTERVAL}s | Cooldown={COOLDOWN_MIN}m")
    try:
        assets_str = ", ".join(ASSETS.values())
        b.send_message(
            CHAT_ID,
            f"\U0001f916 *Aswadd Bot Aktif 24/7!*\n\n"
            f"\U0001f4ca Memantau: {assets_str}\n"
            f"\u23f1 TF: 5 Menit\n"
            f"\U0001f504 Interval: {CHECK_INTERVAL//60} menit\n"
            f"\u23f3 Cooldown: {COOLDOWN_MIN} menit/pair\n\n"
            f"_Menunggu sinyal berkualitas..._",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[STARTUP MSG ERROR] {e}")

    while True:
        for symbol, name in ASSETS.items():
            try:
                if is_on_cooldown(symbol):
                    continue
                result = analyze(symbol)
                if result is None:
                    continue
                sig = result["signal"]
                now = datetime.datetime.now().strftime("%H:%M:%S")
                if sig in ("CALL", "PUT"):
                    msg = format_signal(name, symbol, result)
                    b.send_message(CHAT_ID, msg, parse_mode="Markdown")
                    set_cooldown(symbol)
                    print(f"[{now}] \u2705 {sig} sent: {name} @ {result['price']}")
                else:
                    print(f"[{now}] \u23f3 WAIT: {name} | RSI={result['rsi']}")
            except Exception as e:
                print(f"[ERROR] {symbol}: {e}")
        time.sleep(CHECK_INTERVAL)

# ==================== FLASK ROUTES ====================
@app.route("/")
def health():
    return {"status": "alive", "bot": "@Aswadd_bot"}, 200

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
