"""End-to-end test of NumChat: a fake calculator talks to the real gateway
(demo messaging, or Snapchat with --snapchat) over a pty, the way the
firmware does: list, open, history, live messages, send with accents, close.

Usage: ../.venv/bin/python3 fake_numchat.py            (demo, no browser)
       ../.venv/bin/python3 fake_numchat.py --snapchat NAME          (read only)
       ../.venv/bin/python3 fake_numchat.py --snapchat NAME "text"   (sends it!)
"""
import os
import pty
import select
import subprocess
import sys
import time
import struct
import tty
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402
import numchat_service as chat  # noqa: E402


class FakeCalculator:
    def __init__(self) -> None:
        self.master, slave = pty.openpty()
        tty.setraw(self.master)
        self.slave_path = os.ttyname(slave)
        self.parser = gw.FrameParser()

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        os.write(self.master, gw.encode_frame(frame_type, payload))

    def frames(self, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                for frame in self.parser.feed(os.read(self.master, 4096)):
                    yield frame

    def wait_for(self, wanted: int, timeout: float, collect=None):
        for frame_type, payload in self.frames(timeout):
            if collect is not None:
                collect.append((frame_type, payload))
            if frame_type == wanted:
                return payload
        raise AssertionError(f"pas de trame 0x{wanted:02x} en {timeout} s")


def str8(payload: bytes, cursor: int):
    size = payload[cursor]
    return payload[cursor + 1:cursor + 1 + size].decode(), cursor + 1 + size


def parse_conversation(p: bytes):
    cid, c = str8(p, 2)
    name, c = str8(p, c)
    preview, c = str8(p, c)
    return p[0], p[1], cid, name, preview


def parse_message(p: bytes):
    sender, c = str8(p, 1)
    media_id = None
    if p[0] & chat.F_VIEWABLE:
        media_id, c = p[c], c + 1
    return p[0], sender, p[c:].decode(), media_id


def show(m):
    flags, sender, text, media_id = m
    text = "".join(f"<emoji {ord(ch) - 0xE000}>" if 0xE000 <= ord(ch) <= 0xF8FF else ch for ch in text)
    if media_id is not None:
        text += f" (visible, media {media_id})"
    who = "moi" if flags & chat.F_MINE else ("info" if flags & chat.F_INFO else sender)
    return f"[{who}] {unicodedata.normalize('NFC', text)}"


def main() -> int:
    snapchat = "--snapchat" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = args[0] if args else "Echo"
    # With Snapchat, nothing is sent unless a text is given explicitly.
    text = args[1] if len(args) > 1 else (None if snapchat else "Ça va ? Été à l'école")

    calc = FakeCalculator()
    gateway = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"),
         "--port", calc.slave_path, "--numchat", "snapchat" if snapchat else "demo"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        calc.send(gw.T_HELLO)
        calc.wait_for(gw.T_HELLO_ACK, 10)
        print("1. connexion: ok")

        calc.send(chat.T_PING)
        calc.wait_for(chat.T_PONG, 3)
        print("2. ping/pong: ok")

        calc.send(chat.T_LIST)
        frames = []
        calc.wait_for(chat.T_LIST_DONE, 30, frames)
        convs = [parse_conversation(p) for t, p in frames if t == chat.T_CONVERSATION]
        assert convs, "liste vide"
        assert [c[0] for c in convs] == list(range(len(convs))), "index non consecutifs"
        print("3. liste:", ", ".join(f"{c[3]}{' (non lu)' if c[1] & 1 else ''} - {c[4]}" for c in convs))
        conv = next(c for c in convs if c[3].lower() == target.lower())

        calc.send(chat.T_OPEN, conv[2].encode())
        frames = []
        calc.wait_for(chat.T_OPEN_DONE, 30, frames)
        history = [parse_message(p) for t, p in frames if t == chat.T_MESSAGE]
        assert all(m[0] & chat.F_HISTORY for m in history)
        print(f"4. historique de {conv[3]} ({len(history)} messages):")
        for m in history:
            print("     " + show(m))
        # Accents arrive decomposed, never precomposed (the font has no é).
        for _, _, body, _ in history:
            assert body == unicodedata.normalize("NFD", body), body

        if text is None:
            print("5. envoi: saute (aucun texte donne)")
            calc.send(chat.T_CLOSE)
            time.sleep(1.5)
            print("OK : lecture Snapchat de bout en bout")
            return 0
        wire = unicodedata.normalize("NFD", text).encode()
        calc.send(chat.T_SEND, wire)
        frames = []
        result = calc.wait_for(chat.T_SEND_RESULT, 30, frames)
        assert result[:1] == b"\x01", f"envoi refuse: {result[1:].decode()}"
        live = [parse_message(p) for t, p in frames if t == chat.T_MESSAGE]
        for t, p in calc.frames(3):
            if t == chat.T_MESSAGE:
                live.append(parse_message(p))
        print("5. envoi: ok, messages recus ensuite:")
        for m in live:
            print("     " + show(m))
        mine = [m for m in live if m[0] & chat.F_MINE]
        assert mine and unicodedata.normalize("NFC", mine[-1][2]) == text, "message envoye non relu"

        if not snapchat:
            print("6. attente d'un message pousse par l'horloge (jusqu'a 25 s)...")
            calc.send(chat.T_CLOSE)
            calc.send(chat.T_LIST)
            frames = []
            calc.wait_for(chat.T_LIST_DONE, 10, frames)
            tick = next(parse_conversation(p) for t, p in frames if t == chat.T_CONVERSATION
                        and parse_conversation(p)[3] == "Horloge")
            calc.send(chat.T_OPEN, tick[2].encode())
            frames = []
            calc.wait_for(chat.T_OPEN_DONE, 10, frames)
            emojis = {p[0] for t, p in frames if t == chat.T_EMOJI}
            msgs = [parse_message(p) for t, p in frames if t == chat.T_MESSAGE]
            used = {ord(ch) - 0xE000 for m in msgs for ch in m[2] if 0xE000 <= ord(ch) <= 0xF8FF}
            assert used and used <= emojis, (used, emojis)
            assert all(len(p) == 3 + 14 * 14 * 2 for t, p in frames if t == chat.T_EMOJI)
            print(f"     emojis: {len(emojis)} images 14x14 recues avant leurs messages")
            for m in msgs:
                print("     " + show(m))
            media = [m for m in msgs if m[3] is not None]
            assert len(media) == 2, media

            live_seen = []
            for m in media:
                calc.send(chat.T_VIEW, bytes([m[3]]))
                start = calc.wait_for(chat.T_MEDIA_START, 10)
                w, h = struct.unpack("<HH", start)
                shown, rows_seen = 0, 0
                deadline = time.monotonic() + 3
                end = None
                for t, p in calc.frames(12):
                    if t == chat.T_MEDIA_ROWS:
                        frame, y, rows, last = struct.unpack("<HHBB", p[:6])
                        assert len(p) == 6 + rows * w * 2
                        rows_seen += rows
                        if last:
                            shown += 1
                            calc.send(chat.T_MEDIA_ACK, struct.pack("<H", frame))
                    elif t == chat.T_MESSAGE:
                        live_seen.append(p)  # arrived while the snap was shown
                    elif t == chat.T_MEDIA_END:
                        end = p
                        break
                    if time.monotonic() > deadline and end is None:
                        calc.send(chat.T_VIEW_STOP)
                        deadline = float("inf")
                assert end is not None, "pas de fin de visionneuse"
                print(f"     {m[2]}: {w}x{h}, {shown} image(s) affichee(s), fin: "
                      f"{end[1:].decode() or '(Retour)'}, encore visible: {bool(end[0])}")
                assert shown >= 1 and w <= 320 and h <= 240

            payload = live_seen[0] if live_seen else calc.wait_for(chat.T_MESSAGE, 25)
            m = parse_message(payload)
            assert not m[0] & chat.F_HISTORY
            print("     recu en direct: " + show(m))

        calc.send(chat.T_CLOSE)
        time.sleep(1.5)
        print("OK : tout le protocole NumChat fonctionne")
        return 0
    finally:
        gateway.terminate()
        out = gateway.communicate(timeout=5)[0]
        if "--verbose" in sys.argv or "erreur" in out or "echec" in out:
            print("--- passerelle ---\n" + out)


if __name__ == "__main__":
    sys.exit(main())
