#!/usr/bin/env python3
"""Alerta para cuando aparezca GTA VI en la base de datos de PEGI.

Vigilancias:
  1. Web de PEGI: avisa si aparece "Grand Theft Auto VI" (o "6") o si el
     numero de resultados sube por encima del ultimo conocido (base: 51).
  2. Google News (opcional): avisa de noticias nuevas que mencionen PEGI y
     GTA VI, como pista temprana.

Si PEGI falla varias veces seguidas, avisa una vez y reduce el ritmo de
peticiones (reintenta cada hora) hasta que se recupere.

Uso:
  python pegi_watch.py          # ejecucion normal
  python pegi_watch.py --test   # envia un mensaje de prueba al webhook
"""
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

PEGI_URL = "https://pegi.info/es/search-pegi?q=Grand+Theft+Auto"
NEWS_URL = "https://news.google.com/rss/search?q=GTA+VI+PEGI&hl=es&gl=ES&ceid=ES:es"
BASELINE_COUNT = 51
FAILS_BEFORE_WARNING = 3
BACKOFF_SECONDS = 3600

STATE_FILE = Path("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
NEWS_ENABLED = os.environ.get("NEWS_ALERTS", "true").lower() == "true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (alerta personal de baja frecuencia)",
    "Accept-Language": "es-ES,es;q=0.9",
}

# "Grand Theft Auto V" es subcadena de "... VI", por eso se exige el limite de palabra.
GTA6_RE = re.compile(r"Grand\s+Theft\s+Auto\s*(?:VI\b|6\b)", re.I)
COUNT_RE = re.compile(r"(\d+)\s+(?:resultados?|results?)", re.I)
NEWS_TITLE_RE = re.compile(r"PEGI", re.I)
NEWS_GAME_RE = re.compile(r"GTA\s*(?:VI\b|6\b)|Grand\s+Theft\s+Auto\s*(?:VI\b|6\b)", re.I)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def notify(message):
    print(f"[AVISO] {message}")
    if not WEBHOOK:
        print("(Sin DISCORD_WEBHOOK_URL: solo se muestra por consola)")
        return
    try:
        r = requests.post(WEBHOOK, json={"content": message[:1900]}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"No se pudo enviar a Discord: {exc}")


def describe_response(r):
    text = r.text
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else "sin titulo"
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()[:200]
    return f"HTTP {r.status_code}, {len(text)} bytes, titulo='{title}', inicio='{plain}'"


def check_pegi(state):
    fails = state.get("fail_count", 0)
    now = time.time()
    if fails >= FAILS_BEFORE_WARNING and now - state.get("last_attempt", 0) < BACKOFF_SECONDS:
        print("PEGI: en pausa por fallos seguidos, se reintentara mas tarde")
        return
    state["last_attempt"] = now

    r = None
    try:
        r = requests.get(PEGI_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
        if "grand theft auto" not in html.lower():
            raise ValueError("La pagina no contiene 'Grand Theft Auto'; puede haber cambiado el formato o haber un bloqueo")
    except (requests.RequestException, ValueError) as exc:
        state["fail_count"] = fails + 1
        print(f"Fallo leyendo PEGI ({state['fail_count']}): {exc}")
        detail = ""
        if r is not None:
            detail = describe_response(r)
            print(f"Detalle: {detail}")
        if state["fail_count"] == FAILS_BEFORE_WARNING:
            notify(
                f"⚠️ El vigilante de PEGI lleva {FAILS_BEFORE_WARNING} fallos seguidos "
                f"y no puede leer la web. Reintentara cada hora.\n"
                f"Ultimo error: {exc}\n{detail[:300]}"
            )
        return

    if fails >= FAILS_BEFORE_WARNING:
        notify("✅ El vigilante de PEGI vuelve a leer la web con normalidad.")
    state["fail_count"] = 0

    match = COUNT_RE.search(html)
    count = int(match.group(1)) if match else None
    found_vi = bool(GTA6_RE.search(html))
    last_count = state.get("last_count", BASELINE_COUNT)
    print(f"PEGI: resultados={count} (ultimo conocido={last_count}), Grand Theft Auto VI en el texto={found_vi}")

    if found_vi and not state.get("pegi_vi_found"):
        state["pegi_vi_found"] = True
        notify(f"🚨 ¡Ya aparece GTA VI en PEGI!\n{PEGI_URL}")

    if count is not None:
        if count > last_count:
            notify(
                f"🔔 PEGI ha pasado de {last_count} a {count} resultados para "
                f"'Grand Theft Auto'. Puede ser GTA VI u otra entrada.\n{PEGI_URL}"
            )
        state["last_count"] = count
    elif "last_count" not in state:
        state["last_count"] = BASELINE_COUNT


def check_news(state):
    first_run = "seen_news" not in state
    seen = list(state.get("seen_news", []))
    try:
        r = requests.get(NEWS_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"Fallo leyendo noticias: {exc}")
        return

    new_items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link or link in seen:
            continue
        if NEWS_TITLE_RE.search(title) and NEWS_GAME_RE.search(title):
            new_items.append((title, link))
            seen.append(link)

    state["seen_news"] = seen[-200:]
    if first_run:
        print(f"Noticias: primera ejecucion, {len(new_items)} guardadas sin avisar")
        return
    for title, link in new_items[:5]:
        notify(f"📰 Posible pista sobre PEGI y GTA VI:\n{title}\n{link}")


def main():
    if "--test" in sys.argv:
        notify("✅ Prueba del vigilante de PEGI: el webhook funciona.")
        return
    state = load_state()
    check_pegi(state)
    if NEWS_ENABLED:
        check_news(state)
    save_state(state)


if __name__ == "__main__":
    main()
