"""NumChat : pilote de Snapchat Web (texte seulement) pour la passerelle.

Principe :
- Google Chrome est lancé normalement (pas par Playwright) avec un profil dédié
  et un port de débogage limité à 127.0.0.1. La passerelle s'y attache avec
  Playwright (connect_over_cdp). La page ne voit donc pas les marqueurs
  d'automatisation habituels (navigator.webdriver reste faux).
- L'utilisateur se connecte lui-même dans cette fenêtre la première fois ;
  ce module ne manipule jamais d'identifiants.
- Lecture passive du DOM déjà affiché ; aucune requête réseau fabriquée.
- On ne clique jamais à l'intérieur des messages : les snaps, vocaux et
  vidéos ne sont jamais ouverts, seulement signalés par une étiquette.
- Les seules actions sont : ouvrir la conversation choisie sur la
  calculatrice, et taper/envoyer le texte écrit par l'utilisateur.

Repères utilisés (attributs d'accessibilité et identifiants, pas les classes
CSS générées qui changent à chaque version de Snapchat Web) :
- liste : [role=listitem] [role=button][aria-labelledby^="title-"],
  nom dans #title-<uuid>, statut dans #status-<uuid>, heure dans <time datetime>
- conversation ouverte : URL /web/<uuid>, messages dans ul#cv-<uuid>,
  groupes par expéditeur avec <header> (nom + <time datetime>)
- saisie : [role=textbox][contenteditable=true]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, Error as PlaywrightError

CDP_PORT = 9333
HOME_URL = "https://www.snapchat.com/web"
def _profile_dir() -> Path:
    """Profil de navigateur dédié à NumChat (jamais celui de l'utilisateur)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NumChat" / "chrome-profile"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NumChat" / "chrome-profile"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "numchat" / "chrome-profile"


PROFILE_DIR = _profile_dir()
# Google Chrome d'abord ; sous Windows, Microsoft Edge (toujours installé, et
# lui aussi basé sur Chromium) sert de secours.
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
]

# Noms affichés pour soi-même selon la langue de Snapchat Web.
SELF_NAMES = {"moi", "me", "ich", "yo", "io", "eu"}


@dataclass
class Conversation:
    id: str
    name: str
    status: str
    unread: bool
    time: str | None  # ISO 8601


@dataclass
class Message:
    key: str
    sender: str
    mine: bool
    time: str | None
    kind: str  # "text", "info" (avis système) ou une étiquette : "snap", "photo", "video", "vocal", "autocollant", "media"
    text: str
    viewable: bool = False  # snap non ouvert, photo ou vidéo : peut s'afficher sur demande


# --- JavaScript exécuté dans la page (lecture seule) -----------------------

JS_LIST = r"""
() => {
  const out = [];
  for (const btn of document.querySelectorAll('[role=listitem] [role=button][aria-labelledby^="title-"]')) {
    const tid = btn.getAttribute('aria-labelledby').split(' ')[0];
    const id = tid.slice('title-'.length);
    const title = document.getElementById(tid);
    const status = document.getElementById('status-' + id);
    const statusText = status ? status.textContent.trim() : (btn.querySelector('[class] > span, div > span')?.textContent || '').trim();
    let weight = 400;
    if (status) {
      for (const e of [status, ...status.querySelectorAll('*')]) {
        weight = Math.max(weight, parseInt(getComputedStyle(e).fontWeight) || 400);
      }
    }
    out.push({
      id,
      name: title ? title.textContent.trim() : '',
      status: statusText,
      unread: weight >= 600,
      time: btn.querySelector('time')?.getAttribute('datetime') || null,
    });
  }
  return out;
}
"""

JS_MESSAGES = r"""
(convId) => {
  const ul = document.getElementById('cv-' + convId);
  if (!ul) return null;
  const res = [];
  let n = 0;
  for (const grp of ul.children) {
    const inner = grp.querySelector(':scope > div > ul');
    if (!inner) {
      // Avis système (capture d'écran, « vous utilisez Snapchat pour le web »...),
      // sauf les séparateurs de date (<time>).
      const t = grp.textContent.trim();
      if (t && !grp.querySelector('time')) res.push({n: n++, g: 0, sender: '', time: null, color: '', text: '',
          plain: t, labels: '', info: true, hasImg: false, hasVideo: false, hasAudio: false, hasSvg: false});
      continue;
    }
    let sender = '', time = null, color = '', g = 0;
    for (const li of inner.children) {
      const h = li.querySelector(':scope > header');
      if (h) {
        const nameEl = h.querySelector('span') || h;
        sender = nameEl.textContent.trim();
        time = h.querySelector('time')?.getAttribute('datetime') || time;
        color = getComputedStyle(nameEl).color;
      }
      const body = [...li.children].filter(c => c.tagName !== 'HEADER' && c.tagName !== 'UL');
      const spans = [...li.querySelectorAll('span[dir=auto]')].filter(s => !h || !h.contains(s));
      const text = spans.map(s => s.textContent).join('\n').trim();
      const has = sel => body.some(b => b.querySelector(sel));
      const labels = body.flatMap(b => [...b.querySelectorAll('[aria-label],[title],img[alt]')]
        .map(e => (e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('alt') || '')))
        .join(' ');
      const plain = body.map(b => b.textContent).join(' ').trim();
      res.push({n: n++, g: g++, sender, time, color, text, plain, labels,
                hasImg: has('img'), hasVideo: has('video'), hasAudio: has('audio, canvas'),
                hasSvg: has('svg')});
    }
  }
  return res;
}
"""


# Le bouton « Fermer le Chat » : premier bouton titré du panneau, hors messages.
JS_CLOSE_BUTTON = r"""
(convId) => {
  const ul = document.getElementById('cv-' + convId);
  if (!ul) return null;
  const pick = n => [...n.querySelectorAll('button[title]')].find(b => !ul.contains(b));
  let e = ul;
  while (e && !pick(e)) e = e.parentElement;
  return e ? pick(e) : null;
}
"""


JS_MEDIA_TARGET = r"""
([convId, time, sender, g]) => {
  // Le bloc cliquable d'un message, retrouvé par son groupe (expéditeur +
  // heure) et son rang dans le groupe, comme les clés de read_messages.
  const ul = document.getElementById('cv-' + convId);
  if (!ul) return null;
  for (const grp of ul.children) {
    const inner = grp.querySelector(':scope > div > ul');
    if (!inner) continue;
    let s = '', t = null, i = 0;
    for (const li of inner.children) {
      const h = li.querySelector(':scope > header');
      if (h) { s = (h.querySelector('span') || h).textContent.trim(); t = h.querySelector('time')?.getAttribute('datetime') || t; }
      if (s === sender && String(t) === time && i === g) {
        const body = [...li.children].find(c => c.tagName !== 'HEADER' && c.tagName !== 'UL');
        return body ? (body.querySelector('div') || body) : null;
      }
      i++;
    }
  }
  return null;
}
"""

# Le média affiché par la visionneuse : la plus grande image/vidéo visible,
# hors liste des conversations et hors messages.
JS_VIEWER_MEDIA = r"""
() => {
  let best = null, area = 150 * 150;
  for (const e of document.querySelectorAll('img, video, canvas')) {
    if (e.closest('nav') || e.closest('ul[id^="cv-"]')) continue;
    const r = e.getBoundingClientRect();
    if (r.width * r.height > area && r.bottom > 0 && r.top < innerHeight) { best = e; area = r.width * r.height; }
  }
  return best;
}
"""

# Bouton de fermeture de la visionneuse : le bouton le plus proche de son coin
# en haut à droite.
JS_VIEWER_CLOSE = r"""
(media) => {
  let box = media;
  for (let k = 0; k < 6 && box.parentElement; k++) box = box.parentElement;
  const r = box.getBoundingClientRect();
  let best = null, dist = 1e9;
  for (const b of box.querySelectorAll('button, [role=button]')) {
    const q = b.getBoundingClientRect();
    if (!q.width) continue;
    const d = Math.hypot(r.right - q.right, q.top - r.top);
    if (d < dist) { best = b; dist = d; }
  }
  return best;
}
"""

JS_RENDER_EMOJI = r"""
([list, size]) => list.map(e => {
  // Canvas en mémoire, jamais ajouté à la page.
  const c = document.createElement('canvas'); c.width = c.height = size;
  const g = c.getContext('2d');
  g.font = `${Math.round(size * 0.82)}px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif`;
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(e, size / 2, size / 2 + size * 0.05);
  const d = g.getImageData(0, 0, size, size).data;
  let s = '';
  for (let i = 0; i < d.length; i += 8192) s += String.fromCharCode.apply(null, d.subarray(i, i + 8192));
  return btoa(s);
})
"""


def _classify(raw: dict) -> tuple[str, str]:
    """Renvoie (kind, text) pour un message brut lu dans la page."""
    if raw.get("info"):
        return "info", raw["plain"]
    if raw["text"]:
        return "text", raw["text"]
    hint = (raw["plain"] + " " + raw["labels"]).lower()
    if "snap" in hint:
        # Snap éphémère (photo ou vidéo) : jamais ouvert.
        return "snap", ""
    if any(w in hint for w in ("vocal", "audio", "voice")) or raw["hasAudio"]:
        return "vocal", ""
    if any(w in hint for w in ("vidéo", "video")) or raw["hasVideo"]:
        return "video", ""
    if any(w in hint for w in ("autocollant", "sticker", "bitmoji")):
        return "autocollant", ""
    if any(w in hint for w in ("photo", "image")) or raw["hasImg"]:
        return "photo", ""
    if raw["plain"]:
        # Texte non porté par span[dir=auto] (statut, appel manqué...) : on le garde.
        return "text", raw["plain"]
    return "media", ""


class SnapchatWeb:
    """Accès synchrone à Snapchat Web. À utiliser depuis un seul thread."""

    def __init__(self, port: int = CDP_PORT, profile: Path = PROFILE_DIR) -> None:
        self.port = port
        self.profile = profile
        self._pw = None
        self._browser = None
        self.page: Page | None = None

    # -- navigateur ---------------------------------------------------------

    def _cdp_up(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1):
                return True
        except OSError:
            return False

    def launch_chrome(self) -> None:
        """Lance Chrome (profil dédié) s'il n'est pas déjà lancé."""
        if self._cdp_up():
            return
        exe = (next((c for c in CHROME_CANDIDATES if os.path.exists(c)), None)
               or shutil.which("google-chrome") or shutil.which("chromium"))
        if not exe:
            raise RuntimeError("Google Chrome est introuvable : installe-le pour utiliser NumChat.")
        # Chrome doit survivre à la passerelle : la session Snapchat reste ouverte.
        detach = ({"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | NEW_PROCESS_GROUP
                  if os.name == "nt" else {"start_new_session": True})
        self.profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [exe, f"--user-data-dir={self.profile}", f"--remote-debugging-port={self.port}",
             "--remote-debugging-address=127.0.0.1", "--no-first-run",
             "--no-default-browser-check", HOME_URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
        for _ in range(60):
            if self._cdp_up():
                return
            time.sleep(0.25)
        raise RuntimeError("Chrome ne répond pas sur le port de débogage local.")

    def connect(self) -> None:
        self.launch_chrome()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        ctx = self._browser.contexts[0]
        pages = [p for p in ctx.pages if "snapchat.com/web" in p.url]
        if pages:
            self.page = pages[0]
        else:
            self.page = ctx.new_page()
            self.page.goto(HOME_URL)

    def close(self) -> None:
        # Se détache sans fermer Chrome : la session Snapchat reste ouverte.
        if self._pw:
            self._pw.stop()
        self._pw = self._browser = self.page = None

    # -- état ---------------------------------------------------------------

    def logged_in(self) -> bool:
        return "/web" in self.page.url and self.page.locator('[role=list][aria-label] [role=listitem]').count() > 0

    def current_conversation(self) -> str | None:
        path = self.page.url.split("?")[0].rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        return tail if len(tail) == 36 and tail.count("-") == 4 else None

    # -- lecture ------------------------------------------------------------

    def list_conversations(self) -> list[Conversation]:
        convs = [Conversation(**c) for c in self.page.evaluate(JS_LIST)]
        # Non-lus d'abord, puis du plus récent au plus ancien.
        convs.sort(key=lambda c: (not c.unread, _neg_time(c.time)))
        return convs

    def open_conversation(self, conv_id: str, timeout: float = 8.0) -> None:
        if self.current_conversation() == conv_id:
            return
        btn = self.page.locator(f'[role=button][aria-labelledby^="title-{conv_id}"]')
        btn.click()
        self.page.wait_for_selector(f'ul[id="cv-{conv_id}"]', timeout=timeout * 1000)

    def read_messages(self, conv_id: str) -> list[Message] | None:
        raws = self.page.evaluate(JS_MESSAGES, conv_id)
        if raws is None:
            return None
        out: list[Message] = []
        group_colors: dict[str, str] = {}
        for r in raws:
            kind, text = _classify(r)
            mine = r["sender"].strip().lower() in SELF_NAMES
            if r["color"]:
                group_colors[r["sender"]] = r["color"]
            # Stable even if Snapchat loads older messages above: a message is
            # known by its group (sender + time) and its rank in that group.
            if r.get("info"):
                key = "info|" + r["plain"]
            else:
                key = f'{r["time"]}|{r["sender"]}|{r["g"]}'
            hint = (r["plain"] + " " + r["labels"]).lower()
            viewable = (kind in ("photo", "video")
                        or (kind == "snap" and any(w in hint for w in ("cliquer", "click", "appuie", "tap"))))
            out.append(Message(key=key, sender=r["sender"], mine=mine, time=r["time"], kind=kind,
                               text=text, viewable=viewable))
        return out

    def close_conversation(self) -> None:
        """Ferme le chat ouvert, pour que Snapchat ne marque plus comme lus
        les messages qui arrivent ensuite."""
        conv_id = self.current_conversation()
        if not conv_id:
            return
        button = self.page.evaluate_handle(JS_CLOSE_BUTTON, conv_id).as_element()
        if button is None:
            raise RuntimeError("Bouton de fermeture du chat introuvable.")
        button.click()
        self.page.wait_for_url(lambda url: self.current_conversation() is None, timeout=5000)

    # -- emojis et médias ------------------------------------------------------

    def render_emojis(self, emojis: list[str], size: int) -> list[bytes]:
        import base64

        return [base64.b64decode(b) for b in self.page.evaluate(JS_RENDER_EMOJI, [emojis, size])]

    def open_media(self, conv_id: str, key: str, timeout: float = 6.0):
        """Ouvre le snap ou la photo du message `key` et renvoie l'élément
        affiché (image, vidéo...). Un snap ainsi ouvert est consommé."""
        self.open_conversation(conv_id)
        time_, rest = key.split("|", 1)
        sender, g = rest.rsplit("|", 1)
        target = self.page.evaluate_handle(JS_MEDIA_TARGET, [conv_id, time_, sender, int(g)]).as_element()
        if target is None:
            raise RuntimeError("Message introuvable dans la page.")
        target.click()
        deadline = time.time() + timeout
        while time.time() < deadline:
            media = self.page.evaluate_handle(JS_VIEWER_MEDIA).as_element()
            if media is not None:
                return media
            self.page.wait_for_timeout(150)
        raise RuntimeError("Le snap ne s'est pas affiché.")

    def media_visible(self, media) -> bool:
        try:
            return bool(media.evaluate(
                "e => e.isConnected && e.getBoundingClientRect().width > 100"))
        except PlaywrightError:
            return False

    def capture_media(self, media) -> bytes:
        """Image actuelle du média, en JPEG, en mémoire seulement."""
        return media.screenshot(type="jpeg", quality=85, timeout=3000)

    def close_media(self, media) -> None:
        if not self.media_visible(media):
            return
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        if self.media_visible(media):
            button = media.evaluate_handle(JS_VIEWER_CLOSE).as_element()
            if button is not None:
                button.click()

    # -- envoi --------------------------------------------------------------

    def send(self, conv_id: str, text: str) -> None:
        text = " ".join(text.split("\n")).strip()
        if not text:
            return
        self.open_conversation(conv_id)
        box = self.page.locator('[role=textbox][contenteditable=true]').first
        box.click()
        self.page.keyboard.type(text, delay=60)
        self.page.wait_for_timeout(250)
        self.page.keyboard.press("Enter")
        # Snapchat vide la zone après l'envoi.
        deadline = time.time() + 5
        while time.time() < deadline:
            if not box.inner_text().strip():
                return
            self.page.wait_for_timeout(100)
        raise RuntimeError("Le message n'a pas l'air parti (zone de saisie non vidée).")


def _neg_time(iso: str | None) -> float:
    if not iso:
        return 0.0
    from datetime import datetime
    try:
        return -datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --- Ligne de commande de prototype -----------------------------------------

def _find(snap: SnapchatWeb, name: str) -> Conversation:
    for c in snap.list_conversations():
        if c.name.lower() == name.lower() or c.id == name:
            return c
    raise SystemExit(f"Conversation introuvable : {name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Prototype NumChat : Snapchat Web en ligne de commande")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("read"); r.add_argument("name")
    w = sub.add_parser("watch"); w.add_argument("name")
    s = sub.add_parser("send"); s.add_argument("name"); s.add_argument("text")
    args = ap.parse_args()

    snap = SnapchatWeb()
    snap.connect()
    try:
        if not snap.logged_in():
            print("Pas connecté : connecte-toi dans la fenêtre Chrome NumChat.")
            return 1
        if args.cmd == "list":
            for c in snap.list_conversations():
                print(("● " if c.unread else "  ") + f"{c.name:20} {c.status:30} {c.time or ''}")
            return 0
        conv = _find(snap, args.name)
        snap.open_conversation(conv.id)
        if args.cmd == "send":
            snap.send(conv.id, args.text)
            print("envoyé")
            return 0
        seen: set[str] = set()
        while True:
            for m in snap.read_messages(conv.id) or []:
                if m.key in seen:
                    continue
                seen.add(m.key)
                if m.kind == "info":
                    print(f"   -- {m.text} --")
                    continue
                body = m.text if m.kind == "text" else f"[{m.kind}]"
                side = ">>" if m.mine else "<<"
                print(f"{side} {m.sender}: {body}")
            if args.cmd == "read":
                return 0
            time.sleep(1.0)
    finally:
        snap.close()


if __name__ == "__main__":
    sys.exit(main())
