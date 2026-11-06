import json
import os
import sys
import threading
import time
from datetime import datetime
import urllib.request
import urllib.parse
from flask import Flask, jsonify, render_template, request

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOGS_PATH = os.path.join(APP_DIR, "alert_logs.json")

DEFAULT_TARGETS = {
    "QQQUSDT": {
        "symbol": "QQQUSDT",
        "name": "나스닥 QQQ",
        "target_price": 660.0,
        "condition": "gte",  # "gte": >= (이상), "lte": <= (이하)
        "enabled": True
    },
    "NFLXUSDT": {
        "symbol": "NFLXUSDT",
        "name": "넷플릭스 NFLX",
        "target_price": 55.0,
        "condition": "lte",  # 55불 이하 도달 감지
        "enabled": True
    }
}

DEFAULT_CONFIG = {
    "bot_token": "9277416965:AAHb27u-WoMSQmNyTrjZ8TM8yKtag-s6XxM",
    "chat_id": "",
    "chat_user": "",
    "cooldown_minutes": 10,
    "rearm_mode": "cross_again",  # "cross_again" or "interval"
    "sound_enabled": True,
    "targets": DEFAULT_TARGETS
}

app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"))
state_lock = threading.Lock()

def init_symbol_state():
    return {
        "current_price": 0.0,
        "high_24h": 0.0,
        "low_24h": 0.0,
        "price_change_pct": 0.0,
        "volume_24h": 0.0,
        "last_updated": None,
        "api_status": "연결 대기 중",
        "price_history": [],
        "last_alert_time": 0,
        "last_alert_price": None,
        "last_price_state": None,
        "alert_count": 0
    }

state = {
    "bot_username": "Alert_123455_bot",
    "symbols": {
        "QQQUSDT": init_symbol_state(),
        "NFLXUSDT": init_symbol_state()
    },
    "logs": []
}

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Handle migration from single-target config
                if "target_price" in data and "targets" not in data:
                    targets = DEFAULT_TARGETS.copy()
                    targets["QQQUSDT"]["target_price"] = float(data.get("target_price", 660.0))
                    targets["QQQUSDT"]["condition"] = data.get("condition", "gte")
                    data["targets"] = targets
                cfg.update(data)
                # Ensure targets has both QQQ and NFLX
                if "targets" not in cfg:
                    cfg["targets"] = DEFAULT_TARGETS.copy()
                else:
                    for k, v in DEFAULT_TARGETS.items():
                        if k not in cfg["targets"]:
                            cfg["targets"][k] = v.copy()
                return cfg
        except Exception as e:
            print(f"Error loading config: {e}")
    return cfg

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

config = load_config()

def load_logs():
    if os.path.exists(LOGS_PATH):
        try:
            with open(LOGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_logs(logs):
    try:
        with open(LOGS_PATH, "w", encoding="utf-8") as f:
            json.dump(logs[:150], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving logs: {e}")

state["logs"] = load_logs()

def send_telegram_message(token, chat_id, text):
    if not token or not chat_id:
        return False, "봇 토큰 또는 Chat ID가 설정되지 않았습니다."
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode())
            if res_data.get("ok"):
                return True, "메시지 발송 성공"
            else:
                return False, res_data.get("description", "발송 실패")
    except Exception as e:
        return False, str(e)

def get_telegram_updates(token):
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset=-10"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            res_data = json.loads(resp.read().decode())
            if res_data.get("ok"):
                return True, res_data.get("result", [])
            return False, res_data.get("description", "조회 실패")
    except Exception as e:
        return False, str(e)

def fetch_ticker_multi(symbol):
    """
    Fetches real-time 24h ticker for QQQUSDT / NFLXUSDT with automatic multi-exchange fallback.
    1. Binance Futures
    2. Bybit (Cloud-friendly, never blocks Render/AWS)
    3. Gate.io (Cloud-friendly)
    4. Yahoo Finance (Ultimate fallback)
    """
    # 1. Binance Futures
    try:
        url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return {
                "source": "Binance",
                "price": float(data["lastPrice"]),
                "high": float(data["highPrice"]),
                "low": float(data["lowPrice"]),
                "priceChangePct": float(data["priceChangePercent"]),
                "volume": float(data["volume"])
            }
    except Exception:
        pass

    # 2. Bybit (Cloud-friendly: identical price, zero cloud block)
    try:
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            item = data["result"]["list"][0]
            return {
                "source": "Bybit",
                "price": float(item["lastPrice"]),
                "high": float(item["highPrice24h"]),
                "low": float(item["lowPrice24h"]),
                "priceChangePct": float(item["price24hPcnt"]) * 100,
                "volume": float(item["volume24h"])
            }
    except Exception:
        pass

    # 3. Gate.io (Cloud-friendly)
    try:
        gate_sym = symbol.replace("USDT", "_USDT")
        url = f"https://api.gateio.ws/api/v4/futures/usdt/tickers?contract={gate_sym}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())[0]
            return {
                "source": "Gate.io",
                "price": float(data["last"]),
                "high": float(data["high_24h"]),
                "low": float(data["low_24h"]),
                "priceChangePct": float(data["change_percentage"]),
                "volume": float(data["volume_24h"])
            }
    except Exception:
        pass

    # 4. Yahoo Finance (100% Never blocked on Cloud)
    try:
        ticker = "QQQ" if "QQQ" in symbol else "NFLX"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            d = json.loads(resp.read().decode())
            meta = d["chart"]["result"][0]["meta"]
            price = float(meta.get("regularMarketPrice", 0))
            prev_close = float(meta.get("chartPreviousClose", price))
            pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            high = float(meta.get("regularMarketDayHigh", price))
            low = float(meta.get("regularMarketDayLow", price))
            vol = float(meta.get("regularMarketVolume", 0))
            if price > 0:
                return {
                    "source": "Yahoo Finance (NASDAQ)",
                    "price": price,
                    "high": high,
                    "low": low,
                    "priceChangePct": pct,
                    "volume": vol
                }
    except Exception:
        pass

    raise RuntimeError("모든 거래소 및 금융 API 호출 실패")

fetch_binance_ticker = fetch_ticker_multi

def monitor_loop():
    while True:
        targets_copy = {}
        with state_lock:
            targets_copy = {k: v.copy() for k, v in config.get("targets", {}).items()}
            chat_id = config.get("chat_id", "")
            bot_token = config.get("bot_token", "")
            rearm_mode = config.get("rearm_mode", "cross_again")
            cooldown_sec = float(config.get("cooldown_minutes", 10)) * 60

        for symbol, t_conf in targets_copy.items():
            if not t_conf.get("enabled", True):
                continue
            
            try:
                ticker = fetch_binance_ticker(symbol)
                now_dt = datetime.now()
                time_str = now_dt.strftime("%H:%M:%S")
                now_ts = time.time()
                current_p = ticker["price"]
                if current_p <= 0:
                    continue

                with state_lock:
                    if symbol not in state["symbols"]:
                        state["symbols"][symbol] = init_symbol_state()
                    s_state = state["symbols"][symbol]

                    s_state["current_price"] = current_p
                    s_state["high_24h"] = ticker["high"]
                    s_state["low_24h"] = ticker["low"]
                    s_state["price_change_pct"] = ticker["priceChangePct"]
                    s_state["volume_24h"] = ticker["volume"]
                    s_state["last_updated"] = time_str
                    s_state["api_status"] = f"정상 연결 ({ticker.get('source', 'Binance')})"

                    # Update price history (keep last 40)
                    hist = s_state["price_history"]
                    hist.append({"time": time_str, "price": current_p})
                    if len(hist) > 40:
                        s_state["price_history"] = hist[-40:]

                    # Evaluate alert condition
                    target_p = float(t_conf.get("target_price", 0.0))
                    condition = t_conf.get("condition", "gte")
                    asset_name = t_conf.get("name", symbol)

                    is_target_met = False
                    if condition == "gte":
                        is_target_met = (current_p >= target_p)
                        current_side = "above" if current_p >= target_p else "below"
                    else:
                        is_target_met = (current_p <= target_p)
                        current_side = "below" if current_p <= target_p else "above"

                    should_fire = False
                    trigger_reason = ""

                    if is_target_met:
                        if rearm_mode == "cross_again":
                            if s_state["last_price_state"] != current_side:
                                should_fire = True
                                trigger_reason = f"목표가 ${target_p:.2f} {'이상 돌파' if condition == 'gte' else '이하 도달'} 감지"
                        else:
                            if (now_ts - s_state["last_alert_time"]) >= cooldown_sec:
                                should_fire = True
                                trigger_reason = f"목표가 조건 유지 중 ({config.get('cooldown_minutes')}분 주기 알림)"

                    s_state["last_price_state"] = current_side

                    if should_fire and chat_id:
                        s_state["last_alert_time"] = now_ts
                        s_state["last_alert_price"] = current_p
                        s_state["alert_count"] += 1

                        diff = current_p - target_p
                        diff_str = f"+${diff:.2f}" if diff >= 0 else f"-${abs(diff):.2f}"
                        pct_diff = ((current_p - target_p) / target_p) * 100 if target_p else 0
                        cond_str = "이상 돌파" if condition == "gte" else "이하 도달"

                        msg = (
                            f"🚨 <b>[바이낸스 {asset_name}({symbol}) 목표가 알림]</b> 🚨\n\n"
                            f"🎯 <b>목표가:</b> ${target_p:,.2f} USDT ({cond_str})\n"
                            f"📊 <b>현재가:</b> ${current_p:,.2f} USDT ({diff_str}, {pct_diff:+.2f}%)\n"
                            f"📈 <b>24h 변동률:</b> {ticker['priceChangePct']:+.2f}%\n"
                            f"🔺 <b>24h 최고/최저:</b> ${ticker['high']:,.2f} / ${ticker['low']:,.2f}\n"
                            f"⏰ <b>도달 시각:</b> {now_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"ℹ️ <b>트리거 사유:</b> {trigger_reason}\n\n"
                            f"🔗 <a href='https://www.binance.com/en/futures/{symbol}'>바이낸스 {symbol} 차트 열기</a>"
                        )

                        ok, result_msg = send_telegram_message(bot_token, chat_id, msg)
                        
                        log_entry = {
                            "id": int(now_ts),
                            "symbol": symbol,
                            "name": asset_name,
                            "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "price": current_p,
                            "target": target_p,
                            "condition": condition,
                            "success": ok,
                            "detail": result_msg if not ok else "텔레그램 전송 완료"
                        }
                        state["logs"].insert(0, log_entry)
                        save_logs(state["logs"])

            except Exception as e:
                err_msg = str(e)
                if "451" in err_msg or "restricted" in err_msg.lower():
                    err_msg = "클라우드 IP 차단됨 (HTTP 451)"
                with state_lock:
                    if symbol in state["symbols"]:
                        state["symbols"][symbol]["api_status"] = f"오류: {err_msg[:40]}"

        time.sleep(1.5)

# Flask API Routes
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    with state_lock:
        res_symbols = {}
        for sym, t_conf in config.get("targets", {}).items():
            s_state = state["symbols"].get(sym, init_symbol_state())
            curr = s_state["current_price"]
            target = float(t_conf.get("target_price", 0.0))
            cond = t_conf.get("condition", "gte")
            dist = target - curr
            dist_pct = (dist / target * 100) if target else 0

            res_symbols[sym] = {
                "symbol": sym,
                "name": t_conf.get("name", sym),
                "target_price": target,
                "condition": cond,
                "enabled": t_conf.get("enabled", True),
                "current_price": curr,
                "distance": dist,
                "distance_pct": dist_pct,
                "high_24h": s_state["high_24h"],
                "low_24h": s_state["low_24h"],
                "price_change_pct": s_state["price_change_pct"],
                "volume_24h": s_state["volume_24h"],
                "last_updated": s_state["last_updated"],
                "api_status": s_state["api_status"],
                "alert_count": s_state["alert_count"],
                "price_history": s_state["price_history"]
            }

        return jsonify({
            "symbols": res_symbols,
            "chat_id": config.get("chat_id", ""),
            "chat_user": config.get("chat_user", ""),
            "bot_username": state["bot_username"],
            "cooldown_minutes": config.get("cooldown_minutes", 10),
            "rearm_mode": config.get("rearm_mode", "cross_again"),
            "sound_enabled": config.get("sound_enabled", True),
            "total_alert_count": len(state["logs"]),
            "logs": state["logs"][:25]
        })

@app.route("/api/update-symbol-config", methods=["POST"])
def api_update_symbol_config():
    data = request.get_json() or {}
    sym = data.get("symbol")
    if not sym or sym not in config["targets"]:
        return jsonify({"ok": False, "error": "유효하지 않은 심볼입니다."})

    t = config["targets"][sym]
    if "target_price" in data:
        t["target_price"] = float(data["target_price"])
    if "condition" in data:
        t["condition"] = str(data["condition"])
    if "enabled" in data:
        t["enabled"] = bool(data["enabled"])

    save_config(config)
    return jsonify({"ok": True, "target": t})

@app.route("/api/config", methods=["POST"])
def api_update_config():
    data = request.get_json() or {}
    global config
    for k in ["chat_id", "chat_user", "cooldown_minutes", "rearm_mode", "sound_enabled"]:
        if k in data:
            if k == "sound_enabled":
                config[k] = bool(data[k])
            elif k == "cooldown_minutes":
                config[k] = int(data[k])
            else:
                config[k] = str(data[k])
    save_config(config)
    return jsonify({"ok": True, "config": config})

@app.route("/api/telegram-detect", methods=["GET"])
def api_telegram_detect():
    token = config.get("bot_token")
    ok, updates = get_telegram_updates(token)
    if not ok:
        return jsonify({"ok": False, "error": updates})
    
    if not updates:
        return jsonify({
            "ok": False,
            "error": "아직 봇으로 수신된 메시지가 없습니다. 텔레그램에서 @Alert_123455_bot 검색 후 [/start]를 전송한 뒤 다시 눌러주세요!"
        })

    last_msg = None
    for u in reversed(updates):
        if "message" in u and "chat" in u["message"]:
            last_msg = u["message"]
            break
        elif "channel_post" in u and "chat" in u["channel_post"]:
            last_msg = u["channel_post"]
            break

    if not last_msg:
        return jsonify({"ok": False, "error": "유효한 채팅 메시지를 찾을 수 없습니다. 봇에 메시지를 먼저 보내주세요."})

    chat_id = str(last_msg["chat"]["id"])
    sender = last_msg.get("from", {})
    name = sender.get("first_name", "") + (" " + sender.get("last_name", "") if sender.get("last_name") else "")
    if sender.get("username"):
        name += f" (@{sender['username']})"
    if not name:
        name = last_msg["chat"].get("title", f"Chat {chat_id}")

    config["chat_id"] = chat_id
    config["chat_user"] = name
    save_config(config)

    # Send confirmation message
    confirm_text = (
        f"✅ <b>[바이낸스 QQQ & NFLX 알리미 연동 완료]</b>\n\n"
        f"반갑습니다! 텔레그램 알림이 성공적으로 연결되었습니다.\n"
        f"• <b>QQQUSDT:</b> 목표가 ${config['targets']['QQQUSDT']['target_price']:,.2f}\n"
        f"• <b>NFLXUSDT:</b> 목표가 ${config['targets']['NFLXUSDT']['target_price']:,.2f}\n\n"
        f"목표가 도달 시 즉시 알림을 발송합니다."
    )
    send_telegram_message(token, chat_id, confirm_text)

    return jsonify({
        "ok": True,
        "chat_id": chat_id,
        "chat_user": name,
        "message": f"연결 완료! {name} (ID: {chat_id})"
    })

@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not chat_id:
        return jsonify({"ok": False, "error": "Chat ID가 등록되지 않았습니다. 먼저 연동해주세요."})

    data = request.get_json() or {}
    sym = data.get("symbol", "NFLXUSDT")
    t_conf = config["targets"].get(sym, config["targets"]["NFLXUSDT"])
    s_state = state["symbols"].get(sym, init_symbol_state())

    curr = s_state["current_price"]
    target = t_conf.get("target_price", 55.0)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"🧪 <b>[텔레그램 테스트 알림 - {t_conf.get('name', sym)}]</b>\n\n"
        f"알림 봇이 정상적으로 연동되어 대기 중입니다!\n"
        f"📊 <b>현재 {sym}:</b> ${curr:,.2f} USDT\n"
        f"🎯 <b>감시 목표가:</b> ${target:,.2f} USDT\n"
        f"⏰ <b>발송 시각:</b> {now_str}\n\n"
        f"실제 목표가 도달 시 이와 같이 자동으로 알림이 전송됩니다."
    )
    ok, err = send_telegram_message(token, chat_id, msg)
    if ok:
        return jsonify({"ok": True, "message": f"{t_conf.get('name', sym)} 테스트 메시지가 성공적으로 발송되었습니다."})
    else:
        return jsonify({"ok": False, "error": err})

@app.route("/api/sync-price", methods=["POST"])
def api_sync_price():
    data = request.get_json() or {}
    sym = data.get("symbol")
    price = data.get("price")
    if sym in state["symbols"] and price and float(price) > 0:
        p = float(price)
        with state_lock:
            s_state = state["symbols"][sym]
            s_state["current_price"] = p
            if "high" in data: s_state["high_24h"] = float(data["high"])
            if "low" in data: s_state["low_24h"] = float(data["low"])
            if "pct" in data: s_state["price_change_pct"] = float(data["pct"])
            s_state["last_updated"] = datetime.now().strftime("%H:%M:%S")
            s_state["api_status"] = "정상 연결 (브라우저 직통)"
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/api/clear-logs", methods=["POST"])
def api_clear_logs():
    with state_lock:
        state["logs"] = []
    save_logs([])
    return jsonify({"ok": True})

@app.route("/api/reset-alert-state", methods=["POST"])
def api_reset_alert_state():
    with state_lock:
        for s in state["symbols"].values():
            s["last_price_state"] = None
            s["last_alert_time"] = 0
    return jsonify({"ok": True, "message": "모든 종목의 알림 감시 상태가 초기화되었습니다. 조건 충족 시 다시 즉시 발송됩니다."})

def keep_alive_loop():
    """Keeps the app awake on free tiers (e.g. Render) by pinging itself every 9 minutes."""
    while True:
        time.sleep(540)
        ext_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL") or os.environ.get("KOYEB_PUBLIC_DOMAIN")
        if ext_url:
            if not ext_url.startswith("http"):
                ext_url = "https://" + ext_url
            try:
                with urllib.request.urlopen(f"{ext_url}/api/status", timeout=10) as r:
                    print(f"[Keep-Alive] Self-ping status {r.status} (stays awake 24/7)")
            except Exception as e:
                print(f"[Keep-Alive] Ping notice: {e}")

# Ensure monitor & keepalive threads start even under gunicorn / WSGI
_started_threads = False
def start_background_workers():
    global _started_threads
    if not _started_threads:
        _started_threads = True
        t_mon = threading.Thread(target=monitor_loop, daemon=True)
        t_mon.start()
        t_keep = threading.Thread(target=keep_alive_loop, daemon=True)
        t_keep.start()

start_background_workers()

if __name__ == "__main__":
    # Initial check of bot username
    try:
        req = urllib.request.urlopen(f"https://api.telegram.org/bot{config['bot_token']}/getMe", timeout=5)
        binfo = json.loads(req.read().decode())
        if binfo.get("ok"):
            state["bot_username"] = binfo["result"].get("username", "Alert_123455_bot")
    except Exception:
        pass

    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print("[Binance Multi-Asset Alert Server Started]")
    print(f"Web Dashboard: http://localhost:{port}")
    print(f"Monitored Symbols: {list(config['targets'].keys())}")
    print(f"Bot Username: @{state['bot_username']}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
