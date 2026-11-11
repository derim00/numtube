#!/usr/bin/env python3
"""NumTube gateway: the PC side of the YouTube homebrew for NumWorks.

The calculator shows up as a USB serial port (the CDC-ACM link introduced by
Termia). It sends search and playback requests in TMIA frames. This program
answers them with yt-dlp (search, stream URL, captions) and ffmpeg (decoding),
then streams a compressed picture and timed subtitles back.

Usage:  ./numtube            (auto-detects the calculator)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import select
import socket
import struct
import subprocess
import tempfile
import sys
import threading
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# Bundled tools. In the packaged app (PyInstaller) yt-dlp runs as a mode of the
# app itself and ffmpeg/libusb come from pip packages, so nothing has to be
# installed on the computer.
# ---------------------------------------------------------------------------
FROZEN = getattr(sys, "frozen", False)
# No console window flashing up for every tool on Windows.
POPEN_FLAGS = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _ytdlp_command() -> list[str]:
    if FROZEN:
        return [sys.executable, "--yt-dlp"]
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return ["yt-dlp"]


def _ffmpeg_path() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


YTDLP = _ytdlp_command()
FFMPEG = _ffmpeg_path()


def usb_backend():
    """libusb from the libusb-package wheel when present, else the system one."""
    try:
        import libusb_package

        return libusb_package.get_libusb1_backend()
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Wire protocol. Mirrors firmware/epsilon/apps/numtube/protocol.h.
# ---------------------------------------------------------------------------
MAGIC = b"TMIA"
VERSION = 1
MAX_PAYLOAD = 4096

T_HELLO = 0x0A
T_HELLO_ACK = 0x0B
T_ERROR = 0x07
T_SEARCH = 0x40
T_RESULT = 0x41
T_SEARCH_DONE = 0x42
T_PLAY = 0x43
T_PLAY_INFO = 0x44
T_VIDEO = 0x45
T_SUBTITLE = 0x46
T_ACK = 0x47
T_CONTROL = 0x48
T_PLAY_END = 0x49
T_STATUS = 0x4A
T_LOG = 0x4B
# NumChat frames, handled by gateway/numchat_service.py.
T_CHAT_FIRST, T_CHAT_LAST = 0x60, 0x72
# NumScreen frames, handled by gateway/numscreen_service.py.
T_SCREEN_FIRST, T_SCREEN_LAST = 0x80, 0x8F

CTRL_STOP = 0
CTRL_PAUSE = 1
CTRL_RESUME = 2
CTRL_SEEK = 3

VIDEO_FLAG_END_OF_FRAME = 1

SCREEN_W, VIDEO_H = 320, 180
TILE = 4
COLS, ROWS = SCREEN_W // TILE, VIDEO_H // TILE
NUM_TILES = COLS * ROWS
CHUNK_OPS_MAX = 2000  # ops bytes per T_VIDEO frame
SUB_COLS, SUB_LINES = 32, 3  # LargeFont (10x18) in the 58 px strip
MAX_RESULTS = 8


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# The CRC runs on every video byte, so use a lookup table in the hot path.
_CRC_TABLE = [crc16(bytes([i])) ^ crc16(b"\0") for i in range(256)]


def crc16_fast(data: bytes) -> int:
    crc = 0xFFFF
    table = _CRC_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ byte) & 0xFF]
    return crc


def encode_frame(frame_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    head = MAGIC + bytes([VERSION, frame_type]) + struct.pack("<H", len(payload))
    body = head + payload
    return body + struct.pack("<H", crc16_fast(body))


class FrameParser:
    """Reassembles TMIA frames from a byte stream, resyncing on corruption."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf += data
        out: list[tuple[int, bytes]] = []
        while True:
            start = self.buf.find(MAGIC)
            if start < 0:
                del self.buf[: max(0, len(self.buf) - 3)]
                return out
            if start:
                del self.buf[:start]
            if len(self.buf) < 8:
                return out
            length = self.buf[6] | (self.buf[7] << 8)
            if self.buf[4] != VERSION or length > MAX_PAYLOAD:
                del self.buf[:1]
                continue
            total = 8 + length + 2
            if len(self.buf) < total:
                return out
            frame = bytes(self.buf[:total])
            if crc16_fast(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
                del self.buf[:1]
                continue
            del self.buf[:total]
            out.append((frame[5], frame[8:-2]))


# ---------------------------------------------------------------------------
# Video codec: 80x45 tiles of 4x4 pixels.
#
# Each frame is a raster stream of ops, one op byte followed by its data:
#   00nnnnnn              skip n+1 tiles (they keep their current pixels)
#   01nnnnnn c:u16        n+1 solid tiles of colour c
#   10nnnnnn (c0 c1 idx)* n+1 block tiles: two RGB565 end colours and 4 bytes
#                         of 2-bit palette indices (one byte per pixel row,
#                         leftmost pixel in the low bits)
# Palette: c0, c1, (2*c0+c1)/3, (c0+2*c1)/3, per 5/6/5 channel.
# ---------------------------------------------------------------------------
class TileEncoder:
    """Delta encoder. `shadow` is exactly what the calculator is showing."""

    def __init__(self, rows: int = ROWS) -> None:
        # rows of tiles: 45 for NumTube (320x180), 50 for NumScreen (320x200)
        self.rows = rows
        self.num_tiles = rows * COLS
        self.shadow = np.zeros((rows, COLS, TILE, TILE), dtype=np.int32)
        self.force_full = True

    def invalidate(self) -> None:
        self.force_full = True

    def _to_tiles(self, rgb: np.ndarray) -> np.ndarray:
        return rgb.reshape(self.rows, TILE, COLS, TILE, 3).transpose(0, 2, 1, 3, 4)

    def encode(self, rgb: np.ndarray, budget: int) -> list[tuple[int, bytes]]:
        """Returns [(start_tile, ops)] for the tiles worth sending this frame."""
        ROWS, NUM_TILES = self.rows, self.num_tiles
        tiles = self._to_tiles(rgb.astype(np.int32))
        r5, g6, b5 = tiles[..., 0] >> 3, tiles[..., 1] >> 2, tiles[..., 2] >> 3

        luma = 2 * tiles[..., 0] + 5 * tiles[..., 1] + tiles[..., 2]
        flat = luma.reshape(ROWS, COLS, 16)
        lo, hi = flat.argmin(axis=2), flat.argmax(axis=2)
        ri, ci = np.ogrid[:ROWS, :COLS]

        def pick(channel: np.ndarray, which: np.ndarray) -> np.ndarray:
            return channel.reshape(ROWS, COLS, 16)[ri, ci, which]

        # End colours are stored in 5/6/5, palette interpolated in 5/6/5.
        c0 = np.stack([pick(r5, hi), pick(g6, hi), pick(b5, hi)], axis=-1)
        c1 = np.stack([pick(r5, lo), pick(g6, lo), pick(b5, lo)], axis=-1)
        palette = np.stack([c0, c1, (2 * c0 + c1) // 3, (c0 + 2 * c1) // 3], axis=2)

        target = np.stack([r5, g6, b5], axis=-1).reshape(ROWS, COLS, 16, 3)
        weights = np.array([2, 1, 2])  # 6-bit green has twice the range
        distance = (np.abs(target[:, :, :, None, :] - palette[:, :, None, :, :]) * weights).sum(axis=-1)
        indices = distance.argmin(axis=3)
        solid = (c0 == c1).all(axis=-1)
        indices[solid] = 0

        out_565 = np.take_along_axis(palette, indices[..., None].repeat(3, axis=-1), axis=2)
        new_packed = (out_565[..., 0] << 11) | (out_565[..., 1] << 5) | out_565[..., 2]
        new_packed = new_packed.reshape(ROWS, COLS, TILE, TILE)

        shadow = self.shadow.reshape(ROWS, COLS, 16)
        old = np.stack([(shadow >> 11) & 31, (shadow >> 5) & 63, shadow & 31], axis=-1)
        error = (np.abs(old - target) * weights).sum(axis=(2, 3))

        # Skip tiles that have not visibly changed, send the worst ones first.
        cost = np.where(solid, 3, 8)
        if self.force_full:
            send = np.ones((ROWS, COLS), dtype=bool)
        else:
            send = error > 72  # about 1.5 colour steps per pixel and channel
            flat_error, flat_cost = error.ravel(), cost.ravel()
            order = np.argsort(-flat_error, kind="stable")
            order = order[send.ravel()[order]]
            spent = np.cumsum(flat_cost[order])
            send = np.zeros(NUM_TILES, dtype=bool)
            send[order[spent <= budget]] = True
            send = send.reshape(ROWS, COLS)
        self.force_full = False

        self.shadow[send] = new_packed[send]

        c0p = ((c0[..., 0] << 11) | (c0[..., 1] << 5) | c0[..., 2]).ravel().tolist()
        c1p = ((c1[..., 0] << 11) | (c1[..., 1] << 5) | c1[..., 2]).ravel().tolist()
        idx = indices.reshape(ROWS, COLS, TILE, TILE)
        idx_bytes = idx[..., 0] | (idx[..., 1] << 2) | (idx[..., 2] << 4) | (idx[..., 3] << 6)
        idx_bytes = idx_bytes.reshape(NUM_TILES, TILE).astype(np.uint8)
        return self._serialize(send.ravel().tolist(), solid.ravel().tolist(), c0p, c1p, idx_bytes)

    def _serialize(self, send, solid, c0, c1, idx_bytes) -> list[tuple[int, bytes]]:
        NUM_TILES = self.num_tiles
        chunks: list[tuple[int, bytes]] = []
        ops = bytearray()
        chunk_start = 0
        i = 0

        def flush(next_tile: int) -> None:
            nonlocal ops, chunk_start
            if ops:
                chunks.append((chunk_start, bytes(ops)))
            ops = bytearray()
            chunk_start = next_tile

        while i < NUM_TILES:
            if len(ops) >= CHUNK_OPS_MAX:
                flush(i)
            if not send[i]:
                j = i
                while j < NUM_TILES and j - i < 64 and not send[j]:
                    j += 1
                ops.append(j - i - 1)
                i = j
            elif solid[i]:
                j = i
                color = c0[i]
                while j < NUM_TILES and j - i < 64 and send[j] and solid[j] and c0[j] == color:
                    j += 1
                ops.append(0x40 | (j - i - 1))
                ops += struct.pack("<H", color)
                i = j
            else:
                j = i
                while j < NUM_TILES and j - i < 64 and send[j] and not solid[j]:
                    j += 1
                ops.append(0x80 | (j - i - 1))
                for k in range(i, j):
                    ops += struct.pack("<HH", c0[k], c1[k])
                    ops += idx_bytes[k].tobytes()
                i = j
        flush(NUM_TILES)
        # Trailing skips carry no information.
        return chunks


# ---------------------------------------------------------------------------
# YouTube access (yt-dlp) and subtitles
# ---------------------------------------------------------------------------
TRANSLATE = {
    "‘": "'", "’": "'", "“": '"', "”": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", "…": "...", " ": " ", "œ": "oe", "Œ": "OE",
    "\n": " ", "\r": " ", "\t": " ",
}
# The calculator fonts have no precomposed accented letters: they draw a base
# letter followed by a combining accent. These are the accents they support,
# plus the few non-ASCII symbols that have a glyph of their own.
FONT_COMBINING = {0x300, 0x301, 0x302, 0x303, 0x305, 0x308, 0x30A, 0x327, 0x338}
FONT_SYMBOLS = set("ª°±·ºÆÐ×ØÞßæð÷øþΓΔΣΩαβγδεζηθλμπρσχω→≈≤≥")


def _drawable(ch: str) -> bool:
    if 32 <= ord(ch) < 127 or ch in FONT_SYMBOLS:
        return True
    decomposed = unicodedata.normalize("NFD", ch)
    return 32 <= ord(decomposed[0]) < 127 and all(ord(c) in FONT_COMBINING for c in decomposed[1:])


def clean_text(text: str, keep_private: bool = False) -> str:
    """Keeps what the calculator can draw. Returns NFC: one char per glyph.
    With keep_private, private-use characters (U+E000-U+F8FF) are kept: NumChat
    uses them as references to emoji pictures sent separately."""
    text = unicodedata.normalize("NFC", text)
    out = []
    for ch in text:
        if keep_private and 0xE000 <= ord(ch) <= 0xF8FF:
            out.append(ch)
            continue
        ch = TRANSLATE.get(ch, ch)
        if all(_drawable(c) for c in ch):
            out.append(ch)
        else:
            decomposed = unicodedata.normalize("NFKD", ch)
            out.append("".join(c for c in decomposed if 32 <= ord(c) < 127))
    return " ".join("".join(out).split())


def truncate_utf8(text: str, max_bytes: int) -> bytes:
    """Wire encoding: decomposed accents (NFD), cut between whole characters."""
    out = bytearray()
    for ch in text:
        encoded = unicodedata.normalize("NFD", ch).encode("utf-8")
        if len(out) + len(encoded) > max_bytes:
            break
        out += encoded
    return bytes(out)


def wrap_lines(text: str, cols: int = SUB_COLS, lines: int = SUB_LINES) -> str:
    """Word-wraps and keeps the *last* lines, which are the most recent words."""
    out: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > cols:
            if current:
                out.append(current)
                current = ""
            out.append(word[:cols])
            word = word[cols:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= cols:
            current += " " + word
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return "\n".join(out[-lines:])


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class VideoInfo:
    title: str
    duration_ms: int
    url: str
    page_url: str
    format_id: str
    info_path: str
    headers: dict = field(default_factory=dict)
    cues: list[Cue] = field(default_factory=list)
    sub_lang: str = ""


def run_ytdlp(args: list[str], timeout: float = 60) -> dict:
    cmd = [*YTDLP, "--no-warnings", "--no-playlist", "-J", *args]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, **POPEN_FLAGS)
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "yt-dlp a echoue")
    return json.loads(result.stdout)


def search(query: str) -> list[dict]:
    data = subprocess.run(
        [*YTDLP, "--no-warnings", "--flat-playlist", "-J", f"ytsearch{MAX_RESULTS}:{query}"],
        capture_output=True, timeout=45, **POPEN_FLAGS,
    )
    if data.returncode != 0:
        lines = data.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "recherche impossible")
    return [e for e in json.loads(data.stdout).get("entries", []) if e and e.get("id")]


def choose_caption_track(info: dict, langs: list[str]) -> tuple[str, str] | None:
    """Prefers human captions, then automatic ones, in the requested languages."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def find(tracks: dict) -> tuple[str, str] | None:
        for lang in langs:
            for key in sorted(tracks):
                if key == lang or key.startswith(lang + "-"):
                    for fmt in tracks[key]:
                        if fmt.get("ext") == "json3":
                            return key, fmt["url"]
        return None

    found = find(manual) or find(auto)
    if found:
        return found
    # Automatic captions always include the original language as "<lang>-orig".
    for key in sorted(auto):
        if key.endswith("-orig"):
            for fmt in auto[key]:
                if fmt.get("ext") == "json3":
                    return key, fmt["url"]
    return None


def parse_json3(data: dict) -> list[Cue]:
    cues: list[Cue] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs or "tStartMs" not in event:
            continue
        text = clean_text("".join(seg.get("utf8", "") for seg in segs))
        if not text:
            continue
        start = int(event["tStartMs"])
        cues.append(Cue(start, start + int(event.get("dDurationMs", 2000)), text))
    cues.sort(key=lambda c: c.start_ms)
    for current, following in zip(cues, cues[1:]):
        current.end_ms = min(current.end_ms, following.start_ms) if following.start_ms > current.start_ms else current.end_ms
    return cues


def fetch_cues(info: dict, langs: list[str]) -> tuple[str, list[Cue]]:
    track = choose_caption_track(info, langs)
    if not track:
        return "", []
    lang, url = track
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                return lang, parse_json3(json.load(response))
        except Exception as error:  # YouTube often answers 429 on the first try
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    log(f"sous-titres indisponibles: {last_error}")
    return "", []


def resolve_video(video_id: str, langs: list[str]) -> VideoInfo:
    info = run_ytdlp([
        "-f", "bv*[protocol^=http][vcodec!*=av01]/b[protocol^=http]/bv*/b",
        "-S", "res:360,+size",
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    chosen = info
    if info.get("requested_formats"):
        chosen = info["requested_formats"][0]
    lang, cues = fetch_cues(info, langs)
    # Saved so that the yt-dlp fallback does not have to analyse the page again.
    info_path = os.path.join(tempfile.gettempdir(), f"numtube-{os.getpid()}.info.json")
    with open(info_path, "w") as handle:
        json.dump(info, handle)
    return VideoInfo(
        title=clean_text(info.get("title") or video_id),
        duration_ms=int((info.get("duration") or 0) * 1000),
        url=chosen["url"],
        page_url=f"https://www.youtube.com/watch?v={video_id}",
        format_id=str(chosen.get("format_id") or info.get("format_id") or "b"),
        info_path=info_path,
        headers=chosen.get("http_headers") or {},
        cues=cues,
        sub_lang=lang,
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class SerialLink:
    """Raw termios access to the calculator's CDC port (stdlib only)."""

    def __init__(self, path: str, sock: socket.socket | None = None) -> None:
        self.path = path
        self.write_lock = threading.Lock()
        self.bytes_sent = 0
        self.sock = sock
        if sock is not None:  # simulator link, see --tcp
            sock.setblocking(False)
            self.fd = sock.fileno()
            return
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        import termios  # POSIX only; this link is for tests and the simulator

        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0  # lflag
        attrs[4] = attrs[5] = termios.B115200  # the speed is ignored on CDC
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        data = os.read(self.fd, 65536)
        if not data:
            # macOS sometimes wakes select() on a tty with nothing to read;
            # closing the port then would drop requests and upset the
            # calculator. Only a vanished device or a closed socket ends it.
            if self.sock is not None or not os.path.exists(self.path):
                raise OSError("port closed")
            time.sleep(0.01)
        return data

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        frame = encode_frame(frame_type, payload)
        with self.write_lock:
            view = memoryview(frame)
            while view:
                try:
                    written = os.write(self.fd, view)
                    view = view[written:]
                except BlockingIOError:
                    select.select([], [self.fd], [], 1.0)
            self.bytes_sent += len(frame)

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
                return
            os.close(self.fd)
        except OSError:
            pass


NUMTUBE_VID, NUMTUBE_PID = 0x0483, 0xA292
EP_OUT, EP_IN, DATA_INTERFACE = 0x02, 0x82, 0


class UsbLink:
    """Talks to the calculator's bulk endpoints directly through libusb.

    The calculator exposes a vendor-class interface, so no macOS driver
    claims it; this avoids the serial (tty) layer entirely."""

    def __init__(self, device) -> None:
        import usb.util

        self.device = device
        self.path = "usb"
        self.write_lock = threading.Lock()
        self.bytes_sent = 0
        try:
            device.get_active_configuration()
        except usb.core.USBError:
            device.set_configuration()
        usb.util.claim_interface(device, DATA_INTERFACE)

    @staticmethod
    def _is_timeout(error) -> bool:
        import usb.core

        return isinstance(error, usb.core.USBTimeoutError) or getattr(error, "errno", None) in (60, 110)

    def read(self, timeout: float) -> bytes:
        import usb.core

        try:
            # One packet per transfer, and no timeout: an aborted transfer
            # makes the host reset the endpoint, which only adds risk.
            return bytes(self.device.read(EP_IN, 64, 0))
        except usb.core.USBError as error:
            if self._is_timeout(error):
                return b""
            raise OSError(str(error)) from error

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        import usb.core

        frame = encode_frame(frame_type, payload)
        with self.write_lock:
            try:
                self.device.write(EP_OUT, frame, 3000)
            except usb.core.USBError as error:
                if self._is_timeout(error):
                    # The calculator is not reading (it left the player):
                    # dropping the frame is the right thing to do.
                    return
                raise OSError(str(error)) from error
            self.bytes_sent += len(frame)

    def close(self) -> None:
        import usb.util

        try:
            usb.util.release_interface(self.device, DATA_INTERFACE)
            usb.util.dispose_resources(self.device)
        except Exception:
            pass


def find_usb_device():
    import usb.core

    return usb.core.find(idVendor=NUMTUBE_VID, idProduct=NUMTUBE_PID, backend=usb_backend())


def find_port() -> str | None:
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


LOG_LISTENERS: list = []


def log(message: str) -> None:
    line = f"{time.strftime('[%H:%M:%S]')} {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # windowed app without a console
    for listener in LOG_LISTENERS:
        listener(line)


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------
class Player(threading.Thread):
    """Decodes one video and paces it to the calculator."""

    def __init__(self, link: SerialLink, video_id: str, start_ms: int, opts: argparse.Namespace) -> None:
        super().__init__(daemon=True)
        self.link = link
        self.video_id = video_id
        self.position_ms = start_ms
        self.opts = opts
        self.fps = opts.fps
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.paused = False
        self.seek_to: int | None = None
        self.encoder = TileEncoder()
        self.frame_counter = 0
        self.pending_sizes: dict[int, int] = {}
        self.pending_started: dict[int, float] = {}
        # Link capacity in bytes/s: how fast the calculator receives *and*
        # draws a big picture. Measured on large frames only, since small
        # ones say more about the video than about the cable.
        self.rate = 300_000.0
        self.process: subprocess.Popen | None = None
        # Some videos refuse direct access from ffmpeg (HTTP 403). For those,
        # yt-dlp downloads and pipes the stream; seeking is slower but works.
        self.downloader: subprocess.Popen | None = None
        self.use_pipe = False
        self.info: VideoInfo | None = None

    # -- commands coming from the serial reader thread
    def stop(self) -> None:
        self.stop_event.set()
        self._kill()

    def _kill(self) -> None:
        for process in (self.process, self.downloader):
            if process and process.poll() is None:
                process.kill()

    def set_paused(self, paused: bool) -> None:
        with self.lock:
            self.paused = paused

    def seek(self, delta_ms: int) -> None:
        with self.lock:
            base = self.seek_to if self.seek_to is not None else self.position_ms
            limit = self.info.duration_ms - 2000 if self.info and self.info.duration_ms else base + delta_ms
            self.seek_to = max(0, min(base + delta_ms, max(0, limit)))
        self._kill()

    def on_ack(self, frame_id: int) -> None:
        with self.lock:
            if frame_id not in self.pending_sizes:
                return
            size = self.pending_sizes.pop(frame_id)
            started = self.pending_started.pop(frame_id)
            # Frames before this one were lost or already handled.
            for stale in [f for f in self.pending_sizes if ((frame_id - f) & 0xFFFF) < 0x8000]:
                self.pending_sizes.pop(stale)
                self.pending_started.pop(stale, None)
            elapsed = time.monotonic() - started
            if size >= 6_000 and elapsed > 0:
                measured = min(max(size / elapsed, 30_000), 1_500_000)
                self.rate = 0.7 * self.rate + 0.3 * measured

    # -- internals
    def _spawn_ffmpeg(self, position_ms: int) -> subprocess.Popen:
        assert self.info is not None
        vf = (
            f"scale={SCREEN_W}:{VIDEO_H}:force_original_aspect_ratio=decrease:flags=area,"
            f"pad={SCREEN_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,fps={self.fps},format=rgb24"
        )
        cmd = [FFMPEG, "-nostdin", "-loglevel", "error"]
        source = None
        self.downloader = None
        if self.use_pipe:
            self.downloader = subprocess.Popen(
                [*YTDLP, "--no-warnings", "-q", "--load-info-json", self.info.info_path,
                 "-f", self.info.format_id, "-o", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **POPEN_FLAGS,
            )
            source = self.downloader.stdout
            cmd += ["-ss", f"{position_ms / 1000:.3f}", "-i", "pipe:0"]
        else:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1"]
            if self.info.headers:
                header_text = "".join(f"{k}: {v}\r\n" for k, v in self.info.headers.items())
                cmd += ["-headers", header_text]
            cmd += ["-ss", f"{position_ms / 1000:.3f}", "-i", self.info.url]
        cmd += ["-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        process = subprocess.Popen(cmd, stdin=source if source else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=SCREEN_W * VIDEO_H * 3 * 4, **POPEN_FLAGS)
        if self.downloader and self.downloader.stdout:
            self.downloader.stdout.close()  # ffmpeg owns the read end now
        return process

    def _in_flight(self) -> int:
        with self.lock:
            return len(self.pending_sizes)

    def _send_frame(self, rgb: np.ndarray, pts_ms: int) -> None:
        # One frame should take about one frame period on the wire.
        budget = int(self.rate / self.fps)
        budget = max(1_500, min(budget, 40_000))
        chunks = self.encoder.encode(rgb, budget)
        self.frame_counter = (self.frame_counter + 1) & 0xFFFF
        frame_id = self.frame_counter
        if not chunks:
            chunks = [(0, b"")]
        payloads = [
            struct.pack("<HIHB", frame_id, pts_ms & 0xFFFFFFFF, start,
                        VIDEO_FLAG_END_OF_FRAME if i == len(chunks) - 1 else 0) + ops
            for i, (start, ops) in enumerate(chunks)
        ]
        with self.lock:
            self.pending_sizes[frame_id] = sum(len(p) + 10 for p in payloads)
            self.pending_started[frame_id] = time.monotonic()
        for payload in payloads:
            self.link.send(T_VIDEO, payload)

    def _send_subtitle(self, text: str) -> None:
        self.link.send(T_SUBTITLE, truncate_utf8(text, 200))

    def run(self) -> None:
        try:
            self._run()
        except OSError:
            pass  # the calculator was unplugged, the main loop handles it
        except Exception as error:
            log(f"erreur de lecture: {error}")
            try:
                self.link.send(T_ERROR, truncate_utf8(clean_text(str(error)), 120))
                self.link.send(T_PLAY_END, b"\x01")
            except OSError:
                pass

    def _run(self) -> None:
        self.link.send(T_STATUS, "Connexion a YouTube...".encode())
        self.info = resolve_video(self.video_id, self.opts.lang)
        info = self.info
        subs_state = f"sous-titres {info.sub_lang}" if info.cues else "AUCUN sous-titre"
        log(f"lecture: {info.title} ({format_duration(info.duration_ms / 1000)}, {subs_state})")
        self.link.send(
            T_PLAY_INFO,
            struct.pack("<IBB", info.duration_ms, self.fps, 1 if info.cues else 0) + truncate_utf8(info.title, 100),
        )

        frame_bytes = SCREEN_W * VIDEO_H * 3
        frame_period = 1.0 / self.fps
        cue_index = -1
        last_sub_text: str | None = None
        last_report = time.monotonic()
        sent_since_report = 0
        bytes_at_report = self.link.bytes_sent

        while not self.stop_event.is_set():
            with self.lock:
                if self.seek_to is not None:
                    self.position_ms = self.seek_to
                    self.seek_to = None
            segment_start_ms = self.position_ms
            self.link.send(T_STATUS, b"Chargement...")
            self.process = self._spawn_ffmpeg(segment_start_ms)
            assert self.process.stdout is not None
            self.encoder.invalidate()
            clock_origin: float | None = None
            frame_index = 0
            ended_naturally = False

            while not self.stop_event.is_set():
                with self.lock:
                    seek_pending = self.seek_to is not None
                    paused = self.paused
                if seek_pending:
                    break
                if paused:
                    time.sleep(0.03)
                    if clock_origin is not None:
                        clock_origin += 0.03
                    continue

                raw = self.process.stdout.read(frame_bytes)
                if raw is None or len(raw) < frame_bytes:
                    ended_naturally = self.process.poll() is not None or not raw
                    break

                now = time.monotonic()
                if clock_origin is None:
                    clock_origin = now
                    self.link.send(T_STATUS, b"")
                due = clock_origin + frame_index * frame_period
                pts_ms = segment_start_ms + int(frame_index * 1000 / self.fps)
                frame_index += 1
                self.position_ms = pts_ms

                # A long stall (network buffering) restarts the clock instead
                # of fast-forwarding through the backlog.
                if now - due > 1.5:
                    clock_origin = now - (frame_index - 1) * frame_period
                    due = now
                elif due > now:
                    self.stop_event.wait(due - now)

                # Subtitles follow the clock even when a picture is dropped.
                new_text = ""
                while cue_index + 1 < len(info.cues) and info.cues[cue_index + 1].start_ms <= pts_ms:
                    cue_index += 1
                while cue_index >= 0 and info.cues[cue_index].start_ms > pts_ms:
                    cue_index -= 1
                if cue_index >= 0 and pts_ms < info.cues[cue_index].end_ms:
                    new_text = wrap_lines(info.cues[cue_index].text)
                if new_text != last_sub_text:
                    self._send_subtitle(new_text)
                    last_sub_text = new_text

                # Credit-based flow control: drop pictures when the
                # calculator has not finished the previous ones.
                if self._in_flight() >= 2:
                    continue
                self._send_frame(np.frombuffer(raw, dtype=np.uint8).reshape(VIDEO_H, SCREEN_W, 3), pts_ms)
                sent_since_report += 1

                if now - last_report >= 5:
                    elapsed = now - last_report
                    kb = (self.link.bytes_sent - bytes_at_report) / 1024 / elapsed
                    log(f"  {sent_since_report / elapsed:4.1f} i/s, {kb:5.0f} Ko/s, debit estime {self.rate / 1024:5.0f} Ko/s")
                    last_report, sent_since_report, bytes_at_report = now, 0, self.link.bytes_sent

            self._kill()
            self.process.wait()
            if self.downloader:
                self.downloader.wait()
            if self.stop_event.is_set():
                return
            with self.lock:
                if self.seek_to is not None:
                    cue_index = -1
                    continue
            if frame_index == 0:
                if not self.use_pipe:
                    log("  acces direct refuse, passage par yt-dlp")
                    self.use_pipe = True
                    continue
                raise RuntimeError("Video illisible")
            if ended_naturally:
                self.link.send(T_SUBTITLE, b"")
                self.link.send(T_PLAY_END, b"\x00")
                return


# ---------------------------------------------------------------------------
# Session: dispatches requests from one connected calculator
# ---------------------------------------------------------------------------
_CHAT_SERVICE = None
_CHAT_LOCK = threading.Lock()


def chat_service(opts: argparse.Namespace):
    """The NumChat service, shared by every calculator connection so that the
    browser session survives reconnections. Created on first use only: NumTube
    alone never starts a browser."""
    global _CHAT_SERVICE
    with _CHAT_LOCK:
        if _CHAT_SERVICE is None:
            import numchat_service as chat

            backend = getattr(opts, "numchat", "snapchat")
            factory = chat.DemoBackend if backend == "demo" else chat.SnapBackend
            _CHAT_SERVICE = chat.ChatService(factory, clean_text, truncate_utf8, log)
        return _CHAT_SERVICE


_SCREEN_SERVICE = None


def screen_service(opts: argparse.Namespace):
    """The NumScreen service (screen sharing), created on first use."""
    global _SCREEN_SERVICE
    with _CHAT_LOCK:
        if _SCREEN_SERVICE is None:
            import numscreen_service as screen

            kind = getattr(opts, "numscreen", "real")
            _SCREEN_SERVICE = screen.ScreenService(
                lambda: TileEncoder(screen.ROWS), clean_text, truncate_utf8, log, kind)
        return _SCREEN_SERVICE


class Session:
    def __init__(self, link: SerialLink, opts: argparse.Namespace) -> None:
        self.link = link
        self.opts = opts
        self.player: Player | None = None
        self.chat = None
        self.screen = None

    def stop_player(self) -> None:
        if self.player:
            self.player.stop()
            self.player = None

    def do_search(self, query: str) -> None:
        log(f"recherche: {query}")
        try:
            entries = search(query)
        except Exception as error:
            log(f"echec de la recherche: {error}")
            self.link.send(T_ERROR, truncate_utf8(clean_text(str(error)), 120))
            return
        for entry in entries:
            title = truncate_utf8(clean_text(entry.get("title") or "?"), 90)
            channel = truncate_utf8(clean_text(entry.get("channel") or entry.get("uploader") or ""), 30)
            video_id = entry["id"].encode()[:16]
            payload = bytes([len(video_id)]) + video_id
            payload += struct.pack("<I", int(entry.get("duration") or 0))
            payload += bytes([len(title)]) + title + bytes([len(channel)]) + channel
            self.link.send(T_RESULT, payload)
        self.link.send(T_SEARCH_DONE, bytes([len(entries)]))
        log(f"  {len(entries)} resultats envoyes")

    def handle(self, frame_type: int, payload: bytes) -> None:
        if T_CHAT_FIRST <= frame_type <= T_CHAT_LAST:
            if self.chat is None:
                self.chat = chat_service(self.opts)
                self.chat.attach(self.link)
            self.chat.handle(self.link, frame_type, payload)
            return
        if T_SCREEN_FIRST <= frame_type <= T_SCREEN_LAST:
            if self.screen is None:
                self.screen = screen_service(self.opts)
            self.screen.handle(self.link, frame_type, payload)
            return
        if frame_type == T_LOG:
            log("  [calc] " + payload.decode("utf-8", "replace"))
        elif frame_type == T_HELLO:
            log("calculatrice connectee")
            self.stop_player()
            if self.screen is not None:
                self.screen.detach(self.link)
            self.link.send(T_HELLO_ACK, b"numtube/1")
        elif frame_type == T_SEARCH:
            query = payload.decode("utf-8", "ignore").strip()
            self.stop_player()
            threading.Thread(target=self._safe_search, args=(query,), daemon=True).start()
        elif frame_type == T_PLAY and len(payload) >= 4:
            start_ms = struct.unpack("<I", payload[:4])[0]
            video_id = payload[4:].decode("ascii", "ignore")
            self.stop_player()
            self.player = Player(self.link, video_id, start_ms, self.opts)
            self.player.start()
        elif frame_type == T_ACK and len(payload) >= 2 and self.player:
            self.player.on_ack(struct.unpack("<H", payload[:2])[0])
        elif frame_type == T_CONTROL and payload and self.player:
            command = payload[0]
            if command == CTRL_STOP:
                self.stop_player()
            elif command == CTRL_PAUSE:
                self.player.set_paused(True)
            elif command == CTRL_RESUME:
                self.player.set_paused(False)
            elif command == CTRL_SEEK and len(payload) >= 5:
                self.player.seek(struct.unpack("<i", payload[1:5])[0])

    def _safe_search(self, query: str) -> None:
        try:
            self.do_search(query)
        except OSError:
            pass

    def run(self) -> None:
        parser = FrameParser()
        # Termia's handshake: greet first, the calculator may already wait.
        self.link.send(T_HELLO_ACK, b"numtube/1")
        try:
            while True:
                data = self.link.read(0.25)
                if data:
                    for frame_type, payload in parser.feed(data):
                        self.handle(frame_type, payload)
        finally:
            self.stop_player()
            if self.chat is not None:
                self.chat.detach(self.link)
            if self.screen is not None:
                self.screen.detach(self.link)


def serve_usb(opts: argparse.Namespace, status=None) -> None:
    """Serves calculators over direct USB, forever. `status(connected)` is
    called when a calculator comes or goes (used by the app's page)."""
    log("en attente de la calculatrice (ouvre NumTube dessus)...")
    while True:
        device = find_usb_device()
        if device is None:
            time.sleep(0.3)
            continue
        try:
            link = UsbLink(device)
        except Exception as error:
            log(f"acces USB impossible: {error}")
            time.sleep(2)
            continue
        log("calculatrice trouvee (USB direct)")
        if status:
            status(True)
        try:
            Session(link, opts).run()
        except OSError as error:
            log(f"calculatrice deconnectee ({error})")
        finally:
            link.close()
            if status:
                status(False)
        time.sleep(0.3)


def default_options() -> argparse.Namespace:
    # NUMCHAT=demo in the environment: NumChat talks to a fake messaging app.
    numchat = "demo" if os.environ.get("NUMCHAT") == "demo" else "snapchat"
    # NUMSCREEN=demo: NumScreen sends a test picture and only logs the keys.
    numscreen = "demo" if os.environ.get("NUMSCREEN") == "demo" else "real"
    return argparse.Namespace(port=None, tcp=None, fps=15, lang=["fr", "en"], numchat=numchat,
                              numscreen=numscreen)


def main() -> int:
    ap = argparse.ArgumentParser(description="Passerelle YouTube pour NumWorks")
    ap.add_argument("--port", help="port serie (detection automatique sinon)")
    ap.add_argument("--fps", type=int, default=15, help="images par seconde visees (defaut: 15)")
    ap.add_argument("--tcp", type=int, nargs="?", const=9735, metavar="PORT",
                    help="attend le simulateur NumWorks sur ce port TCP au lieu du cable")
    ap.add_argument("--lang", default="fr,en", help="langues des sous-titres par priorite (defaut: fr,en)")
    ap.add_argument("--numchat", choices=["snapchat", "demo"], default="snapchat",
                    help="messagerie de NumChat: Snapchat Web, ou une demo pour tester (defaut: snapchat)")
    ap.add_argument("--numscreen", choices=["real", "demo"], default="real",
                    help="NumScreen: le vrai ecran et la vraie souris, ou une image de test sans "
                         "rien piloter (defaut: real)")
    opts = ap.parse_args()
    opts.lang = [lang.strip() for lang in opts.lang.split(",") if lang.strip()]

    for tool in ([] if FROZEN else ["ffmpeg"] if FFMPEG == "ffmpeg" else []):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            print(f"{tool} est introuvable. Installe-le avec: brew install {tool}", file=sys.stderr)
            return 1

    if opts.tcp:
        server = socket.create_server(("127.0.0.1", opts.tcp))
        log(f"en attente du simulateur sur le port {opts.tcp}...")
        while True:
            connection, _ = server.accept()
            log("simulateur connecte")
            link = SerialLink("tcp", connection)
            try:
                Session(link, opts).run()
            except OSError:
                log("simulateur deconnecte")
            finally:
                link.close()

    if not opts.port:
        serve_usb(opts)

    while True:
        path = opts.port
        if not path or not os.path.exists(path):
            time.sleep(0.5)
            continue
        try:
            link = SerialLink(path)
        except OSError as error:
            log(f"ouverture de {path} impossible: {error}")
            time.sleep(1)
            continue
        log(f"port ouvert: {path}")
        try:
            Session(link, opts).run()
        except OSError:
            log("calculatrice deconnectee")
        finally:
            link.close()
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
