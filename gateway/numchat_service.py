"""NumChat, côté passerelle : relie l'appli NumChat de la calculatrice à
Snapchat Web (ou à une messagerie de démonstration pour les tests).

Tout ce qui touche au navigateur tourne dans un seul thread (Playwright n'est
pas utilisable depuis plusieurs threads) : les trames de la calculatrice y
arrivent par une file, et ce même thread surveille la page pour pousser les
nouveaux messages.

Protocole : voir firmware/epsilon/apps/numchat/protocol.h (types 0x60-0x72).
"""
from __future__ import annotations

import io
import queue
import struct
import threading
import time
import unicodedata
from dataclasses import dataclass

import numchat_media as media

T_LIST = 0x60
T_CONVERSATION = 0x61
T_LIST_DONE = 0x62
T_OPEN = 0x63
T_MESSAGE = 0x64
T_OPEN_DONE = 0x65
T_SEND = 0x66
T_SEND_RESULT = 0x67
T_CLOSE = 0x68
T_STATUS = 0x69
T_PING = 0x6A
T_PONG = 0x6B
T_EMOJI = 0x6C        # PC -> calc: u8 slot, u8 w, u8 h, RGB565 pixels
T_VIEW = 0x6D         # PC <- calc: u8 media id
T_MEDIA_START = 0x6E  # PC -> calc: u16 w, u16 h
T_MEDIA_ROWS = 0x6F   # PC -> calc: u16 frame, u16 y, u8 rows, u8 last, pixels
T_MEDIA_END = 0x70    # PC -> calc: u8 still viewable, reason text
T_MEDIA_ACK = 0x71    # PC <- calc: u16 frame
T_VIEW_STOP = 0x72    # PC <- calc
CHAT_TYPES = set(range(T_LIST, T_VIEW_STOP + 1))

F_UNREAD = 1
F_MINE, F_INFO, F_MEDIA, F_HISTORY, F_VIEWABLE = 1, 2, 4, 8, 16
MAX_MEDIA_IDS = 250

MAX_CONVERSATIONS = 24  # see ChatController::k_maxConversations
HISTORY = 30
TEXT_BYTES = 200  # see TextInput::k_capacity

MEDIA_LABELS = {
    "snap": "[snap]", "photo": "[photo]", "video": "[video]", "vocal": "[vocal]",
    "autocollant": "[autocollant]", "media": "[piece jointe]",
}


@dataclass
class Conv:
    id: str
    name: str
    status: str
    unread: bool


@dataclass
class Msg:
    key: str
    sender: str
    mine: bool
    kind: str
    text: str
    viewable: bool = False


def _str8(text: str, max_bytes: int, encode) -> bytes:
    data = encode(text, max_bytes)
    return bytes([len(data)]) + data


class DemoBackend:
    """Messagerie factice : deux conversations, dont une qui écrit toute seule
    toutes les 20 s et une qui répond en écho. Sert à tester la calculatrice
    sans toucher à Snapchat."""

    name = "demo"

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.messages = {
            "demo-echo": [Msg("e0", "Echo", False, "text", "Ecris-moi, je repete tout."),
                          Msg("e1", "Echo", False, "info", "Conversation de demonstration")],
            "demo-tick": [Msg("t0", "Horloge", False, "text", "Je t'ecris toutes les 20 s."),
                          Msg("t1", "Horloge", False, "snap", "", True),
                          Msg("t2", "Horloge", False, "text", "Ça marche avec les accents : é è ê à ç ù ! 😎👍🏽🇫🇷"),
                          Msg("t3", "Horloge", False, "video", "", True)],
        }
        self.ticks = 0
        self.read = {"demo-echo": True, "demo-tick": False}
        self.open_id: str | None = None
        self.media: dict | None = None

    def ready(self) -> str | None:
        return None

    def _tick(self) -> None:
        due = int((time.monotonic() - self.started) // 20)
        while self.ticks < due:
            self.ticks += 1
            self.messages["demo-tick"].append(
                Msg(f"tick{self.ticks}", "Horloge", False, "text", f"Tic numero {self.ticks} ({time.strftime('%H:%M:%S')})"))
            if self.open_id != "demo-tick":
                self.read["demo-tick"] = False

    def list_conversations(self) -> list[Conv]:
        self._tick()
        return [Conv(cid, "Horloge" if cid == "demo-tick" else "Echo",
                     "Nouveau chat" if not self.read[cid] else "Lu", not self.read[cid])
                for cid in ("demo-tick", "demo-echo")]

    def open_conversation(self, conv_id: str) -> None:
        self.open_id = conv_id
        self.read[conv_id] = True

    def close_conversation(self) -> None:
        self.open_id = None

    def read_messages(self, conv_id: str) -> list[Msg]:
        self._tick()
        return list(self.messages.get(conv_id, []))

    def send(self, conv_id: str, text: str) -> None:
        msgs = self.messages[conv_id]
        msgs.append(Msg(f"m{len(msgs)}", "Moi", True, "text", text))
        if conv_id == "demo-echo":
            msgs.append(Msg(f"m{len(msgs)}", "Echo", False, "text", "Echo : " + text))

    def render_emojis(self, emojis: list[str]) -> list[bytes | None]:
        return [media.render_emoji_pillow(e) for e in emojis]

    # Médias de démonstration : une photo (8 s) et une vidéo (6 s) dessinées ici.
    def open_media(self, conv_id: str, key: str):
        msg = next(m for m in self.messages[conv_id] if m.key == key)
        if msg.kind == "snap":
            msg.viewable = False  # un snap ne se voit qu'une fois
        self.media = {"video": msg.kind == "video", "start": time.monotonic(),
                      "length": 6.0 if msg.kind == "video" else 8.0}
        return self.media

    def media_is_video(self, handle) -> bool:
        return handle["video"]

    def media_visible(self, handle) -> bool:
        return self.media is handle and time.monotonic() - handle["start"] < handle["length"]

    def capture_media(self, handle) -> bytes:
        from PIL import Image, ImageDraw

        t = time.monotonic() - handle["start"]
        if handle["video"]:
            im = Image.new("RGB", (270, 480), (20, 20, 60))
            d = ImageDraw.Draw(im)
            x = int(135 + 100 * __import__("math").sin(t * 2))
            d.ellipse((x - 40, 200, x + 40, 280), fill=(255, 200, 0))
            d.text((20, 20), f"Video de demo {t:4.1f} s", fill=(255, 255, 255))
        else:
            im = Image.new("RGB", (480, 360))
            d = ImageDraw.Draw(im)
            for y in range(360):
                d.line((0, y, 480, y), fill=(y * 255 // 360, 80, 255 - y * 255 // 360))
            d.text((20, 20), "Snap de demo", fill=(255, 255, 255))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue()

    def close_media(self, handle) -> None:
        self.media = None


class SnapBackend:
    """Snapchat Web, à travers gateway/numchat_snap.py."""

    name = "snapchat"

    def __init__(self) -> None:
        from numchat_snap import SnapchatWeb

        self.snap = SnapchatWeb()
        self.connected = False

    def ready(self) -> str | None:
        """None si tout va bien, sinon un message à afficher sur la calculatrice."""
        try:
            if not self.connected:
                self.snap.connect()
                self.connected = True
            if not self.snap.logged_in():
                return "Connecte Snapchat dans Chrome sur le Mac"
        except Exception as error:  # Chrome fermé, page plantée...
            self.connected = False
            try:
                self.snap.close()
            except Exception:
                pass
            return "Chrome/Snapchat indisponible: " + str(error)[:40]
        return None

    def list_conversations(self) -> list[Conv]:
        return [Conv(c.id, c.name, c.status, c.unread) for c in self.snap.list_conversations()]

    def open_conversation(self, conv_id: str) -> None:
        self.snap.open_conversation(conv_id)

    def close_conversation(self) -> None:
        self.snap.close_conversation()

    def read_messages(self, conv_id: str) -> list[Msg]:
        out = []
        for m in self.snap.read_messages(conv_id) or []:
            out.append(Msg(m.key, m.sender, m.mine, m.kind, m.text, m.viewable))
        return out

    def send(self, conv_id: str, text: str) -> None:
        self.snap.send(conv_id, text)

    def render_emojis(self, emojis: list[str]) -> list[bytes | None]:
        return self.snap.render_emojis(emojis, media.RENDER_SIZE)

    def open_media(self, conv_id: str, key: str):
        return self.snap.open_media(conv_id, key)

    def media_is_video(self, handle) -> bool:
        return handle.evaluate("e => e.tagName === 'VIDEO'")

    def media_visible(self, handle) -> bool:
        return self.snap.media_visible(handle)

    def capture_media(self, handle) -> bytes:
        return self.snap.capture_media(handle)

    def close_media(self, handle) -> None:
        self.snap.close_media(handle)


class ChatService:
    """Fait le lien entre une calculatrice (link) et un backend de messagerie."""

    LIST_INTERVAL = 2.0
    MESSAGES_INTERVAL = 0.7

    def __init__(self, backend_factory, clean_text, truncate_utf8, log) -> None:
        self.backend_factory = backend_factory
        self.backend = None
        self.clean_text = clean_text
        self.truncate_utf8 = truncate_utf8
        self.log = log
        self.inbox: queue.Queue = queue.Queue()
        self.link = None
        self.mode = "idle"  # idle, list, open
        self.open_id: str | None = None
        self.sent_keys: set[str] = set()
        self.last_list: list | None = None
        self.last_preview: dict[str, str] = {}
        self.status = ""
        self.next_poll = 0.0
        self.emojis = media.EmojiTable()
        self.media_keys: list[str] = []
        self.thread = threading.Thread(target=self._run, name="numchat", daemon=True)
        self.thread.start()

    # -- appelé depuis le thread de la session USB ---------------------------

    def attach(self, link) -> None:
        self.inbox.put(("attach", link))

    def detach(self, link) -> None:
        self.inbox.put(("detach", link))

    def handle(self, link, frame_type: int, payload: bytes) -> None:
        if frame_type == T_PING:
            link.send(T_PONG)  # répondu tout de suite, même si le navigateur est occupé
            return
        self.inbox.put(("frame", link, frame_type, payload))

    # -- encodage ------------------------------------------------------------

    def _wire(self, text: str, max_bytes: int) -> bytes:
        return self.truncate_utf8(self.clean_text(text), max_bytes)

    def _send(self, frame_type: int, payload: bytes = b"") -> None:
        if self.link is None:
            return
        try:
            self.link.send(frame_type, payload)
        except OSError:
            self.link = None

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self._send(T_STATUS, self._wire(text, 60))

    def _chat_text(self, text: str, max_bytes: int) -> bytes:
        """Texte d'un message : les emojis deviennent des références (U+E000+n)
        à des images envoyées juste avant dans des trames Emoji."""
        parts = media.split_emoji(text)
        emojis = [p for is_emoji, p in parts if is_emoji]
        slots = {}
        if emojis and self.backend is not None:
            try:
                slots = self.emojis.resolve(
                    emojis, self._render_emojis,
                    lambda slot, pixels: self._send(T_EMOJI, media.emoji_payload(slot, pixels)))
            except Exception as error:
                self.log(f"numchat: emojis: {error}")
        out = "".join(chr(0xE000 + slots[p]) if is_emoji and p in slots else p
                      for is_emoji, p in parts)
        return self.truncate_utf8(self.clean_text(out, keep_private=True), max_bytes)

    def _render_emojis(self, emojis: list[str]) -> list[bytes | None]:
        try:
            return self.backend.render_emojis(emojis)
        except Exception:
            return [media.render_emoji_pillow(e) for e in emojis]

    def _message_payload(self, m: Msg, history: bool) -> bytes:
        flags = (F_MINE if m.mine else 0) | (F_HISTORY if history else 0)
        if m.kind == "info":
            flags |= F_INFO
            # Snapchat writes its notices in capitals: back to sentence case.
            letters = [c for c in m.text if c.isalpha()]
            text = m.text
            if letters and sum(c.isupper() for c in letters) > len(letters) / 2:
                text = m.text.lower()
                text = text[:1].upper() + text[1:]
        elif m.kind == "text":
            body = self._chat_text(m.text, TEXT_BYTES) or self._wire("[emoji]", 20)
            return bytes([flags]) + _str8(m.sender, 22, self._wire) + body
        else:
            flags |= F_MEDIA
            text = MEDIA_LABELS.get(m.kind, "[piece jointe]")
            if m.viewable and len(self.media_keys) < MAX_MEDIA_IDS:
                flags |= F_VIEWABLE
                self.media_keys.append(m.key)
                return (bytes([flags]) + _str8(m.sender, 22, self._wire)
                        + bytes([len(self.media_keys) - 1]) + self._wire(text, TEXT_BYTES))
        return bytes([flags]) + _str8(m.sender, 22, self._wire) + self._wire(text, TEXT_BYTES)

    # -- boucle du thread navigateur -----------------------------------------

    def _run(self) -> None:
        while True:
            timeout = max(0.0, self.next_poll - time.monotonic()) if self.mode != "idle" else None
            try:
                item = self.inbox.get(timeout=timeout)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self._dispatch(item)
                if self.mode != "idle" and time.monotonic() >= self.next_poll:
                    self._poll()
            except Exception as error:
                self.log(f"numchat: erreur: {error}")
                self._set_status("Erreur: " + str(error)[:50])
                self.next_poll = time.monotonic() + 2.0

    def _ensure_backend(self) -> bool:
        if self.backend is None:
            self.backend = self.backend_factory()
            self.log(f"numchat: messagerie {self.backend.name}")
        problem = self.backend.ready()
        self._set_status(problem or "")
        return problem is None

    def _dispatch(self, item) -> None:
        kind = item[0]
        if kind == "attach":
            self.link = item[1]
            self.mode = "idle"
            self.emojis.reset()  # a new calculator session starts with no emoji
            return
        if kind == "detach":
            if self.link is item[1]:
                self.link = None
                self._leave_conversation()
                self.mode = "idle"
            return
        _, link, frame_type, payload = item
        self.link = link
        if frame_type == T_LIST:
            self._leave_conversation()
            self.mode = "list"
            self.last_list = None  # renvoyer la liste même si elle n'a pas changé
            self.status = None  # idem pour le statut
            self.next_poll = 0
        elif frame_type == T_OPEN:
            self._open(payload.decode("utf-8", "ignore"))
        elif frame_type == T_SEND:
            self._send_text(unicodedata.normalize("NFC", payload.decode("utf-8", "ignore")))
        elif frame_type == T_CLOSE:
            self._leave_conversation()
            self.mode = "idle"
        elif frame_type == T_VIEW and payload:
            self._view(payload[0])

    def _leave_conversation(self) -> None:
        if self.open_id is not None and self.backend is not None:
            try:
                self.backend.close_conversation()
            except Exception as error:
                self.log(f"numchat: fermeture du chat: {error}")
        self.open_id = None

    def _poll(self) -> None:
        if not self._ensure_backend():
            self.next_poll = time.monotonic() + 2.0
            return
        if self.mode == "list":
            self._push_list()
            self.next_poll = time.monotonic() + self.LIST_INTERVAL
        elif self.mode == "open":
            self._push_new_messages(history=False)
            self.next_poll = time.monotonic() + self.MESSAGES_INTERVAL

    def _push_list(self) -> None:
        convs = self.backend.list_conversations()[:MAX_CONVERSATIONS]
        rows = []
        for c in convs:
            # Snapchat Web n'affiche pas d'extrait : on montre le statut pour
            # les non-lus, sinon le dernier message vu pendant la session.
            preview = c.status if c.unread else self.last_preview.get(c.id, c.status)
            rows.append((c.id, c.name, preview, c.unread))
        if rows == self.last_list:
            return
        self.last_list = rows
        for index, (cid, name, preview, unread) in enumerate(rows):
            payload = bytes([index, F_UNREAD if unread else 0])
            payload += _str8(cid, 39, lambda t, n: t.encode()[:n])
            payload += _str8(name, 30, self._wire) + _str8(preview, 62, self._wire)
            self._send(T_CONVERSATION, payload)
        self._send(T_LIST_DONE, bytes([len(rows)]))

    def _open(self, conv_id: str) -> None:
        if not self._ensure_backend():
            self._send(T_OPEN_DONE)
            return
        self.log(f"numchat: ouverture de {conv_id}")
        self.backend.open_conversation(conv_id)
        self.open_id = conv_id
        self.mode = "open"
        self.sent_keys = set()
        self.media_keys = []
        # Laisse à la page le temps d'afficher les messages.
        deadline = time.monotonic() + 3
        msgs = self.backend.read_messages(conv_id)
        while not msgs and time.monotonic() < deadline:
            time.sleep(0.3)
            msgs = self.backend.read_messages(conv_id)
        for m in msgs[-HISTORY:]:
            self._send(T_MESSAGE, self._message_payload(m, history=True))
        self.sent_keys = {m.key for m in msgs}
        self._remember_preview(msgs)
        self._send(T_OPEN_DONE)
        self.next_poll = time.monotonic() + self.MESSAGES_INTERVAL

    def _push_new_messages(self, history: bool) -> None:
        if self.open_id is None:
            return
        msgs = self.backend.read_messages(self.open_id)
        for m in msgs:
            if m.key not in self.sent_keys:
                self.sent_keys.add(m.key)
                self._send(T_MESSAGE, self._message_payload(m, history))
        self._remember_preview(msgs)

    def _remember_preview(self, msgs: list[Msg]) -> None:
        for m in reversed(msgs):
            if m.kind == "info":
                continue
            body = m.text if m.kind == "text" else MEDIA_LABELS.get(m.kind, "")
            self.last_preview[self.open_id] = ("Moi: " if m.mine else "") + body
            break

    def _send_text(self, text: str) -> None:
        text = " ".join(text.split())
        if not text or self.open_id is None:
            self._send(T_SEND_RESULT, b"\x00" + self._wire("Aucune conversation ouverte", 40))
            return
        try:
            if not self._ensure_backend():
                raise RuntimeError(self.status)
            self.log(f"numchat: envoi de {len(text)} caracteres")
            self.backend.send(self.open_id, text)
        except Exception as error:
            self.log(f"numchat: echec de l'envoi: {error}")
            self._send(T_SEND_RESULT, b"\x00" + self._wire("Echec: " + str(error), 40))
            return
        self._send(T_SEND_RESULT, b"\x01")
        # Le message envoyé apparaît dans la page : on le pousse tout de suite.
        time.sleep(0.4)
        self._push_new_messages(history=False)

    # -- visionneuse de snaps -------------------------------------------------

    def _viewer_inbox(self, acked: set[int]) -> bool:
        """Lit les trames reçues pendant la visionneuse. Renvoie True s'il faut
        arrêter (Retour sur la calculatrice, ou autre demande en attente)."""
        stop = False
        pending = []
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if item[0] == "frame" and item[2] == T_MEDIA_ACK and len(item[3]) >= 2:
                acked.add(struct.unpack("<H", item[3][:2])[0])
            elif item[0] == "frame" and item[2] == T_VIEW_STOP:
                stop = True
            else:
                pending.append(item)  # traité après la visionneuse
                stop = True
        for item in pending:
            self.inbox.put(item)
        return stop

    def _view(self, media_id: int) -> None:
        def end(reason: str, still_viewable: bool = False) -> None:
            self._send(T_MEDIA_END, bytes([1 if still_viewable else 0]) + self._wire(reason, 60))

        if self.open_id is None or media_id >= len(self.media_keys):
            end("Snap introuvable", False)
            return
        key = self.media_keys[media_id]
        try:
            handle = self.backend.open_media(self.open_id, key)
            video = self.backend.media_is_video(handle)
        except Exception as error:
            self.log(f"numchat: ouverture du snap: {error}")
            end("Impossible d'ouvrir ce snap", True)
            return
        self.log(f"numchat: affichage d'un {'video' if video else 'snap photo'}")
        acked: set[int] = set()
        frame, size, last_sent, last_jpeg = 0, None, 0.0, None
        reason = "Snap termine"
        interval = 0.1 if video else 0.8
        try:
            while True:
                if self._viewer_inbox(acked):
                    reason = ""
                    break
                if not self.backend.media_visible(handle):
                    break
                waiting = frame > 0 and (frame - 1) not in acked
                now = time.monotonic()
                if waiting and now - last_sent < 2.0 or now - last_sent < interval:
                    time.sleep(0.02)
                    continue
                jpeg = self.backend.capture_media(handle)
                if jpeg == last_jpeg:  # photo inchangée : rien à renvoyer
                    last_sent = now
                    continue
                last_jpeg = jpeg
                picture = media.fit_picture(jpeg)
                if picture.size != size:
                    size = picture.size
                    self._send(T_MEDIA_START, struct.pack("<HH", *size))
                for rows in media.picture_rows(frame & 0xFFFF, picture):
                    self._send(T_MEDIA_ROWS, rows)
                frame += 1
                last_sent = now
        except Exception as error:
            self.log(f"numchat: visionneuse: {error}")
            reason = "Snap interrompu"
        finally:
            try:
                self.backend.close_media(handle)
            except Exception as error:
                self.log(f"numchat: fermeture du snap: {error}")
        # Une photo ou vidéo de chat reste visible ; un snap est consommé.
        msgs = {m.key: m for m in self.backend.read_messages(self.open_id)}
        still = bool(msgs.get(key) and msgs[key].viewable)
        end(reason, still)
