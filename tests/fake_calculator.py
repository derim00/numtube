"""End-to-end test: a fake calculator talks to the real gateway over a pty.

Speaks the same protocol as the firmware (search, play, acks, pause, seek,
stop), runs the received video through the calculator's C++ decoder, and saves
snapshots of what the screen would show, subtitles included.

Usage: ../.venv/bin/python3 fake_calculator.py "query" [throughput_kB_s]
"""
import os
import pty
import struct
import subprocess
import sys
import time
import tty

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402
from codec_test import HARNESS, build_harness, to_rgb  # noqa: E402

W, H = gw.SCREEN_W, gw.VIDEO_H


class FakeCalculator:
    def __init__(self, throughput: float) -> None:
        self.master, slave = pty.openpty()
        tty.setraw(self.master)
        self.slave_path = os.ttyname(slave)
        self.parser = gw.FrameParser()
        self.throughput = throughput
        self.harness = subprocess.Popen([HARNESS], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.subtitle = ""
        self.status = ""
        self.frames_shown = 0
        self.bytes_in = 0

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        os.write(self.master, gw.encode_frame(frame_type, payload))

    def receive(self, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            import select
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                yield None, b""  # lets the caller run its script while idle
                continue
            data = os.read(self.master, 4096)
            self.bytes_in += len(data)
            # Emulate the USB link speed of the real calculator.
            time.sleep(len(data) / self.throughput)
            for frame in self.parser.feed(data):
                yield frame

    def snapshot(self, name: str) -> str:
        self.harness.stdin.write(b"\x02")
        self.harness.stdin.flush()
        picture = np.frombuffer(self.harness.stdout.read(W * H * 2), dtype="<u2").reshape(H, W)
        path = os.path.join(HERE, name)
        raw = to_rgb(picture).tobytes()
        text = (self.status or self.subtitle).replace("\n", "\\n") or " "
        subprocess.run([
            "magick", "-size", f"{W}x{H}", "-depth", "8", "rgb:-",
            "-background", "black", "-gravity", "north", "-extent", f"{W}x240",
            "-font", "/System/Library/Fonts/Supplemental/Arial.ttf", "-fill", "white", "-pointsize", "15", "-gravity", "south",
            "-annotate", "+0+6", text.replace("\\n", "\n"), path,
        ], input=raw, check=True)
        return path

    def handle_video(self, payload: bytes) -> None:
        frame_id, _pts, start, flags = struct.unpack("<HIHB", payload[:9])
        ops = payload[9:]
        self.harness.stdin.write(b"\x01" + struct.pack("<HI", start, len(ops)) + ops)
        if flags & gw.VIDEO_FLAG_END_OF_FRAME:
            self.frames_shown += 1
            self.send(gw.T_ACK, struct.pack("<H", frame_id))


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "numworks"
    throughput = float(sys.argv[2]) * 1024 if len(sys.argv) > 2 else 400 * 1024
    build_harness()
    calc = FakeCalculator(throughput)
    gateway = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"), "--port", calc.slave_path],
    )
    try:
        calc.send(gw.T_HELLO)
        assert any(t == gw.T_HELLO_ACK for t, _ in calc.receive(5) if t), "pas de HELLO_ACK"
        print("handshake ok")

        calc.send(gw.T_SEARCH, query.encode())
        results = []
        for frame_type, payload in calc.receive(60):
            if frame_type == gw.T_RESULT:
                n = payload[0]
                video_id = payload[1:1 + n].decode()
                duration = struct.unpack("<I", payload[1 + n:5 + n])[0]
                tlen = payload[5 + n]
                title = payload[6 + n:6 + n + tlen].decode()
                results.append((video_id, duration, title))
            elif frame_type == gw.T_SEARCH_DONE:
                break
        print(f"{len(results)} resultats:")
        for r in results:
            print("   ", r)
        assert results, "aucun resultat"

        video_id = next((r[0] for r in results if 30 < r[1] < 1200), results[0][0])
        calc.send(gw.T_PLAY, struct.pack("<I", 0) + video_id.encode())
        script = [(10.0, "snap", "e2e_1.png"), (10.5, "seek", 30000), (16.0, "snap", "e2e_2.png"),
                  (16.5, "pause", None), (18.0, "resume", None), (23.0, "snap", "e2e_3.png"),
                  (23.5, "stop", None)]
        started = None
        info = None
        for frame_type, payload in calc.receive(120):
            if frame_type == gw.T_PLAY_INFO:
                info = struct.unpack("<IBB", payload[:6]) + (payload[6:].decode(),)
                print("PLAY_INFO", info)
                started = time.monotonic()
            elif frame_type == gw.T_VIDEO:
                calc.handle_video(payload)
            elif frame_type == gw.T_SUBTITLE:
                calc.subtitle = payload.decode()
            elif frame_type == gw.T_STATUS:
                calc.status = payload.decode()
            elif frame_type in (gw.T_ERROR, gw.T_PLAY_END):
                print("fin:", frame_type, payload)
                break
            if started is not None and script and time.monotonic() - started >= script[0][0]:
                _, action, arg = script.pop(0)
                if action == "snap":
                    print(f"t={time.monotonic() - started:4.1f}s images={calc.frames_shown} "
                          f"sous-titre={calc.subtitle!r} -> {calc.snapshot(arg)}")
                elif action == "seek":
                    calc.send(gw.T_CONTROL, struct.pack("<Bi", gw.CTRL_SEEK, arg))
                elif action == "pause":
                    calc.send(gw.T_CONTROL, bytes([gw.CTRL_PAUSE]))
                    paused_at = calc.frames_shown
                elif action == "resume":
                    print(f"pendant la pause: {calc.frames_shown - paused_at} image(s) recue(s)")
                    calc.send(gw.T_CONTROL, bytes([gw.CTRL_RESUME]))
                elif action == "stop":
                    calc.send(gw.T_CONTROL, bytes([gw.CTRL_STOP]))
                    break
        elapsed = time.monotonic() - started if started else 0
        print(f"{calc.frames_shown} images en {elapsed:.1f}s ({calc.bytes_in / 1024 / max(elapsed, 1):.0f} Ko/s recus)")
        leftovers = [f for f in calc.receive(1.5) if f[0]]
        print(f"apres STOP: {len(leftovers)} trame(s) residuelle(s)")
        return 0 if calc.frames_shown > 20 and not script else 1
    finally:
        gateway.terminate()
        gateway.wait()


if __name__ == "__main__":
    sys.exit(main())
