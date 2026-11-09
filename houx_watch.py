#!/usr/bin/env python3
"""Surveille la revente des 24h du Mans et prévient dès qu'une place
HOUX / HOUX ANNEXE apparaît (push ntfy, et SMS Free Mobile en option).

Usage :
  python3 houx_watch.py           # tourne en boucle (vérif toutes les ~2 min)
  python3 houx_watch.py --once    # une seule vérification (pour cron)
  python3 houx_watch.py --test    # envoie une notif de test et quitte

Configuration (variables d'environnement) :
  NTFY_TOPIC   nom du sujet ntfy (choisis un nom long et difficile à deviner)
  FREE_USER    identifiant Free Mobile   } optionnel, pour recevoir aussi un SMS
  FREE_PASS    clé API Free Mobile       }
  INTERVAL     secondes entre deux vérifs (défaut 120)
  MAX_PAGES    nombre max de pages de résultats parcourues (défaut 5)
  KEYWORD      expression recherchée (défaut : le mot "houx", sans tenir compte de la casse)
  WATCH_URL    page surveillée (défaut : la liste des parkings en revente)
  STATE_FILE   fichier d'état (défaut : houx_state.json à côté du script)
  NTFY_SERVER  serveur ntfy (défaut https://ntfy.sh)
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path

URL = os.environ.get(
    "WATCH_URL",
    "https://ticketchange.24h-lemans.com/list/resaleProducts/"
    "?lang=fr&topicKey=10228407428276-AA-10229945409158",
)
KEYWORD = re.compile(os.environ.get("KEYWORD", r"\bhoux\b"), re.I)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
INTERVAL = int(os.environ.get("INTERVAL", "120"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
FREE_USER = os.environ.get("FREE_USER")
FREE_PASS = os.environ.get("FREE_PASS")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("houx_state.json")))
FAIL_ALERT = 5  # échecs d'affilée avant de prévenir que la surveillance ne voit plus rien

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class TextExtractor(HTMLParser):
    """Récupère le texte visible de la page, ligne par ligne (sans scripts ni styles)."""
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self.lines, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in self.SKIP

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and not self.skip:
            self.lines.append(text)


def page_url(n):
    parts = urllib.parse.urlsplit(URL)
    query = dict(urllib.parse.parse_qsl(parts.query), page=str(n))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def fetch_lines(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    with opener.open(req, timeout=30) as r:
        if urllib.parse.urlsplit(r.geturl()).netloc != urllib.parse.urlsplit(url).netloc:
            raise RuntimeError(f"redirigé vers {r.geturl()} (file d'attente ?)")
        html = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    parser = TextExtractor()
    parser.feed(html)
    return parser.lines


def scan():
    """Liste des annonces trouvées : (ligne contenant le mot-clé, contexte, url de la page)."""
    hits, previous = [], None
    for n in range(1, MAX_PAGES + 1):
        url = page_url(n)
        lines = fetch_lines(url)
        if lines == previous:  # au-delà de la dernière page, le site renvoie la même chose
            break
        previous = lines
        for i, line in enumerate(lines):
            if KEYWORD.search(line):
                hits.append((line, " · ".join(lines[i:i + 3]), url))
        time.sleep(2)  # on reste discret avec le site
    return hits


def notify(title, message, click, priority=5):
    """Envoie sur tous les canaux configurés. Renvoie True si au moins un a marché."""
    ok = False
    if NTFY_TOPIC:
        body = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": message,
                           "click": click, "priority": priority, "tags": ["car"]}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                NTFY_SERVER, data=body, headers={"Content-Type": "application/json"}), timeout=15)
            ok = True
        except Exception as e:
            log(f"ntfy en échec : {e}")
    if FREE_USER and FREE_PASS:
        q = urllib.parse.urlencode({"user": FREE_USER, "pass": FREE_PASS,
                                    "msg": f"{title}\n{message}\n{click}"})
        try:
            urllib.request.urlopen(f"https://smsapi.free-mobile.fr/sendmsg?{q}", timeout=15)
            ok = True
        except Exception as e:
            log(f"SMS Free en échec : {e}")
    return ok


def check(state):
    try:
        hits = scan()
    except Exception as e:
        state["failures"] = state.get("failures", 0) + 1
        log(f"échec n°{state['failures']} : {e}")
        if state["failures"] == FAIL_ALERT:
            notify("Surveillance HOUX en panne",
                   f"{FAIL_ALERT} échecs d'affilée, je ne vois plus la page : {e}", URL, priority=3)
        return
    if state.get("failures", 0) >= FAIL_ALERT:
        notify("Surveillance HOUX rétablie", "Le site répond de nouveau.", URL, priority=2)
    state["failures"] = 0

    current = Counter(line for line, _, _ in hits)
    new = current - Counter(state.get("seen", {}))
    log(f"{sum(current.values())} annonce(s) HOUX en ligne, {sum(new.values())} nouvelle(s)")
    if new:
        details = [(ctx, url) for line, ctx, url in hits if line in new]
        message = "\n".join(f"• {ctx}" for ctx, _ in details)
        if not notify("Place HOUX en revente !", message, details[0][1]):
            return  # aucune notif partie : on retentera au prochain passage
    state["seen"] = dict(current)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="une seule vérification (pour cron)")
    ap.add_argument("--test", action="store_true", help="envoie une notif de test et quitte")
    args = ap.parse_args()

    if not (NTFY_TOPIC or (FREE_USER and FREE_PASS)):
        sys.exit("Configure au moins NTFY_TOPIC, ou FREE_USER + FREE_PASS.")
    if args.test:
        sys.exit(0 if notify("Test surveillance HOUX", "Si tu lis ça, les notifs marchent.", URL) else 1)

    log(f"Surveillance de {URL} (mot-clé : {KEYWORD.pattern})")
    while True:
        state = load_state()
        check(state)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        if args.once:
            break
        time.sleep(max(30, INTERVAL + random.uniform(-15, 15)))


if __name__ == "__main__":
    main()
