#!/usr/bin/env python3
"""Внешняя проверка сайтов Fattakhov HR Agency: работает вне серверов Selectel.

Заходит на сайты, проверяет ответ 200 и срок TLS-сертификата, при изменении состояния
пишет в Telegram. Состояние между запусками лежит в state.json (кэш Actions).
Секреты (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) берутся из переменных окружения и не печатаются.
"""
import json, os, socket, ssl, sys, time, urllib.parse, urllib.request
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
MSK = timezone(timedelta(hours=3))


def fetch(url):
    """Три попытки с паузой: одиночный сбой сети не должен будить."""
    last = "нет ответа"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fhr-uptime/1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return None
                last = f"код {r.status}"
        except urllib.error.HTTPError as e:
            last = f"код {e.code}"
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        if attempt < 2:
            time.sleep(10)
    return last


def cert_days(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            exp = datetime.strptime(t.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (exp - datetime.now(timezone.utc)).days
    except Exception:  # noqa: BLE001
        return None


def send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram не настроен, сообщение не отправлено:", text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data), timeout=20)
    except Exception as e:  # noqa: BLE001
        print("не удалось отправить в Telegram:", type(e).__name__)


def main():
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {}
    problems = {}
    for name, url in SITES.items():
        err = fetch(url)
        if err:
            problems[name] = err
    for host in CERT_HOSTS:
        d = cert_days(host)
        if d is not None and d < CERT_WARN_DAYS:
            problems[f"Сертификат {host}"] = f"истекает через {d} дн."
    now = datetime.now(MSK)
    prev = state.get("problems", {})
    for name, err in problems.items():
        if name not in prev:
            send(f"🔴 {name}: {err} ({now:%d.%m %H:%M} МСК)")
    for name in prev:
        if name not in problems:
            send(f"🟢 {name}: снова в порядке ({now:%d.%m %H:%M} МСК)")
    # Ежедневная сводка в 9:00 МСК: её отсутствие значит, что сломан сам мониторинг
    today = now.strftime("%Y-%m-%d")
    if now.hour >= 9 and state.get("daily") != today:
        state["daily"] = today
        status = "всё работает" if not problems else "есть проблемы: " + ", ".join(problems)
        send(f"📋 Ежедневная сводка: {status}")
    state["problems"] = problems
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
    print("проблем:", len(problems), problems)
    sys.exit(0)


if __name__ == "__main__":
    main()
