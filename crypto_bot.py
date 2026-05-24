import os
import csv
import time
import json
import requests
import hmac
import hashlib
from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

API_KEY        = os.getenv("BINANCE_API_KEY")
API_SECRET     = os.getenv("BINANCE_API_SECRET")
groq           = Groq(api_key=os.getenv("GROQ_API_KEY"))
tavily         = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL       = "https://testnet.binancefuture.com"  # Change to https://fapi.binance.com for live
CAPITAL_USDT   = 100.0
LEVERAGE       = 10
SCAN_INTERVAL  = 60 * 15
TRAIL_INTERVAL = 60 * 1
TOP_COINS      = 20

RSI_OVERSOLD             = 32   # slightly looser
RSI_OVERBOUGHT           = 68   # slightly looser
MIN_VOLUME_USDT          = 5_000_000  # lower volume requirement
MIN_TIMEFRAMES_AGREE     = 2    # 2 out of 3 timeframes must agree
SENTIMENT_CONFIDENCE_MIN = 60

# Only trade quality coins — NO micro caps!
WHITELIST = [
    # Top tier
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    # Large caps
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
    "LTCUSDT", "ATOMUSDT", "UNIUSDT", "AAVEUSDT", "FILUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
    "SUIUSDT", "SEIUSDT", "TIAUSDT", "STXUSDT", "RUNEUSDT",
    # Mid caps with good volume
    "DOGEUSDT", "SHIBUSDT", "TRXUSDT", "XLMUSDT", "VETUSDT",
    "ICPUSDT", "LDOUSDT", "MKRUSDT", "SNXUSDT", "COMPUSDT",
    "CRVUSDT", "GALAUSDT", "SANDUSDT", "MANAUSDT", "AXSUSDT",
    "APEUSDT", "GMTUSDT", "FLOWUSDT", "ALGOUSDT", "FTMUSDT",
    "HBARUSDT", "QNTUSDT", "EGLDUSDT", "XTZUSDT", "EOSUDT",
    "ZILUSDT", "CHZUSDT", "ENJUSDT", "BATUSDT", "1INCHUSDT"
]

TRAIL_ACTIVATE_PCT = 0.008   # was 0.005 — wait for more profit before trailing
TRAIL_DISTANCE_PCT = 0.005   # was 0.003 — give trade more room

# Max trade duration — auto close if stuck too long
MAX_TRADE_HOURS = 3   # was 4 — exit faster if stuck

# Trade log CSV file
LOG_FILE = "trade_log.csv"

# ─────────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────────

def send_telegram(message):
    """Send a message to your Telegram bot"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return  # silently skip if not configured
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text":    message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─────────────────────────────────────────────
# TRADE LOG CSV
# ─────────────────────────────────────────────

def init_log():
    """Create CSV file with headers if it doesn't exist"""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "date", "time", "symbol", "direction",
                "entry_price", "stop_loss", "take_profit",
                "quantity", "capital_usdt", "leverage",
                "tf_agreement", "sentiment", "news_confidence",
                "outcome", "exit_price", "pnl_usdt", "pnl_pct",
                "duration_mins", "exit_reason"
            ])
        print(f" 📊 Trade log created: {LOG_FILE}")

def log_trade_open(trade):
    """Log when a trade is opened"""
    now = datetime.now()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            trade.get("symbol"),
            trade.get("direction"),
            trade.get("entry_price"),
            trade.get("stop_loss"),
            trade.get("take_profit"),
            trade.get("quantity"),
            CAPITAL_USDT,
            LEVERAGE,
            trade.get("tf_agreement", "3/3"),
            trade.get("sentiment", "UNKNOWN"),
            trade.get("news_confidence", 0),
            "OPEN",   # outcome — will update when closed
            "",       # exit_price
            "",       # pnl_usdt
            "",       # pnl_pct
            "",       # duration_mins
            ""        # exit_reason
        ])

def log_trade_close(symbol, exit_price, entry_price, direction, quantity, open_time, exit_reason):
    """Update the last open trade row with close details"""
    if direction == "LONG":
        pnl_pct  = (exit_price - entry_price) / entry_price * 100 * LEVERAGE
        pnl_usdt = (exit_price - entry_price) * quantity
    else:
        pnl_pct  = (entry_price - exit_price) / entry_price * 100 * LEVERAGE
        pnl_usdt = (entry_price - exit_price) * quantity

    outcome = "WIN" if pnl_usdt > 0 else "LOSS"
    duration = int((datetime.now() - open_time).total_seconds() / 60)

    # Read all rows, update last OPEN row for this symbol
    rows = []
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    for i in reversed(range(1, len(rows))):
        if rows[i][2] == symbol and rows[i][13] == "OPEN":
            rows[i][13] = outcome
            rows[i][14] = str(round(exit_price, 4))
            rows[i][15] = str(round(pnl_usdt, 4))
            rows[i][16] = str(round(pnl_pct, 2))
            rows[i][17] = str(duration)
            rows[i][18] = exit_reason
            break

    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    return pnl_usdt, pnl_pct, outcome, duration

def print_stats():
    """Print win rate and profit summary from CSV"""
    if not os.path.exists(LOG_FILE):
        return
    wins = losses = 0
    total_pnl = 0.0
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["outcome"] == "WIN":
                wins += 1
                total_pnl += float(row["pnl_usdt"] or 0)
            elif row["outcome"] == "LOSS":
                losses += 1
                total_pnl += float(row["pnl_usdt"] or 0)
    total = wins + losses
    if total == 0:
        return
    win_rate = (wins / total) * 100
    print(f"\n 📊 STATS: {total} trades | Win rate: {win_rate:.0f}% | Total PnL: ${total_pnl:+.2f}")
    send_telegram(f"📊 <b>Bot Stats</b>\nTrades: {total} | Win Rate: {win_rate:.0f}% | PnL: ${total_pnl:+.2f}")

# ─────────────────────────────────────────────
# BINANCE API HELPERS
# ─────────────────────────────────────────────

def sign(params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def get_headers():
    return {"X-MBX-APIKEY": API_KEY}

def api_get(path, params=None, signed=False):
    if params is None:
        params = {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params = sign(params)
    r = requests.get(BASE_URL + path, params=params, headers=get_headers(), timeout=10)
    return r.json()

def api_post(path, params):
    params["timestamp"] = int(time.time() * 1000)
    params = sign(params)
    r = requests.post(BASE_URL + path, params=params, headers=get_headers(), timeout=10)
    return r.json()

# ─────────────────────────────────────────────
# TRAILING STOP LOSS MANAGER
# ─────────────────────────────────────────────

class TrailingStopManager:
    def __init__(self):
        self.active          = False
        self.symbol          = None
        self.direction       = None
        self.entry_price     = None
        self.current_sl      = None
        self.best_price      = None
        self.sl_order_id     = None
        self.qty             = None
        self.price_precision = 2
        self.open_time       = None
        self.sentiment       = "UNKNOWN"
        self.news_confidence = 0
        self.tf_agreement    = "3/3"

    def start(self, symbol, direction, entry_price, initial_sl,
              sl_order_id, qty, price_precision,
              sentiment="UNKNOWN", news_confidence=0, tf_agreement="3/3"):
        self.active          = True
        self.symbol          = symbol
        self.direction       = direction
        self.entry_price     = entry_price
        self.current_sl      = initial_sl
        self.best_price      = entry_price
        self.sl_order_id     = sl_order_id
        self.qty             = qty
        self.price_precision = price_precision
        self.open_time       = datetime.now()
        self.sentiment       = sentiment
        self.news_confidence = news_confidence
        self.tf_agreement    = tf_agreement
        print(f"\n 🔁 Trailing SL started | {symbol} {direction} @ ${entry_price:.4f}")
        print(f"    Initial SL: ${initial_sl:.4f} | Activates at +{TRAIL_ACTIVATE_PCT*100:.1f}%")

    def reset(self):
        self.active = False
        self.symbol = self.direction = self.entry_price = None
        self.current_sl = self.best_price = self.sl_order_id = None
        self.qty = self.open_time = None

    def get_current_price(self):
        try:
            r = requests.get(
                BASE_URL + "/fapi/v1/ticker/price",
                params={"symbol": self.symbol}, timeout=5
            ).json()
            return float(r["price"])
        except:
            return None

    def place_new_sl(self, new_sl_price):
        try:
            if self.sl_order_id:
                try:
                    requests.delete(
                        BASE_URL + "/fapi/v1/order",
                        params=sign({
                            "symbol":    self.symbol,
                            "orderId":   self.sl_order_id,
                            "timestamp": int(time.time() * 1000)
                        }),
                        headers=get_headers(), timeout=5
                    )
                except:
                    pass

            sl_side  = "SELL" if self.direction == "LONG" else "BUY"
            sl_price = round(new_sl_price, self.price_precision)
            result   = api_post("/fapi/v1/order", {
                "symbol":      self.symbol,
                "side":        sl_side,
                "type":        "STOP_MARKET",
                "stopPrice":   sl_price,
                "quantity":    self.qty,
                "reduceOnly":  "true",
                "timeInForce": "GTC"
            })
            if "orderId" in result:
                self.sl_order_id = result["orderId"]
                self.current_sl  = new_sl_price
                return True
        except Exception as e:
            print(f"    SL update error: {e}")
        return False

    def is_expired(self):
        """Check if trade has been open too long"""
        if not self.open_time:
            return False
        hours_open = (datetime.now() - self.open_time).total_seconds() / 3600
        return hours_open >= MAX_TRADE_HOURS

    def force_close(self):
        """Force close position with market order"""
        try:
            side = "SELL" if self.direction == "LONG" else "BUY"
            result = api_post("/fapi/v1/order", {
                "symbol":     self.symbol,
                "side":       side,
                "type":       "MARKET",
                "quantity":   self.qty,
                "reduceOnly": "true"
            })
            if "orderId" in result:
                print(f"  ⏰ Force closed {self.symbol} after {MAX_TRADE_HOURS}h")
                return True
        except Exception as e:
            print(f"  Force close error: {e}")
        return False

    def update(self):
        if not self.active:
            return
        price = self.get_current_price()
        if not price:
            return

        # ── TIME LIMIT CHECK ──
        if self.is_expired():
            hours_open = (datetime.now() - self.open_time).total_seconds() / 3600
            print(f"\n  ⏰ TRADE EXPIRED after {hours_open:.1f}h — force closing!")
            send_telegram(
                f"⏰ <b>Trade Expired</b>\n"
                f"Symbol: {self.symbol} {self.direction}\n"
                f"Open for: {hours_open:.1f} hours\n"
                f"Force closing at market price..."
            )
            self.force_close()
            return

        if self.direction == "LONG":
            profit_pct = (price - self.entry_price) / self.entry_price
        else:
            profit_pct = (self.entry_price - price) / self.entry_price

        # Show time remaining
        hours_open    = (datetime.now() - self.open_time).total_seconds() / 3600
        hours_left    = MAX_TRADE_HOURS - hours_open

        if profit_pct < TRAIL_ACTIVATE_PCT:
            print(f"  ⏳ Waiting | ${price:.4f} | Profit: {profit_pct*100:+.2f}% | Time left: {hours_left:.1f}h")
            return

        if self.direction == "LONG":
            if price > self.best_price:
                self.best_price = price
            new_sl = self.best_price * (1 - TRAIL_DISTANCE_PCT)
            should_update = new_sl > self.current_sl
        else:
            if price < self.best_price:
                self.best_price = price
            new_sl = self.best_price * (1 + TRAIL_DISTANCE_PCT)
            should_update = new_sl < self.current_sl

        if should_update:
            old_sl  = self.current_sl
            success = self.place_new_sl(new_sl)
            if success:
                print(f"  🔁 Trail moved | ${price:.4f} | SL: ${old_sl:.4f} → ${new_sl:.4f} | Locked: {profit_pct*100:.2f}%")
                send_telegram(
                    f"🔁 <b>Trailing SL Updated</b>\n"
                    f"Symbol: {self.symbol} {self.direction}\n"
                    f"Price: ${price:.4f}\n"
                    f"New SL: ${new_sl:.4f}\n"
                    f"Profit locked: {profit_pct*100:.2f}%"
                )
        else:
            print(f"  ✅ Trail active | ${price:.4f} | SL: ${self.current_sl:.4f} | Profit: {profit_pct*100:+.2f}%")

# Global trailing stop manager
trail_manager = TrailingStopManager()

# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────

def get_klines(symbol, interval="1h", limit=100):
    try:
        data = requests.get(
            BASE_URL + "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        ).json()
        return (
            [float(c[4]) for c in data],
            [float(c[2]) for c in data],
            [float(c[3]) for c in data],
            [float(c[5]) for c in data]
        )
    except:
        return [], [], [], []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period + i] - closes[-period + i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def calc_macd(closes):
    if len(closes) < 26:
        return 0, 0
    macd_line = calc_ema(closes, 12) - calc_ema(closes, 26)
    signal    = calc_ema(closes[-9:], 9) if len(closes) >= 35 else macd_line
    return macd_line, signal

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return closes[-1] * 0.01 if closes else 1
    trs = []
    for i in range(1, period + 1):
        idx = -(period + 1) + i
        tr  = max(highs[idx] - lows[idx],
                  abs(highs[idx] - closes[idx - 1]),
                  abs(lows[idx]  - closes[idx - 1]))
        trs.append(tr)
    return sum(trs) / period

def calc_bollinger(closes, period=20):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    recent = closes[-period:]
    mid    = sum(recent) / period
    std    = (sum((x - mid) ** 2 for x in recent) / period) ** 0.5
    return mid + 2 * std, mid, mid - 2 * std

# ─────────────────────────────────────────────
# VOLUME CONFIRMATION
# ─────────────────────────────────────────────

def check_volume_confirmation(symbol, direction):
    """
    Check if volume is increasing in the trade direction.
    Rising volume = real move
    Falling volume = fake signal
    """
    try:
        closes, _, _, volumes = get_klines(symbol, "1h", 20)
        if len(volumes) < 10:
            print(f"  📊 Volume: Not enough data — allowing trade")
            return True, 0

        # Compare recent volume to average
        avg_volume    = sum(volumes[-10:]) / 10
        latest_volume = volumes[-1]
        prev_volume   = volumes[-2]
        volume_ratio  = latest_volume / avg_volume if avg_volume > 0 else 1

        # Check if volume is increasing in the right direction
        price_change = closes[-1] - closes[-2]
        volume_rising = latest_volume > prev_volume

        if direction == "LONG":
            # For LONG: want rising price with rising volume
            confirmed = price_change > 0 and volume_rising and volume_ratio >= 1.2
        else:
            # For SHORT: want falling price with rising volume
            confirmed = price_change < 0 and volume_rising and volume_ratio >= 1.2

        emoji = "✅" if confirmed else "⚠️"
        print(f"  📊 Volume: {emoji} Ratio: {volume_ratio:.2f}x avg | Rising: {volume_rising} | Ratio OK: {volume_ratio >= 1.2}")

        if not confirmed:
            print(f"    ⚠️  Volume not confirming {direction} — weak signal")
            # Don't block — just warn (volume can be tricky)
            return confirmed, volume_ratio

        print(f"    ✅ Volume confirms {direction} move!")
        return confirmed, volume_ratio

    except Exception as e:
        print(f"  Volume check error: {e}")
        return True, 0

# ─────────────────────────────────────────────
# CANDLE PATTERN RECOGNITION
# ─────────────────────────────────────────────

def detect_candle_patterns(symbol, direction):
    """
    Detect key reversal/continuation candle patterns.
    Patterns checked:
    - Hammer / Shooting Star
    - Bullish / Bearish Engulfing
    - Doji rejection
    - Morning Star / Evening Star
    """
    try:
        closes, highs, lows, volumes = get_klines(symbol, "1h", 10)
        if len(closes) < 4:
            return True, "No data"

        # Latest candles
        o1, c1, h1, l1 = closes[-2], closes[-1], highs[-1], lows[-1]
        o2, c2, h2, l2 = closes[-3], closes[-2], highs[-2], lows[-2]
        o3, c3         = closes[-4], closes[-3]

        body1    = abs(c1 - o1)
        body2    = abs(c2 - o2)
        range1   = h1 - l1 if h1 != l1 else 0.0001
        range2   = h2 - l2 if h2 != l2 else 0.0001
        upper_wick1 = h1 - max(o1, c1)
        lower_wick1 = min(o1, c1) - l1
        upper_wick2 = h2 - max(o2, c2)
        lower_wick2 = min(o2, c2) - l2

        patterns_found = []

        # ── BULLISH PATTERNS ──
        # Hammer: small body at top, long lower wick
        if lower_wick1 > body1 * 2 and upper_wick1 < body1 * 0.5 and lower_wick1 > range1 * 0.6:
            patterns_found.append(("BULLISH", "Hammer"))

        # Bullish Engulfing: green candle engulfs previous red candle
        if c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2:
            patterns_found.append(("BULLISH", "Bullish Engulfing"))

        # Bullish Doji: tiny body with long wicks = indecision → reversal
        if body1 < range1 * 0.1 and lower_wick1 > range1 * 0.4:
            patterns_found.append(("BULLISH", "Bullish Doji"))

        # Morning Star: red → doji → green
        if c3 < o3 and body2 < range2 * 0.2 and c1 > o1 and c1 > (o3 + c3) / 2:
            patterns_found.append(("BULLISH", "Morning Star"))

        # ── BEARISH PATTERNS ──
        # Shooting Star: small body at bottom, long upper wick
        if upper_wick1 > body1 * 2 and lower_wick1 < body1 * 0.5 and upper_wick1 > range1 * 0.6:
            patterns_found.append(("BEARISH", "Shooting Star"))

        # Bearish Engulfing: red candle engulfs previous green candle
        if c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2:
            patterns_found.append(("BEARISH", "Bearish Engulfing"))

        # Bearish Doji: tiny body with long upper wick
        if body1 < range1 * 0.1 and upper_wick1 > range1 * 0.4:
            patterns_found.append(("BEARISH", "Bearish Doji"))

        # Evening Star: green → doji → red
        if c3 > o3 and body2 < range2 * 0.2 and c1 < o1 and c1 < (o3 + c3) / 2:
            patterns_found.append(("BEARISH", "Evening Star"))

        if not patterns_found:
            print(f"  🕯️  Candles: ⚪ No pattern detected — allowing trade")
            return True, "None"

        # Check if any pattern confirms direction
        bullish_patterns = [p for p in patterns_found if p[0] == "BULLISH"]
        bearish_patterns = [p for p in patterns_found if p[0] == "BEARISH"]

        if direction == "LONG" and bullish_patterns:
            names = ", ".join(p[1] for p in bullish_patterns)
            print(f"  🕯️  Candles: ✅ BULLISH pattern — {names}")
            return True, names
        elif direction == "SHORT" and bearish_patterns:
            names = ", ".join(p[1] for p in bearish_patterns)
            print(f"  🕯️  Candles: ✅ BEARISH pattern — {names}")
            return True, names
        elif direction == "LONG" and bearish_patterns:
            names = ", ".join(p[1] for p in bearish_patterns)
            print(f"  🕯️  Candles: ❌ BEARISH pattern contradicts LONG — {names}")
            return False, names
        elif direction == "SHORT" and bullish_patterns:
            names = ", ".join(p[1] for p in bullish_patterns)
            print(f"  🕯️  Candles: ❌ BULLISH pattern contradicts SHORT — {names}")
            return False, names

        return True, "None"

    except Exception as e:
        print(f"  Candle check error: {e}")
        return True, "Error"

# ─────────────────────────────────────────────
# DYNAMIC POSITION SIZING
# ─────────────────────────────────────────────

def calc_position_size(signal, sentiment_confidence, volume_ratio, pattern_found):
    """
    Adjust position size based on signal confidence.
    High confidence → use more capital
    Low confidence  → use less capital
    """
    base_capital = CAPITAL_USDT
    score        = signal.get("score", 0)
    agreements   = signal.get("agreements", 2)

    # Start with base multiplier
    multiplier = 1.0

    # TF agreement bonus
    if agreements == 3:
        multiplier += 0.3   # 3/3 = +30%
    elif agreements == 2:
        multiplier -= 0.2   # 2/3 = -20%

    # Signal score bonus
    if score >= 15:
        multiplier += 0.2   # very strong signal
    elif score >= 10:
        multiplier += 0.1   # strong signal
    elif score < 6:
        multiplier -= 0.2   # weak signal

    # News sentiment confidence bonus
    if sentiment_confidence >= 80:
        multiplier += 0.1
    elif sentiment_confidence < 50:
        multiplier -= 0.1

    # Volume confirmation bonus
    if volume_ratio >= 2.0:
        multiplier += 0.1   # strong volume
    elif volume_ratio < 1.0:
        multiplier -= 0.1   # weak volume

    # Pattern found bonus
    if pattern_found and pattern_found not in ["None", "No data", "Error"]:
        multiplier += 0.1

    # Cap between 0.5x and 1.5x
    multiplier = max(0.5, min(1.5, multiplier))

    final_capital = round(base_capital * multiplier, 2)
    print(f"  💰 Position Size: ${final_capital} USDT (multiplier: {multiplier:.2f}x | base: ${base_capital})")

    return final_capital

# ─────────────────────────────────────────────
# FUNDING RATE CHECK
# ─────────────────────────────────────────────

def get_funding_rate(symbol):
    """
    Get current funding rate for a symbol.
    Positive funding = longs paying shorts = market too bullish = SHORT bias
    Negative funding = shorts paying longs = market too bearish = LONG bias
    """
    try:
        r = requests.get(
            BASE_URL + "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=5
        ).json()
        if isinstance(r, list) and len(r) > 0:
            rate = float(r[0]["fundingRate"]) * 100  # convert to %
            return rate
    except:
        pass
    return 0.0

def check_funding_rate(symbol, direction):
    """
    Returns True if funding rate confirms the trade direction.
    HIGH positive funding (>0.05%) → confirms SHORT
    HIGH negative funding (<-0.05%) → confirms LONG
    Neutral funding → allow trade anyway
    """
    rate = get_funding_rate(symbol)
    THRESHOLD = 0.05  # 0.05% funding rate threshold

    if rate > THRESHOLD:
        bias = "SHORT"
        emoji = "🔴"
    elif rate < -THRESHOLD:
        bias = "LONG"
        emoji = "🟢"
    else:
        bias = "NEUTRAL"
        emoji = "⚪"

    print(f"  💰 Funding Rate: {rate:+.4f}% → Bias: {emoji} {bias}")

    # Block trade if funding strongly contradicts direction
    if bias != "NEUTRAL" and bias != direction:
        print(f"    ❌ Funding rate contradicts {direction} signal — skipping")
        return False, rate

    if bias == direction:
        print(f"    ✅ Funding rate confirms {direction} signal")
    else:
        print(f"    ⚪ Neutral funding — allowing trade")

    return True, rate

# ─────────────────────────────────────────────
# BTC CORRELATION CHECK
# ─────────────────────────────────────────────

def get_btc_trend():
    """
    Get BTC trend on 1h chart.
    Returns: BULLISH, BEARISH, or NEUTRAL
    """
    try:
        closes, _, _, _ = get_klines("BTCUSDT", "1h", 50)
        if len(closes) < 20:
            return "NEUTRAL"

        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        rsi   = calc_rsi(closes)
        price = closes[-1]

        # BTC trend score
        bullish = 0
        bearish = 0

        if ema20 > ema50:   bullish += 1
        else:               bearish += 1

        if rsi > 55:        bullish += 1
        elif rsi < 45:      bearish += 1

        if price > ema20:   bullish += 1
        else:               bearish += 1

        if bullish >= 2:    return "BULLISH"
        elif bearish >= 2:  return "BEARISH"
        else:               return "NEUTRAL"
    except:
        return "NEUTRAL"

def check_btc_correlation(symbol, direction):
    """
    Don't trade altcoins against BTC trend.
    If BTC is strongly BEARISH → skip LONG on altcoins
    If BTC is strongly BULLISH → skip SHORT on altcoins
    BTC pairs are exempt from this check.
    """
    if symbol == "BTCUSDT":
        return True  # BTC doesn't need to check itself

    btc_trend = get_btc_trend()
    emoji     = "🟢" if btc_trend == "BULLISH" else "🔴" if btc_trend == "BEARISH" else "⚪"
    print(f"  ₿  BTC Trend: {emoji} {btc_trend}")

    if btc_trend == "BEARISH" and direction == "LONG":
        print(f"    ❌ BTC is BEARISH — risky to go LONG on altcoin")
        return False
    elif btc_trend == "BULLISH" and direction == "SHORT":
        print(f"    ❌ BTC is BULLISH — risky to go SHORT on altcoin")
        return False

    print(f"    ✅ BTC trend aligned with {direction} signal")
    return True

# ─────────────────────────────────────────────
# SUPPORT & RESISTANCE LEVELS
# ─────────────────────────────────────────────

def find_support_resistance(symbol, interval="1h", limit=100):
    """
    Auto detect key S/R levels using pivot points.
    Returns list of (price, type) tuples: type = 'support' or 'resistance'
    """
    try:
        closes, highs, lows, _ = get_klines(symbol, interval, limit)
        if len(closes) < 20:
            return []

        levels = []
        sensitivity = 3  # candles on each side to confirm level

        for i in range(sensitivity, len(highs) - sensitivity):
            # Resistance: local high
            if all(highs[i] >= highs[i-j] for j in range(1, sensitivity+1)) and \
               all(highs[i] >= highs[i+j] for j in range(1, sensitivity+1)):
                levels.append((highs[i], "resistance"))

            # Support: local low
            if all(lows[i] <= lows[i-j] for j in range(1, sensitivity+1)) and \
               all(lows[i] <= lows[i+j] for j in range(1, sensitivity+1)):
                levels.append((lows[i], "support"))

        # Remove duplicate levels (within 0.5% of each other)
        filtered = []
        for level, ltype in levels:
            too_close = any(abs(level - l) / l < 0.005 for l, _ in filtered)
            if not too_close:
                filtered.append((level, ltype))

        return filtered
    except:
        return []

def check_near_sr(symbol, direction, current_price):
    """
    Check if entry is near a strong S/R level.
    LONG entry near support = good
    SHORT entry near resistance = good
    Entry in middle of nowhere = risky
    """
    levels = find_support_resistance(symbol)
    if not levels:
        print(f"  📍 S/R: Could not detect levels — allowing trade")
        return True, None

    SR_ZONE_PCT = 0.008  # within 0.8% of S/R level counts as "near"

    nearest_support    = None
    nearest_resistance = None
    min_sup_dist       = float("inf")
    min_res_dist       = float("inf")

    for price, ltype in levels:
        dist = abs(current_price - price) / current_price
        if ltype == "support" and dist < min_sup_dist:
            min_sup_dist    = dist
            nearest_support = price
        elif ltype == "resistance" and dist < min_res_dist:
            min_res_dist    = dist
            nearest_resistance = price

    if direction == "LONG" and nearest_support:
        dist_pct = min_sup_dist * 100
        if min_sup_dist <= SR_ZONE_PCT:
            print(f"  📍 S/R: ✅ Near support ${nearest_support:.4f} ({dist_pct:.2f}% away) — good LONG entry")
            return True, nearest_support
        else:
            print(f"  📍 S/R: ⚠️  Nearest support ${nearest_support:.4f} is {dist_pct:.2f}% away — risky entry")
            return False, nearest_support

    elif direction == "SHORT" and nearest_resistance:
        dist_pct = min_res_dist * 100
        if min_res_dist <= SR_ZONE_PCT:
            print(f"  📍 S/R: ✅ Near resistance ${nearest_resistance:.4f} ({dist_pct:.2f}% away) — good SHORT entry")
            return True, nearest_resistance
        else:
            print(f"  📍 S/R: ⚠️  Nearest resistance ${nearest_resistance:.4f} is {dist_pct:.2f}% away — risky entry")
            return False, nearest_resistance

    print(f"  📍 S/R: ⚪ No nearby level found — allowing trade")
    return True, None

# ─────────────────────────────────────────────
# MULTI TIMEFRAME ANALYSIS
# ─────────────────────────────────────────────

TIMEFRAMES = {
    "4h":  {"interval": "4h",  "limit": 100, "label": "Big Trend"},
    "1h":  {"interval": "1h",  "limit": 100, "label": "Main Signal"},
    "15m": {"interval": "15m", "limit": 100, "label": "Entry Timing"},
}

def analyze_timeframe(symbol, interval, limit=100):
    closes, highs, lows, volumes = get_klines(symbol, interval, limit)
    if len(closes) < 30:
        return None, 0, []

    rsi              = calc_rsi(closes)
    ema20            = calc_ema(closes, 20)
    ema50            = calc_ema(closes, 50)
    macd, signal     = calc_macd(closes)
    bb_up, _, bb_low = calc_bollinger(closes)
    price            = closes[-1]
    vol_surge        = volumes[-1] > (sum(volumes[-20:]) / 20) * 1.5

    long_score, long_reasons = 0, []
    if rsi < RSI_OVERSOLD:   long_score += 2; long_reasons.append(f"RSI oversold ({rsi:.0f})")
    if ema20 > ema50:        long_score += 1; long_reasons.append("EMA bullish")
    if macd > signal:        long_score += 1; long_reasons.append("MACD bullish")
    if price < bb_low:       long_score += 2; long_reasons.append("Below BB")
    if vol_surge:            long_score += 1; long_reasons.append("Vol surge")

    short_score, short_reasons = 0, []
    if rsi > RSI_OVERBOUGHT: short_score += 2; short_reasons.append(f"RSI overbought ({rsi:.0f})")
    if ema20 < ema50:        short_score += 1; short_reasons.append("EMA bearish")
    if macd < signal:        short_score += 1; short_reasons.append("MACD bearish")
    if price > bb_up:        short_score += 2; short_reasons.append("Above BB")
    if vol_surge:            short_score += 1; short_reasons.append("Vol surge")

    if long_score >= 3 and long_score > short_score:
        return "LONG", long_score, long_reasons
    elif short_score >= 3 and short_score > long_score:
        return "SHORT", short_score, short_reasons
    return "NEUTRAL", 0, []

def analyze_coin_mtf(symbol):
    results = {}
    print(f"    Timeframes: ", end="")
    for tf_key, tf_cfg in TIMEFRAMES.items():
        direction, score, reasons = analyze_timeframe(symbol, tf_cfg["interval"], tf_cfg["limit"])
        results[tf_key] = {"direction": direction, "score": score, "reasons": reasons, "label": tf_cfg["label"]}
        emoji = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
        print(f"{tf_key}:{emoji} ", end="")
        time.sleep(0.2)
    print()

    directions  = [r["direction"] for r in results.values() if r["direction"] != "NEUTRAL"]
    long_count  = directions.count("LONG")
    short_count = directions.count("SHORT")

    if long_count >= MIN_TIMEFRAMES_AGREE:
        final_direction = "LONG"
        agreeing = {k: v for k, v in results.items() if v["direction"] == "LONG"}
    elif short_count >= MIN_TIMEFRAMES_AGREE:
        final_direction = "SHORT"
        agreeing = {k: v for k, v in results.items() if v["direction"] == "SHORT"}
    else:
        return None

    closes, highs, lows, _ = get_klines(symbol, "1h", 100)
    if not closes:
        return None

    price  = closes[-1]
    atr    = calc_atr(highs, lows, closes)
    sl_pct = atr / price
    tp_pct = (atr * 2) / price

    if final_direction == "LONG":
        stop_loss, take_profit = price * (1 - sl_pct), price * (1 + tp_pct)
    else:
        stop_loss, take_profit = price * (1 + sl_pct), price * (1 - tp_pct)

    all_reasons = [f"[{tf}] {r}" for tf, v in agreeing.items() for r in v["reasons"]]

    return {
        "symbol":      symbol,
        "direction":   final_direction,
        "price":       price,
        "score":       sum(v["score"] for v in agreeing.values()),
        "rsi":         calc_rsi(closes),
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "atr":         atr,
        "reasons":     all_reasons,
        "tf_results":  results,
        "agreements":  max(long_count, short_count)
    }

# ─────────────────────────────────────────────
# NEWS SENTIMENT
# ─────────────────────────────────────────────

def get_coin_name(symbol):
    mapping = {
        "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
        "BNB": "BNB Binance", "XRP": "XRP Ripple", "ADA": "Cardano",
        "DOGE": "Dogecoin", "AVAX": "Avalanche", "LINK": "Chainlink",
        "DOT": "Polkadot", "MATIC": "Polygon", "LTC": "Litecoin",
    }
    return mapping.get(symbol.replace("USDT", ""), symbol.replace("USDT", ""))

def search_crypto_news(symbol):
    coin, ticker = get_coin_name(symbol), symbol.replace("USDT", "")
    queries = [
        f"{coin} crypto news today",
        f"{ticker} price prediction latest",
        f"{coin} market analysis bullish bearish"
    ]
    all_news, total = "", 0
    for query in queries:
        try:
            results = tavily.search(query=query, search_depth="basic", max_results=2)
            for r in results.get("results", []):
                all_news += f"- {r.get('title','')}: {r.get('content','')[:250]}\n"
                total += 1
        except:
            pass
    print(f"    Found {total} news articles")
    return all_news if all_news else "No recent news found."

def analyze_sentiment(symbol, technical_direction, news_text):
    prompt = f"""You are a crypto trading news analyst.
Coin: {symbol} | Signal: {technical_direction}
NEWS: {news_text}
Does news SUPPORT or REJECT the {technical_direction} signal?
Respond ONLY valid JSON:
{{"sentiment":"BULLISH","confidence":75,"supports_trade":true,"reason":"reason","key_headline":"headline"}}"""
    try:
        r    = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200, temperature=0.2
        )
        text = r.choices[0].message.content.strip()
        return json.loads(text[text.find("{"):text.rfind("}")+1])
    except:
        return None

def check_news_sentiment(symbol, technical_direction):
    print(f"  📰 Checking news for {symbol}...")
    news      = search_crypto_news(symbol)
    sentiment = analyze_sentiment(symbol, technical_direction, news)
    if not sentiment:
        print(f"    Could not analyze — allowing trade")
        return True, "UNKNOWN", 0
    supports   = sentiment.get("supports_trade", True)
    confidence = int(sentiment.get("confidence", 0))
    sent       = sentiment.get("sentiment", "NEUTRAL")
    emoji      = "🟢" if supports else "🔴"
    print(f"    {emoji} Sentiment: {sent} ({confidence}%) | {sentiment.get('key_headline','')[:70]}")
    if not supports and confidence >= SENTIMENT_CONFIDENCE_MIN:
        print(f"    ❌ NEWS BLOCKS TRADE")
        return False, sent, confidence
    print(f"    ✅ NEWS CONFIRMS trade")
    return True, sent, confidence

# ─────────────────────────────────────────────
# ACCOUNT HELPERS
# ─────────────────────────────────────────────

def set_leverage(symbol, leverage):
    try:
        api_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
    except:
        pass

def get_open_positions():
    try:
        data = api_get("/fapi/v2/positionRisk", signed=True)
        return [p for p in data if float(p.get("positionAmt", 0)) != 0]
    except:
        return []

def get_balance():
    try:
        data = api_get("/fapi/v2/balance", signed=True)
        for asset in data:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0
    except:
        return 0

def get_symbol_info(symbol):
    try:
        info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s["quantityPrecision"], s["pricePrecision"]
    except:
        pass
    return 3, 2

# ─────────────────────────────────────────────
# PLACE TRADE
# ─────────────────────────────────────────────

def place_trade(signal, sentiment="UNKNOWN", news_confidence=0, capital_override=None):
    symbol    = signal["symbol"]
    direction = signal["direction"]
    price     = signal["price"]
    sl        = signal["stop_loss"]
    tp        = signal["take_profit"]

    # Use dynamic capital if provided, otherwise default
    capital = capital_override if capital_override else CAPITAL_USDT

    qty_precision, price_precision = get_symbol_info(symbol)
    set_leverage(symbol, LEVERAGE)

    notional = capital * LEVERAGE
    quantity = round(notional / price, qty_precision)
    if quantity == 0:
        quantity = round(1 / price, qty_precision)
    if quantity <= 0:
        print(f"  Quantity too small: {quantity}")
        return False, None, None, None

    side     = "BUY"  if direction == "LONG"  else "SELL"
    sl_side  = "SELL" if direction == "LONG"  else "BUY"
    tp_side  = "SELL" if direction == "LONG"  else "BUY"
    sl_price = round(sl, price_precision)
    tp_price = round(tp, price_precision)

    print(f"  Placing {direction} | {quantity} {symbol} @ ${price:.4f}")
    print(f"  SL: ${sl_price:.4f} | TP: ${tp_price:.4f}")

    entry = api_post("/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "MARKET", "quantity": quantity
    })
    if "orderId" not in entry:
        print(f"  Entry failed: {entry}")
        return False, None, None, None
    print(f"  Entry placed! Order ID: {entry['orderId']}")

    sl_result = api_post("/fapi/v1/order", {
        "symbol": symbol, "side": sl_side,
        "type": "STOP_MARKET", "stopPrice": sl_price,
        "quantity": quantity, "reduceOnly": "true", "timeInForce": "GTC"
    })
    sl_order_id = sl_result.get("orderId")
    print(f"  Stop Loss set @ ${sl_price:.4f}")

    api_post("/fapi/v1/order", {
        "symbol": symbol, "side": tp_side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": tp_price,
        "quantity": quantity, "reduceOnly": "true", "timeInForce": "GTC"
    })
    print(f"  Take Profit set @ ${tp_price:.4f}")

    # Log trade open
    log_trade_open({
        "symbol":          symbol,
        "direction":       direction,
        "entry_price":     price,
        "stop_loss":       sl_price,
        "take_profit":     tp_price,
        "quantity":        quantity,
        "tf_agreement":    f"{signal.get('agreements', 3)}/3",
        "sentiment":       sentiment,
        "news_confidence": news_confidence
    })

    # Telegram alert
    send_telegram(
        f"🚀 <b>NEW TRADE OPENED</b>\n"
        f"Symbol    : {symbol}\n"
        f"Direction : {direction}\n"
        f"Entry     : ${price:.4f}\n"
        f"Stop Loss : ${sl_price:.4f}\n"
        f"Take Profit: ${tp_price:.4f}\n"
        f"Sentiment : {sentiment} ({news_confidence}%)\n"
        f"TF Agree  : {signal.get('agreements', 3)}/3\n"
        f"Mode      : {'TESTNET' if 'testnet' in BASE_URL else 'LIVE'}"
    )

    return True, sl_order_id, quantity, price_precision

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def run_bot():
    init_log()

    print("=" * 60)
    print("   BINANCE FUTURES BOT - FULL v8")
    print("   ✅ Multi Timeframe  ✅ News Sentiment  ✅ Trailing SL")
    print("   ✅ Funding Rate     ✅ BTC Correlation ✅ S/R Levels")
    print("   ✅ Volume Confirm   ✅ Candle Patterns ✅ Dynamic Size")
    print(f"   Capital    : ${CAPITAL_USDT} USDT | Leverage: {LEVERAGE}x")
    print(f"   Timeframes : 15m + 1h + 4h (need {MIN_TIMEFRAMES_AGREE}/3)")
    print(f"   Trail SL   : activates at +{TRAIL_ACTIVATE_PCT*100:.1f}%, trails {TRAIL_DISTANCE_PCT*100:.1f}%")
    print(f"   Trade Log  : {LOG_FILE}")
    print(f"   Mode       : {'TESTNET' if 'testnet' in BASE_URL else '🔴 LIVE'}")
    print(f"   Started    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>Bot Started</b>\n"
        f"Capital: ${CAPITAL_USDT} | Leverage: {LEVERAGE}x\n"
        f"Mode: {'TESTNET' if 'testnet' in BASE_URL else 'LIVE'}\n"
        f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    scan_count = 0
    last_scan  = 0

    while True:
        now = time.time()

        # ── ALWAYS CHECK OPEN POSITIONS EVERY MINUTE ──
        positions = get_open_positions()

        # ── AUTO RESTART TRAIL MANAGER FROM EXISTING POSITION ──
        if positions and not trail_manager.active:
            p         = positions[0]
            sym       = p["symbol"]
            amt       = float(p["positionAmt"])
            pnl       = float(p.get("unRealizedProfit", 0))
            price     = float(p.get("markPrice", 0))
            entry     = float(p.get("entryPrice", 0))
            direction = "LONG" if amt > 0 else "SHORT"
            qty       = abs(amt)
            _, price_precision = get_symbol_info(sym)

            print(f"\n⏱  {datetime.now().strftime('%H:%M:%S')} — Existing position detected — restarting trail manager")
            print(f"  {sym} | {direction} | Entry: ${entry:.4f} | Price: ${price:.4f} | PnL: ${pnl:+.4f}")

            # Calculate SL from entry using ATR
            closes, highs, lows, _ = get_klines(sym, "1h", 100)
            atr = calc_atr(highs, lows, closes) if closes else entry * 0.003
            if direction == "LONG":
                initial_sl = entry * (1 - atr / entry)
            else:
                initial_sl = entry * (1 + atr / entry)

            trail_manager.start(
                symbol          = sym,
                direction       = direction,
                entry_price     = entry,
                initial_sl      = initial_sl,
                sl_order_id     = None,
                qty             = qty,
                price_precision = price_precision
            )
            print(f"  ✅ Trail manager restarted! Monitoring every minute...")

        # ── TRAILING SL CHECK (every 1 min) ──
        if trail_manager.active:
            print(f"\n⏱  {datetime.now().strftime('%H:%M:%S')} — Trailing SL check")
            trail_manager.update()

            positions = get_open_positions()
            if not any(p["symbol"] == trail_manager.symbol for p in positions):
                # Position closed — figure out exit price and log it
                print(f"\n Position closed! Logging result...")
                try:
                    ticker    = requests.get(
                        BASE_URL + "/fapi/v1/ticker/price",
                        params={"symbol": trail_manager.symbol}, timeout=5
                    ).json()
                    exit_price = float(ticker["price"])
                except:
                    exit_price = trail_manager.entry_price

                pnl_usdt, pnl_pct, outcome, duration = log_trade_close(
                    symbol      = trail_manager.symbol,
                    exit_price  = exit_price,
                    entry_price = trail_manager.entry_price,
                    direction   = trail_manager.direction,
                    quantity    = trail_manager.qty,
                    open_time   = trail_manager.open_time,
                    exit_reason = "SL/TP/Manual"
                )

                result_emoji = "✅ WIN" if outcome == "WIN" else "❌ LOSS"
                print(f" {result_emoji} | PnL: ${pnl_usdt:+.4f} ({pnl_pct:+.2f}%) | Duration: {duration}m")

                send_telegram(
                    f"{result_emoji}\n"
                    f"Symbol   : {trail_manager.symbol}\n"
                    f"Direction: {trail_manager.direction}\n"
                    f"Entry    : ${trail_manager.entry_price:.4f}\n"
                    f"Exit     : ${exit_price:.4f}\n"
                    f"PnL      : ${pnl_usdt:+.4f} ({pnl_pct:+.2f}%)\n"
                    f"Duration : {duration} minutes"
                )

                trail_manager.reset()
                print_stats()

        # ── FULL SCAN (every 15 min) ──
        if now - last_scan >= SCAN_INTERVAL:
            last_scan = now
            scan_count += 1
            print(f"\n{'='*60}")
            print(f" SCAN #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*60}")

            balance   = get_balance()
            positions = get_open_positions()
            print(f" Balance  : ${balance:.2f} USDT")
            print(f" Positions: {len(positions)}")
            for p in positions:
                pnl = float(p.get("unRealizedProfit", 0))
                print(f"   {p['symbol']} | Amt: {p['positionAmt']} | PnL: ${pnl:+.4f}")

            if len(positions) > 0:
                print("\n Position open — monitoring trailing SL...")
            else:
                trail_manager.reset()
                print(f"\n Scanning {TOP_COINS} coins across 3 timeframes...")

                try:
                    tickers    = requests.get(BASE_URL + "/fapi/v1/ticker/24hr", timeout=10).json()
                    coins = [
                        t["symbol"] for t in tickers
                        if t["symbol"] in WHITELIST
                        and float(t["quoteVolume"]) >= MIN_VOLUME_USDT
                    ]
                    print(f"  Whitelisted coins available: {len(coins)}")
                except:
                    coins = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

                signals = []
                for symbol in coins:
                    print(f"\n  [{symbol}]")
                    sig = analyze_coin_mtf(symbol)
                    if sig:
                        print(f"    ✅ {sig['direction']} ({sig['agreements']}/3, score: {sig['score']})")
                        signals.append(sig)
                    else:
                        print(f"    ⚪ No MTF agreement")
                    time.sleep(0.5)

                print(f"\n{'─'*60}")
                print(f" MTF Signals: {len(signals)}")

                if not signals:
                    print(" No signals this scan.")
                else:
                    best = max(signals, key=lambda x: x["score"])
                    print(f"\n BEST: {best['symbol']} {best['direction']} | Score: {best['score']} | {best['agreements']}/3 TFs")

                    # ── FILTER 1: NEWS SENTIMENT ──
                    approved, sentiment, news_conf = check_news_sentiment(
                        best["symbol"], best["direction"]
                    )

                    # ── FILTER 2: FUNDING RATE ──
                    if approved:
                        funding_ok, funding_rate = check_funding_rate(
                            best["symbol"], best["direction"]
                        )
                        if not funding_ok:
                            approved = False

                    # ── FILTER 3: BTC CORRELATION ──
                    if approved:
                        btc_ok = check_btc_correlation(
                            best["symbol"], best["direction"]
                        )
                        if not btc_ok:
                            approved = False

                    # ── FILTER 4: SUPPORT & RESISTANCE ──
                    if approved:
                        sr_ok, sr_level = check_near_sr(
                            best["symbol"], best["direction"], best["price"]
                        )
                        if not sr_ok:
                            approved = False

                    # ── FILTER 5: VOLUME CONFIRMATION ──
                    volume_ratio = 0
                    if approved:
                        vol_ok, volume_ratio = check_volume_confirmation(
                            best["symbol"], best["direction"]
                        )
                        if not vol_ok:
                            print(f"  ⚠️  Weak volume — proceeding with reduced size")

                    # ── FILTER 6: CANDLE PATTERNS ──
                    pattern_name = "None"
                    if approved:
                        candle_ok, pattern_name = detect_candle_patterns(
                            best["symbol"], best["direction"]
                        )
                        if not candle_ok:
                            approved = False

                    # ── TRY SECOND BEST IF BLOCKED ──
                    if not approved:
                        remaining = [s for s in signals if s["symbol"] != best["symbol"]]
                        if remaining:
                            second = max(remaining, key=lambda x: x["score"])
                            print(f"\n Trying next: {second['symbol']} {second['direction']}")
                            approved, sentiment, news_conf = check_news_sentiment(second["symbol"], second["direction"])
                            if approved:
                                funding_ok, _ = check_funding_rate(second["symbol"], second["direction"])
                                approved = approved and funding_ok
                            if approved:
                                approved = check_btc_correlation(second["symbol"], second["direction"])
                            if approved:
                                sr_ok, _ = check_near_sr(second["symbol"], second["direction"], second["price"])
                                approved = approved and sr_ok
                            if approved:
                                vol_ok, volume_ratio = check_volume_confirmation(second["symbol"], second["direction"])
                            if approved:
                                candle_ok, pattern_name = detect_candle_patterns(second["symbol"], second["direction"])
                                approved = approved and candle_ok
                            if approved:
                                best = second

                    if approved:
                        if balance < 5.0:
                            print(" Balance too low — skipping.")
                        else:
                            # ── DYNAMIC POSITION SIZING ──
                            dynamic_capital = calc_position_size(
                                best, news_conf, volume_ratio, pattern_name
                            )

                            success, sl_order_id, quantity, price_precision = place_trade(
                                best, sentiment, news_conf, dynamic_capital
                            )
                            if success:
                                print(f" ✅ Trade placed! Pattern: {pattern_name}")
                                trail_manager.start(
                                    symbol          = best["symbol"],
                                    direction       = best["direction"],
                                    entry_price     = best["price"],
                                    initial_sl      = best["stop_loss"],
                                    sl_order_id     = sl_order_id,
                                    qty             = quantity,
                                    price_precision = price_precision,
                                    sentiment       = sentiment,
                                    news_confidence = news_conf,
                                    tf_agreement    = f"{best['agreements']}/3"
                                )

        time.sleep(TRAIL_INTERVAL)

if __name__ == "__main__":
    run_bot()
