#!/usr/bin/env python3
"""Внешний мониторинг сайтов Fattakhov HR Agency: работает вне серверов Selectel.

Каждый запуск (раз в 5 минут из GitHub Actions):
  1. проверяет сайты и срок TLS-сертификатов, дописывает результат в историю;
  2. при изменении состояния пишет в Telegram (🔴 упало, 🟢 восстановилось);
  3. остаётся на связи ~4,5 минуты и отвечает на кнопки бота: статус сейчас, график за сутки и за неделю,
     «что это значит». Отвечает только владельцу (TELEGRAM_CHAT_ID).
Состояние и история (7 суток) лежат в state.json (кэш Actions). Секреты берутся из окружения и не печатаются.
"""
import io, json, os, socket, ssl, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

SITES = {
    "Лендинг": "https://fattakhovhr.ru/",
    "Кабинет CRM": "https://my.fattakhovhr.ru/api/health",
    "Фоновые задачи CRM": "https://my.fattakhovhr.ru/api/health/worker",
    "Студенческая платформа": "https://students.fattakhovhr.ru/api/health",
}
CERT_HOSTS = ["fattakhovhr.ru", "my.fattakhovhr.ru", "students.fattakhovhr.ru"]
CERT_WARN_DAYS = 14
STATE = "state.json"
KEEP = timedelta(days=7)
LISTEN_SECONDS = 240
MSK = timezone(timedelta(hours=3))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Разрешённые чаты: один или несколько id через запятую; уведомления идут всем, кнопки отвечают каждому из них
CHATS = [c.strip() for c in str(os.environ.get("TELEGRAM_CHAT_ID", "")).split(",") if c.strip()]
CHAT = CHATS[0] if CHATS else ""

BTN_NOW, BTN_DAY, BTN_WEEK, BTN_HELP = "📊 Сейчас", "📈 График за сутки", "📅 График за неделю", "ℹ️ Что это значит"
KEYBOARD = {"keyboard": [[{"text": BTN_NOW}, {"text": BTN_DAY}], [{"text": BTN_WEEK}, {"text": BTN_HELP}]], "resize_keyboard": True}

HELP = (
    "Как читать мониторинг\n\n"
    "Каждые 5 минут я захожу на 4 части проекта со стороны интернета, как обычный посетитель:\n"
    "• Лендинг: главный сайт fattakhovhr.ru.\n"
    "• Кабинет CRM: вход для клиентов и сотрудников.\n"
    "• Фоновые задачи CRM: напоминания и рассылки. Если стоят, уведомления не приходят.\n"
    "• Студенческая платформа: сайт для студентов.\n\n"
    "Сайт «работает», если ответил кодом 200. Три неудачные попытки подряд считаются падением, тогда приходит 🔴.\n\n"
    "График:\n"
    "• Верхняя часть: скорость ответа в секундах. Ниже линия, тем лучше: до 1 с хорошо, больше 3 с медленно.\n"
    "• Нижние полосы: зелёное, сайт отвечал; красное, не отвечал; серое, данных нет (сам мониторинг не запускался).\n"
    "• Доступность 99,9% за месяц допускает около 43 минут простоя.\n\n"
    "Каждый день в 9:00 по Москве приходит сводка. Если сводки нет, значит, сломан сам мониторинг."
)


def now_msk():
    return datetime.now(MSK)


def api(method, params=None, files=None, timeout=40):
    """Вызов Telegram Bot API. Токен нигде не печатается."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        if files:
            boundary = "----fhr" + str(int(time.time() * 1000))
            body = b""
            for k, v in (params or {}).items():
                body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
            for k, (fname, data, ctype) in files.items():
                body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f"Content-Type: {ctype}\r\n\r\n").encode("utf-8") + data + b"\r\n"
            body += f"--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(url, urllib.parse.urlencode(params or {}).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        print("Telegram:", method, type(e).__name__)
        return {}


def send(text, keyboard=True, chat=None):
    """Без chat: всем разрешённым (уведомления). С chat: только этому чату (ответ на кнопку)."""
    if not TOKEN or not CHATS:
        print("Telegram не настроен:", text)
        return
    for target in ([chat] if chat else CHATS):
        p = {"chat_id": target, "text": text}
        if keyboard:
            p["reply_markup"] = json.dumps(KEYBOARD)
        api("sendMessage", p)


def fetch(url):
    """До трёх попыток с паузой: одиночный сбой сети не должен будить. Возвращает (ошибка|None, секунд до ответа)."""
    last = "нет ответа"
    for attempt in range(3):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fhr-uptime/2"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return None, round(time.time() - t0, 3)
                last = f"код {r.status}"
        except urllib.error.HTTPError as e:
            last = f"код {e.code}"
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        if attempt < 2:
            time.sleep(10)
    return last, None


def cert_days(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            exp = datetime.strptime(t.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (exp - datetime.now(timezone.utc)).days
    except Exception:  # noqa: BLE001
        return None


def load():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save(state):
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def run_check(state):
    """Один проход: результаты в историю, уведомления при смене состояния. Возвращает текущие проблемы."""
    problems, point = {}, {}
    for name, url in SITES.items():
        err, secs = fetch(url)
        point[name] = [0 if err else 1, secs]
        if err:
            problems[name] = err
    for host in CERT_HOSTS:
        d = cert_days(host)
        if d is not None and d < CERT_WARN_DAYS:
            problems[f"Сертификат {host}"] = f"истекает через {d} дн."
    now = now_msk()
    hist = state.setdefault("history", [])
    hist.append({"t": int(time.time()), "s": point})
    cutoff = time.time() - KEEP.total_seconds()
    state["history"] = [h for h in hist if h["t"] >= cutoff]
    prev = state.get("problems", {})
    for name, err in problems.items():
        if name not in prev:
            send(f"🔴 {name}: {err} ({now:%d.%m %H:%M} МСК)")
    for name in prev:
        if name not in problems:
            send(f"🟢 {name}: снова в порядке ({now:%d.%m %H:%M} МСК)")
    today = now.strftime("%Y-%m-%d")
    if now.hour >= 9 and state.get("daily") != today:
        state["daily"] = today
        status = "всё работает" if not problems else "есть проблемы: " + ", ".join(problems)
        send(f"📋 Ежедневная сводка: {status}")
    state["problems"] = problems
    return problems


def stats(state, hours):
    """Доступность и скорость по сайтам за период."""
    cutoff = time.time() - hours * 3600
    pts = [h for h in state.get("history", []) if h["t"] >= cutoff]
    out = {}
    for name in SITES:
        oks = [h["s"][name][0] for h in pts if name in h["s"]]
        secs = [h["s"][name][1] for h in pts if name in h["s"] and h["s"][name][1] is not None]
        out[name] = {
            "n": len(oks),
            "uptime": (100.0 * sum(oks) / len(oks)) if oks else None,
            "avg": (sum(secs) / len(secs)) if secs else None,
            "fails": sum(1 for o in oks if o == 0),
        }
    return out, len(pts)


def status_text(state):
    now = now_msk()
    problems = state.get("problems", {})
    last = state.get("history", [])[-1:] or [None]
    lines = [f"📊 Состояние на {now:%d.%m %H:%M} МСК"]
    s24, _ = stats(state, 24)
    for name in SITES:
        h = last[0]["s"].get(name) if last[0] else None
        icon = "🔴" if name in problems else ("🟢" if h else "⚪")
        info = s24[name]
        sec = f", {h[1]:.2f} с" if h and h[1] is not None else ""
        up = f", за сутки {info['uptime']:.2f}%" if info["uptime"] is not None else ""
        lines.append(f"{icon} {name}{sec}{up}")
    certs = [f"{k}: {v}" for k, v in problems.items() if k.startswith("Сертификат")]
    lines.append("🔒 Сертификаты в порядке" if not certs else "🔒 " + "; ".join(certs))
    lines.append("\nКнопки внизу: график за сутки или неделю, «Что это значит» объяснит всё простыми словами.")
    return "\n".join(lines)


def ensure_matplotlib():
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", "matplotlib"], check=False)


def make_chart(state, hours):
    ensure_matplotlib()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    now = time.time()
    start = now - hours * 3600
    pts = [h for h in state.get("history", []) if h["t"] >= start]
    names = list(SITES)
    colors = ["#2563eb", "#7c3aed", "#d97706", "#059669"]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.2), gridspec_kw={"height_ratios": [2.2, 1.6]}, sharex=True)
    ts = [datetime.fromtimestamp(h["t"], MSK) for h in pts]
    for name, c in zip(names, colors):
        pairs = [(t, h["s"][name][1]) for t, h in zip(ts, pts) if name in h["s"] and h["s"][name][1] is not None]
        if pairs:
            ax1.plot([p[0] for p in pairs], [p[1] for p in pairs], color=c, linewidth=1.4, marker="o" if len(pairs) < 60 else None, markersize=3, label=name)
    ax1.axhline(1, color="#059669", linewidth=0.8, linestyle="--")
    ax1.axhline(3, color="#dc2626", linewidth=0.8, linestyle="--")
    ax1.set_ylabel("Ответ сайта, секунд")
    ax1.set_title(f"Мониторинг сайтов за {'сутки' if hours <= 24 else 'неделю'} (время московское)")
    ax1.legend(loc="upper left", fontsize=8, ncol=2)
    ax1.grid(alpha=0.25)
    gap = 12 * 60  # паузы длиннее 12 минут = мониторинг не запускался
    for i, name in enumerate(names):
        for j, h in enumerate(pts):
            if name not in h["s"]:
                continue
            nxt = pts[j + 1]["t"] if j + 1 < len(pts) else min(now, h["t"] + 300)
            width = min(nxt - h["t"], gap)
            ok = h["s"][name][0] == 1
            ax2.barh(i, width / 86400, left=mdates.date2num(datetime.fromtimestamp(h["t"], MSK)),
                     color="#22c55e" if ok else "#ef4444", height=0.7, linewidth=0)
    ax2.set_facecolor("#e5e7eb")
    ax2.set_yticks(range(len(names)))
    ax2.set_yticklabels(names, fontsize=8)
    ax2.invert_yaxis()
    # Ось начинается там, где есть данные: на недельном графике при истории в час не должно быть пустой недели
    first = pts[0]["t"] if pts else start
    xmin = max(start, first - max(600, (now - first) * 0.03))
    ax2.set_xlim(mdates.date2num(datetime.fromtimestamp(xmin, MSK)), mdates.date2num(datetime.fromtimestamp(now, MSK)))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if hours <= 24 else "%d.%m", tz=MSK))
    ax2.set_xlabel("зелёное: отвечал   красное: не отвечал   серое: данных нет")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def chart_caption(state, hours):
    st, n = stats(state, hours)
    period = "сутки" if hours <= 24 else "неделю"
    lines = [f"График за {period}. Верх: скорость ответа (до 1 с хорошо, больше 3 с медленно). Низ: зелёное работал, красное не отвечал, серое данных нет.", ""]
    for name, info in st.items():
        if info["uptime"] is None:
            lines.append(f"• {name}: данных пока нет")
        else:
            avg = f", в среднем {info['avg']:.2f} с" if info["avg"] is not None else ""
            fails = f", сбоев {info['fails']}" if info["fails"] else ""
            lines.append(f"• {name}: доступность {info['uptime']:.2f}%{avg}{fails}")
    if n < 12:
        lines.append("\nИстории ещё мало: график станет информативнее через несколько часов работы.")
    return "\n".join(lines)[:1000]


def send_chart(state, hours, chat):
    img = make_chart(state, hours)
    r = api("sendPhoto", {"chat_id": chat, "caption": chart_caption(state, hours), "reply_markup": json.dumps(KEYBOARD)},
            files={"photo": ("chart.png", img, "image/png")}, timeout=60)
    if not r.get("ok"):
        send("Не получилось нарисовать график, попробуйте позже.", chat=chat)


def handle(text, state, chat):
    t = (text or "").strip()
    if t in (BTN_NOW, "/status"):
        send(status_text(state), chat=chat)
    elif t in (BTN_DAY, "/day"):
        send_chart(state, 24, chat)
    elif t in (BTN_WEEK, "/week"):
        send_chart(state, 24 * 7, chat)
    elif t in (BTN_HELP, "/help"):
        send(HELP, chat=chat)
    elif t == "/start":
        send("Привет! Я слежу за сайтами Fattakhov HR Agency. Кнопки внизу: состояние, графики и пояснение.\n\n" + status_text(state))


def listen(state, seconds):
    """Отвечает на кнопки, пока не вышло время запуска. Только владельцу."""
    end = time.time() + seconds
    offset = state.get("offset", 0)
    while time.time() < end:
        wait = int(min(25, max(1, end - time.time() - 1)))
        r = api("getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": json.dumps(["message"])}, timeout=wait + 15)
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            chat_id = str(m.get("chat", {}).get("id"))
            if chat_id not in CHATS:
                # Чужому не показываем данные, только его номер: его можно передать владельцу, чтобы открыть доступ
                if chat_id != "None" and m.get("text"):
                    send(f"Доступ к мониторингу не открыт. Ваш номер: {chat_id}. Передайте его владельцу проекта.", keyboard=False, chat=chat_id)
                continue
            try:
                handle(m.get("text"), state, chat_id)
            except Exception as e:  # noqa: BLE001
                # Сбой одной кнопки не должен останавливать бота
                print("ошибка обработки кнопки:", type(e).__name__)
                send("Не получилось выполнить команду, попробуйте ещё раз чуть позже.", chat=chat_id)
        state["offset"] = offset
        save(state)


def main():
    state = load()
    run_check(state)
    save(state)
    print("проблем:", len(state.get("problems", {})), state.get("problems", {}))
    if TOKEN and CHATS and "--no-listen" not in sys.argv:
        api("setMyCommands", {"commands": json.dumps([
            {"command": "status", "description": "Состояние сейчас"},
            {"command": "day", "description": "График за сутки"},
            {"command": "week", "description": "График за неделю"},
            {"command": "help", "description": "Что это значит"},
        ])})
        listen(state, LISTEN_SECONDS)
    save(state)


if __name__ == "__main__":
    main()
