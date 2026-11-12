"""NumScreen: the computer screen on the calculator, and the calculator keys
on the computer.

The calculator is a thin client (apps/numscreen in the patch): it shows a
320x200 picture coded like NumTube's video, and sends every key press and
release. Everything else lives here: shift and alpha, the mouse, which key
does what, the status strip at the bottom of the calculator screen.

Frames (keep in sync with apps/numscreen/protocol.h):
  0x80 Start   calc -> PC
  0x81 Frame   PC -> calc  u16 frame, u16 start tile, u8 flags, tile ops
  0x82 Ack     calc -> PC  u16 frame
  0x83 Key     calc -> PC  u8 key, u8 down
  0x84 Status  PC -> calc  UTF-8, two lines at most
  0x85 Stop    calc -> PC
"""
from __future__ import annotations

import io
import struct
import sys
import threading
import time

import numpy as np

import numscreen_input

T_START, T_FRAME, T_ACK, T_KEY, T_STATUS, T_STOP = 0x80, 0x81, 0x82, 0x83, 0x84, 0x85
T_FIRST, T_LAST = 0x80, 0x8F

PICTURE_W, PICTURE_H = 320, 200
ROWS = PICTURE_H // 4
CAMERA_ROWS = 60   # NumCam: the whole screen, 320x240
FPS = 15
END_OF_FRAME = 1

# ---------------------------------------------------------------------------
# Calculator keys (Ion::Keyboard::Key = row * 6 + column)
# ---------------------------------------------------------------------------
LEFT, UP, DOWN, RIGHT, OK, BACK = 0, 1, 2, 3, 4, 5
HOME, ONOFF = 6, 8
SHIFT, ALPHA, XNT, VAR, TOOLBOX, BACKSPACE = 12, 13, 14, 15, 16, 17
EXP, LN, LOG, IMAGINARY, COMMA, POWER = 18, 19, 20, 21, 22, 23
SINE, COSINE, TANGENT, PI, SQRT, SQUARE = 24, 25, 26, 27, 28, 29
SEVEN, EIGHT, NINE, LPAREN, RPAREN = 30, 31, 32, 33, 34
FOUR, FIVE, SIX, TIMES, DIVIDE = 36, 37, 38, 39, 40
ONE, TWO, THREE, PLUS, MINUS = 42, 43, 44, 45, 46
ZERO, DOT, EE, ANS, EXE = 48, 49, 50, 51, 52

ARROWS = {LEFT: (-1, 0), RIGHT: (1, 0), UP: (0, -1), DOWN: (0, 1)}
ARROW_KEYS = {LEFT: "left", RIGHT: "right", UP: "up", DOWN: "down"}

# Alpha layer: the letters printed on the calculator keys.
ALPHA_CHARS = {
    EXP: "a", LN: "b", LOG: "c", IMAGINARY: "d", COMMA: "e", POWER: "f",
    SINE: "g", COSINE: "h", TANGENT: "i", PI: "j", SQRT: "k", SQUARE: "l",
    SEVEN: "m", EIGHT: "n", NINE: "o", LPAREN: "p", RPAREN: "q",
    FOUR: "r", FIVE: "s", SIX: "t", TIMES: "u", DIVIDE: "v",
    ONE: "w", TWO: "x", THREE: "y", PLUS: "z", MINUS: " ",
    ZERO: "?", DOT: "!", VAR: ";", TOOLBOX: '"',
}
# Plain layer: digits and the single-character symbols. Keys that type a
# math function on the calculator (sin, ln...) do something useful instead.
PLAIN = {
    SEVEN: "7", EIGHT: "8", NINE: "9", LPAREN: "(", RPAREN: ")",
    FOUR: "4", FIVE: "5", SIX: "6", TIMES: "*", DIVIDE: "/",
    ONE: "1", TWO: "2", THREE: "3", PLUS: "+", MINUS: "-",
    ZERO: "0", DOT: ".", COMMA: ",", POWER: "^", PI: "π", SQRT: "√", SQUARE: "²",
    EXP: ("hold", "shift"), LN: ("hold", "ctrl"), LOG: ("hold", "alt"),
}
# Yellow symbols (shift on the calculator), here typed with alpha in the
# digits mode.
SHIFTED = {
    EXP: "[", LN: "]", LOG: "{", IMAGINARY: "}", COMMA: "_", POWER: "→",
    PI: "=", SQRT: "<", SQUARE: ">", BACKSPACE: ("key", "delete"),
}
# Same in every mode.
ALWAYS = {
    BACK: ("key", "esc"), EXE: ("key", "enter"), BACKSPACE: ("key", "backspace"),
    EE: "'", ANS: ("key", "space"), XNT: ("mode",), VAR: ("zqsd",),
    TOOLBOX: ("button", "right"),
}
# var: Z Q S D on the 8 4 5 6 block (AZERTY game controls).
ZQSD = {EIGHT: "z", FOUR: "q", FIVE: "s", SIX: "d"}

HELP = {
    "souris": "fleches: souris  OK: clic  outils: clic droit",
    "jeu": "fleches : deplacement  OK : saut",
}


class KeyMapper:
    """Turns calculator key presses into mouse and keyboard actions.

    Letters by default (alpha locked); shift switches between letters and
    digits; alpha gives one capital letter (twice: caps lock), or a yellow
    symbol in the digits mode; var puts Z Q S D on 8 4 5 6 for games."""

    def __init__(self, out: numscreen_input.Input, status_changed) -> None:
        self.out = out
        self.status_changed = status_changed
        self.mode = "souris"
        self.letters = True
        self.alpha = "off"  # off, once, lock: capital letters
        self.zqsd = False
        self.held: dict[int, tuple] = {}
        self.arrows: set[int] = set()
        self.arrow_since = 0.0
        self.scroll_dir = 0
        self.lock = threading.Lock()

    # -- status shown on the calculator
    def status_line(self) -> str:
        parts = ["MODE SOURIS" if self.mode == "souris" else "MODE JEU",
                 "lettres" if self.letters else "chiffres"]
        if self.alpha == "lock":
            parts.append("MAJ verrouille")
        elif self.alpha == "once":
            parts.append("MAJ" if self.letters else "symbole")
        if self.zqsd:
            parts.append("ZQSD")
        return "  ".join(parts)

    # -- resolution
    def _resolve(self, key: int):
        if key in ARROWS:
            if self.mode == "jeu":
                return ("key", ARROW_KEYS[key])
            if self.alpha != "off" and key in (UP, DOWN):
                return ("scroll", -1 if key == UP else 1)
            return ("arrow", key)
        if key == OK:
            return ("key", "space") if self.mode == "jeu" else ("button", "left")
        if self.zqsd and key in ZQSD:
            return ZQSD[key]
        if self.letters and key in ALPHA_CHARS and key not in (VAR, TOOLBOX):
            ch = ALPHA_CHARS[key]
            if ch == " ":
                return ("key", "space")
            return ch.upper() if (self.alpha != "off" and ch.isalpha()) else ch
        if not self.letters and self.alpha != "off" and key in SHIFTED:
            return SHIFTED[key]
        if key in ALWAYS:
            return ALWAYS[key]
        return PLAIN.get(key)

    def _consume_alpha(self) -> None:
        if self.alpha == "once":
            self.alpha = "off"
            self.status_changed()

    def press(self, key: int) -> None:
        with self.lock:
            if key == SHIFT:
                self.letters = not self.letters
                self.alpha = "off"
                self.status_changed()
                return
            if key == ALPHA:
                self.alpha = {"off": "once", "once": "lock", "lock": "off"}[self.alpha]
                self.status_changed()
                return
            action = self._resolve(key)
            if action is None:
                return
            self._do(key, action)
            if not (isinstance(action, tuple) and action[0] in ("arrow", "mode", "zqsd")):
                self._consume_alpha()

    def _do(self, key: int, action) -> None:
        if isinstance(action, str):
            # A character: a real key (held while the calculator key is) when
            # the keyboard has one, typed text otherwise.
            self.out.key(action, True)
            self.held[key] = ("char", action)
            return
        kind = action[0]
        if kind in ("key", "hold"):
            self.out.key(action[1], True)
            self.held[key] = ("key", action[1])
        elif kind == "button":
            self.out.button(action[1], True)
            self.held[key] = ("button", action[1])
        elif kind == "arrow":
            if not self.arrows:
                self.arrow_since = time.monotonic()
            self.arrows.add(key)
            dx, dy = ARROWS[key]
            step = 2 * self.out.screen_size()[0] / PICTURE_W  # two calculator pixels
            self.out.move(dx * step, dy * step)
            self.held[key] = ("arrow",)
        elif kind == "scroll":
            self.out.scroll(-action[1] * 3)
            self.scroll_dir = action[1]
            self.held[key] = ("scroll",)
        elif kind == "mode":
            self.release_all()
            self.mode = "jeu" if self.mode == "souris" else "souris"
            self.status_changed()
        elif kind == "zqsd":
            self.release_all()
            self.zqsd = not self.zqsd
            self.status_changed()

    def release(self, key: int) -> None:
        with self.lock:
            held = self.held.pop(key, None)
            if held is None:
                return
            if held[0] in ("char", "key"):
                self.out.key(held[1], False)
            elif held[0] == "button":
                self.out.button(held[1], False)
            elif held[0] == "arrow":
                self.arrows.discard(key)
            elif held[0] == "scroll":
                self.scroll_dir = 0

    def release_all(self) -> None:
        for key in list(self.held):
            held = self.held.pop(key)
            if held[0] in ("char", "key"):
                self.out.key(held[1], False)
            elif held[0] == "button":
                self.out.button(held[1], False)
        self.arrows.clear()
        self.scroll_dir = 0

    def tick(self, dt: float) -> None:
        """Mouse motion while arrows are held: slow at first for precision,
        then faster and faster."""
        with self.lock:
            if self.scroll_dir:
                self.out.scroll(-self.scroll_dir)
            if not self.arrows:
                return
            held_for = time.monotonic() - self.arrow_since
            if held_for < 0.15:
                return
            width = self.out.screen_size()[0]
            # screen widths per second: 0.2 at first, up to 1.2 after 1 s
            speed = width * min(0.2 + 1.0 * (held_for - 0.15), 1.2)
            dx = sum(ARROWS[k][0] for k in self.arrows)
            dy = sum(ARROWS[k][1] for k in self.arrows)
            if dx or dy:
                self.out.move(dx * speed * dt, dy * speed * dt)


class CameraKeys:
    """NumCam's keys: OK a photo, EXE a video (start, stop), shift the other camera, var turns the
    picture. The phone does what is asked (numcam_service.py)."""

    def __init__(self, phone, status_changed) -> None:
        self.phone, self.status_changed = phone, status_changed
        self.mode = "camera"
        self.recording = 0.0      # when the video started (0: not filming)
        self.notice, self.notice_until = "", 0.0
        self.shown_second = -1
        self.connected_at = 0.0
        self.was_connected = False
        self.lock = threading.Lock()
        phone.on_saved = self._saved

    def _saved(self, kind: str, path: str) -> None:
        self.say(("Photo" if kind == "photo" else "Video") + " enregistree : Images/NumCam")

    def say(self, text: str, seconds: float = 3) -> None:
        self.notice, self.notice_until = text, time.monotonic() + seconds
        self.status_changed()

    def status_line(self) -> str:
        if not self.phone.connected:
            return "Sur le telephone, ouvre :"
        if self.recording:
            s = int(time.monotonic() - self.recording)
            return f"REC {s // 60:02d}:{s % 60:02d}    EXE : arreter"
        return "OK : photo   EXE : video   shift : camera"

    def overlay(self, picture: np.ndarray) -> np.ndarray:
        """The camera takes the whole screen: what there is to say is drawn in the picture, on a dark
        band at the bottom (the address to open, until the phone is there; the video's time; a photo
        taken...), or not at all."""
        from PIL import Image, ImageDraw, ImageFont

        if not self.phone.connected:
            lines = ["NumCam", "Sur le telephone, ouvre :", self.phone.url.replace("https://", ""),
                     "(QR code sur la page de NumSocial)"]
        else:
            lines = []
            if self.recording:
                s = int(time.monotonic() - self.recording)
                lines.append(f"REC {s // 60:02d}:{s % 60:02d}")
            if self.notice and time.monotonic() < self.notice_until:
                lines.append(self.notice)
            elif time.monotonic() - self.connected_at < 5:
                lines.append("OK photo  EXE video  shift camera  var pivoter")
        if not lines:
            return picture
        image = Image.fromarray(picture)
        draw = ImageDraw.Draw(image, "RGBA")
        try:
            font = ImageFont.load_default(size=13)
        except TypeError:
            font = ImageFont.load_default()
        h = 16 * len(lines) + 6
        top = image.height - h if self.phone.connected else (image.height - h) // 2
        draw.rectangle([0, top, image.width, top + h], fill=(0, 0, 0, 150))
        for i, line in enumerate(lines):
            w = draw.textlength(line, font=font)
            color = (255, 80, 70) if line.startswith("REC") else (255, 255, 255)
            draw.text(((image.width - w) / 2, top + 3 + 16 * i), line, font=font, fill=color)
        return np.asarray(image)

    def second_line(self) -> str:
        if not self.phone.connected:
            return self.phone.url.replace("https://", "")
        if self.notice and time.monotonic() < self.notice_until:
            return self.notice
        return "var : pivoter   maison : quitter"

    def press(self, key: int) -> None:
        with self.lock:
            if not self.phone.connected:
                return
            if key == OK:
                self.phone.command(do="photo")
                self.say("Photo...")
            elif key == EXE:
                if self.recording:
                    self.phone.command(do="rec_stop")
                    self.recording = 0.0
                    self.say("Envoi de la video...", 10)
                else:
                    self.phone.command(do="rec_start")
                    self.recording = time.monotonic()
                    self.status_changed()
            elif key == SHIFT:
                if self.recording:
                    self.phone.command(do="rec_stop")
                    self.recording = 0.0
                self.phone.flip()
                self.say("Camera " + ("avant" if self.phone.facing == "user" else "arriere"))
            elif key == VAR:
                self.phone.turn()
                self.say(f"Image pivotee de {self.phone.rotate} degres")

    def release(self, key: int) -> None:
        pass

    def release_all(self) -> None:
        if self.recording:
            self.phone.command(do="rec_stop")
            self.recording = 0.0

    def tick(self, dt: float) -> None:
        # (the status follows the phone coming and going, the video's seconds, notices ending)
        if self.phone.connected and not self.was_connected:
            self.connected_at = time.monotonic()   # (the keys shown a few seconds)
        self.was_connected = self.phone.connected
        second = int(time.monotonic())
        if second != self.shown_second:
            self.shown_second = second
            self.status_changed()


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------
def _cursor_sprite() -> tuple[np.ndarray, np.ndarray]:
    """Arrow pointer, 9x13: returns (mask, colour) arrays."""
    shape = [
        "X........",
        "XX.......",
        "XOX......",
        "XOOX.....",
        "XOOOX....",
        "XOOOOX...",
        "XOOOOOX..",
        "XOOOOOOX.",
        "XOOOOXXXX",
        "XOXOOX...",
        "XX.XOOX..",
        "X...XOX..",
        ".....X...",
    ]
    mask = np.array([[c != "." for c in row] for row in shape])
    color = np.zeros(mask.shape + (3,), dtype=np.uint8)
    for y, row in enumerate(shape):
        for x, c in enumerate(row):
            color[y, x] = (255, 255, 255) if c == "O" else (0, 0, 0)
    return mask, color


class Capture:
    def grab(self) -> tuple[np.ndarray, tuple[float, float]]:
        """Returns (RGB picture of the whole screen at any size, size of the
        screen in the coordinates of the mouse)."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class ScreenCapture(Capture):
    def __init__(self, out: numscreen_input.Input) -> None:
        import mss

        self.out = out
        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.monitor = self.sct.monitors[1]

    def grab(self):
        shot = self.sct.grab(self.monitor)
        rgb = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)[..., 2::-1]
        return rgb, self.out.screen_size()

    def close(self) -> None:
        try:
            self.sct.close()
        except Exception:
            pass


class ThreadedCapture(Capture):
    """Grabs in its own thread, so that capturing the next picture overlaps
    with scaling and coding the current one."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.latest = None
        self.error: Exception | None = None
        self.ready = threading.Event()
        self.wanted = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            capture = self.factory()  # mss objects belong to one thread
        except Exception as error:
            self.error = error
            self.ready.set()
            return
        try:
            while not self.stopped.is_set():
                if not self.wanted.wait(0.2):
                    continue
                self.wanted.clear()
                try:
                    self.latest = capture.grab()
                except Exception as error:
                    self.error = error
                self.ready.set()
        finally:
            capture.close()

    def grab(self):
        if self.latest is None:
            self.wanted.set()
        self.ready.wait(5)
        self.ready.clear()
        if self.latest is None:
            raise self.error or RuntimeError("pas d'image")
        picture = self.latest
        self.wanted.set()  # start on the next one right away
        return picture

    def close(self) -> None:
        self.stopped.set()


class PhoneCapture(Capture):
    """NumCam: the latest picture the phone sent (black until it is there)."""

    def __init__(self, phone) -> None:
        self.phone = phone

    def grab(self):
        from PIL import Image

        frame = self.phone.frame if self.phone.connected else None
        if not frame:
            return np.zeros((PICTURE_H, PICTURE_W, 3), dtype=np.uint8), (0, 0)
        image = Image.open(io.BytesIO(frame)).convert("RGB")
        return np.asarray(image), (0, 0)


class PatternCapture(Capture):
    """Moving test picture (tests, demo): no real screen involved."""

    def __init__(self) -> None:
        self.start = time.monotonic()

    def grab(self):
        t = time.monotonic() - self.start
        h, w = 450, 720
        y, x = np.mgrid[0:h, 0:w]
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[..., 0] = (x * 255 // w)
        rgb[..., 1] = (y * 255 // h)
        rgb[..., 2] = 128
        bar = int((t * 120) % w)
        rgb[:, bar:bar + 20] = (255, 255, 255)
        return rgb, (1440, 900)


def fit_picture(rgb: np.ndarray, height: int = PICTURE_H, cover: bool = False
                ) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Scales the screen into 320 x height: all of it with black bars, or (cover) filling it all, the
    edges cut. Returns the picture and the box (x, y, w, h) the source takes in it."""
    from PIL import Image

    h, w = rgb.shape[:2]
    scale = (max if cover else min)(PICTURE_W / w, height / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    image = np.asarray(Image.fromarray(np.ascontiguousarray(rgb)).resize((nw, nh), Image.BOX))
    out = np.zeros((height, PICTURE_W, 3), dtype=np.uint8)
    x0, y0 = (PICTURE_W - nw) // 2, (height - nh) // 2
    sx, sy = max(0, -x0), max(0, -y0)
    dx, dy = max(0, x0), max(0, y0)
    cw, ch = min(nw - sx, PICTURE_W - dx), min(nh - sy, height - dy)
    out[dy:dy + ch, dx:dx + cw] = image[sy:sy + ch, sx:sx + cw]
    return out, (x0, y0, nw, nh)


class Streamer(threading.Thread):
    """Captures, encodes and sends the screen, at most 2 frames ahead of the
    calculator."""

    def __init__(self, link, encoder, capture: Capture, out: numscreen_input.Input,
                 mapper: KeyMapper, status, log, refresh=None, height: int = PICTURE_H, cover: bool = False,
                 overlay=None) -> None:
        super().__init__(daemon=True)
        self.link = link
        self.encoder = encoder
        self.capture_factory = capture
        self.out = out
        self.mapper = mapper
        self.status = status
        self.log = log
        self.refresh = refresh
        self.height, self.cover, self.overlay = height, cover, overlay   # (NumCam: 240, filled, notices drawn in)
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.pending: dict[int, tuple[float, int]] = {}
        self.frame = 0
        self.rate = 250_000.0  # bytes/s, measured from the acks
        self.sprite = _cursor_sprite()
        self.frames_sent = 0

    def stop(self) -> None:
        self.stopped.set()

    def on_ack(self, frame_id: int) -> None:
        with self.lock:
            if frame_id not in self.pending:
                return
            started, size = self.pending.pop(frame_id)
            for stale in [f for f in self.pending if ((frame_id - f) & 0xFFFF) < 0x8000]:
                self.pending.pop(stale)
            elapsed = time.monotonic() - started
            if size >= 6_000 and elapsed > 0:
                measured = min(max(size / elapsed, 30_000), 1_500_000)
                self.rate = 0.7 * self.rate + 0.3 * measured

    def _in_flight(self) -> int:
        now = time.monotonic()
        with self.lock:
            # A lost ack must not stall the stream forever.
            for f in [f for f, (t, _) in self.pending.items() if now - t > 1.5]:
                self.pending.pop(f)
            return len(self.pending)

    def _draw_cursor(self, picture: np.ndarray, box, screen) -> None:
        x0, y0, w, h = box
        cx, cy = self.out.cursor()
        px = int(x0 + cx * w / max(screen[0], 1))
        py = int(y0 + cy * h / max(screen[1], 1))
        mask, color = self.sprite
        sh, sw = mask.shape
        if px >= PICTURE_W or py >= PICTURE_H or px < 0 or py < 0:
            return
        ph, pw = min(sh, PICTURE_H - py), min(sw, PICTURE_W - px)
        region = picture[py:py + ph, px:px + pw]
        m = mask[:ph, :pw]
        region[m] = color[:ph, :pw][m]

    def _send(self, picture: np.ndarray) -> None:
        budget = max(1_500, min(int(self.rate / FPS), 40_000))
        chunks = self.encoder.encode(picture, budget) or [(0, b"")]
        self.frame = (self.frame + 1) & 0xFFFF
        payloads = [struct.pack("<HHB", self.frame, start, END_OF_FRAME if i == len(chunks) - 1 else 0) + ops
                    for i, (start, ops) in enumerate(chunks)]
        with self.lock:
            self.pending[self.frame] = (time.monotonic(), sum(len(p) + 10 for p in payloads))
        for payload in payloads:
            self.link.send(T_FRAME, payload)
        self.frames_sent += 1

    def run(self) -> None:
        capture = None
        try:
            capture = self.capture_factory()
        except Exception as error:
            self.log(f"capture d'ecran impossible: {error}")
            self.status(None, "Capture d'ecran impossible sur ce PC")
        black = np.zeros((self.height, PICTURE_W, 3), dtype=np.uint8)
        last_tick = time.monotonic()
        next_frame = 0.0
        next_refresh = time.monotonic() + 3
        next_stats, sent_before = time.monotonic() + 5, 0
        phone = getattr(self.mapper, "phone", None)
        phone_before = phone.frames_in if phone else 0
        try:
            while not self.stopped.is_set():
                now = time.monotonic()
                if phone is not None and now > next_stats:
                    # (NumCam: who is slow, the phone or the calculator)
                    got = max(phone.frames_in - phone_before, 1)
                    self.log(f"numcam: telephone {(phone.frames_in - phone_before) / 5:.1f} img/s "
                             f"(compression {phone.enc_ms // got} ms, aller-retour {phone.rtt_ms // got} ms), "
                             f"calculatrice {(self.frames_sent - sent_before) / 5:.1f} img/s")
                    phone.enc_ms = phone.rtt_ms = 0
                    next_stats, sent_before, phone_before = now + 5, self.frames_sent, phone.frames_in
                if self.refresh is not None and now > next_refresh:
                    next_refresh = now + 3
                    self.refresh()
                self.mapper.tick(now - last_tick)
                last_tick = now
                if now < next_frame or self._in_flight() >= 2:
                    time.sleep(0.01)
                    continue
                next_frame = now + 1.0 / FPS
                picture = black
                if capture is not None:
                    try:
                        rgb, screen = capture.grab()
                        picture, box = fit_picture(rgb, self.height, self.cover)
                        if screen[0]:   # (the computer's screen: its cursor; the camera has none)
                            self._draw_cursor(picture, box, screen)
                    except Exception as error:
                        self.log(f"capture: {error}")
                        picture = black
                if self.overlay is not None:
                    picture = self.overlay(picture.copy())
                self._send(picture)
        except OSError:
            pass  # calculator gone
        finally:
            if capture is not None:
                capture.close()


# ---------------------------------------------------------------------------
# Service, one per gateway
# ---------------------------------------------------------------------------
class ScreenService:
    def __init__(self, encoder_factory, clean_text, truncate_utf8, log, kind: str = "real") -> None:
        self.encoder_factory = encoder_factory
        self.clean_text = clean_text
        self.truncate_utf8 = truncate_utf8
        self.log = log
        self.kind = kind  # "real", or "demo": test picture, nothing injected
        self.out = numscreen_input.create("dry" if kind == "demo" else "real", log)
        self.link = None
        self.streamer: Streamer | None = None
        self.mapper: KeyMapper | None = None
        self.notice = ""
        self.last_status = None
        self.asked_permissions = False

    # -- status strip
    def _status(self, _unused=None, notice: str | None = None) -> None:
        if notice is not None:
            self.notice = notice
        if self.link is None or self.mapper is None:
            return
        second = (self.mapper.second_line() if hasattr(self.mapper, "second_line")
                  else self.notice or HELP[self.mapper.mode])
        text = self.mapper.status_line() + "\n" + second
        if text == self.last_status:
            return
        self.last_status = text
        try:
            lines = [self.clean_text(line) for line in text.split("\n")]
            self.link.send(T_STATUS, self.truncate_utf8("\n".join(lines), 90))
        except OSError:
            pass

    def refresh_permissions(self) -> None:
        """Called every few seconds: the notice goes away once the user has
        given the permissions."""
        if self.kind != "real" or not self.notice.startswith(("Reglages Mac", "Autorisation")):
            return
        notice = self._permission_notice()
        if notice != self.notice:
            self._status(None, notice)

    def _permission_notice(self) -> str:
        """Short enough for one line of the calculator strip (45 columns)."""
        capture, events = self.out.capture_allowed(), self.out.input_allowed()
        if capture and events:
            return ""
        if sys.platform != "darwin":
            return "Autorisation refusee par le systeme"
        if not capture and not events:
            return "Reglages Mac : Ecran + Accessibilite"
        if not capture:
            return "Reglages Mac : Enregistrement de l'ecran"
        return "Reglages Mac : Accessibilite > NumSocial"

    # -- frames from the calculator
    def handle(self, link, frame_type: int, payload: bytes) -> None:
        if frame_type == T_START:
            self.start(link, camera=bool(payload and payload[0] == 1))
        elif frame_type == T_KEY and len(payload) >= 2 and self.mapper is not None:
            key, down = payload[0], payload[1]
            if down:
                self.mapper.press(key)
            else:
                self.mapper.release(key)
        elif frame_type == T_ACK and len(payload) >= 2 and self.streamer is not None:
            self.streamer.on_ack(struct.unpack("<H", payload[:2])[0])
        elif frame_type == T_STOP:
            self.log("partage d'ecran arrete")
            self.stop()

    def start(self, link, camera: bool = False) -> None:
        self.stop()
        self.link = link
        self.last_status = None
        if camera:
            import numcam_service

            phone = numcam_service.phone(self.log)
            self.log("numcam demarre")
            self.mapper = CameraKeys(phone, lambda: self._status())
            self.notice = ""
            self._status()
            self.streamer = Streamer(link, self.encoder_factory(CAMERA_ROWS), lambda: PhoneCapture(phone), self.out,
                                     self.mapper, self._status, self.log, height=CAMERA_ROWS * 4, cover=True,
                                     overlay=self.mapper.overlay)
            self.streamer.start()
            return
        self.log("partage d'ecran demarre")
        self.mapper = KeyMapper(self.out, lambda: self._status())
        notice = ""
        if self.kind == "real":
            notice = self._permission_notice()
            if notice:
                self.log("NumScreen a besoin de deux autorisations de macOS : Reglages Systeme > "
                         "Confidentialite et securite > Enregistrement de l'ecran, et > Accessibilite. "
                         "Active NumSocial dans les deux (pour l'ecran, il faut ensuite relancer NumSocial).")
                if not self.asked_permissions:
                    self.asked_permissions = True
                    try:
                        self.out.request_permissions()
                    except Exception as error:
                        self.log(f"demande d'autorisation impossible: {error}")
        self.notice = notice
        self._status()
        if self.kind == "demo":
            capture = PatternCapture
        else:
            capture = lambda: ThreadedCapture(lambda: ScreenCapture(self.out))  # noqa: E731
        self.streamer = Streamer(link, self.encoder_factory(), capture, self.out, self.mapper,
                                 self._status, self.log, self.refresh_permissions)
        self.streamer.start()

    def stop(self) -> None:
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer.join(timeout=2)
            self.streamer = None
        if self.mapper is not None:
            with self.mapper.lock:
                self.mapper.release_all()
            self.mapper = None

    def detach(self, link) -> None:
        if self.link is link:
            self.stop()
            self.link = None
