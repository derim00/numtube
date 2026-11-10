"""Checks that the calculator decoder reproduces exactly what the encoder tracks.

Encodes a few seconds of a local or synthetic video, feeds every chunk to the
C++ decoder (tests/decode_harness.cpp, the code that runs on the calculator)
and compares its picture with the encoder's shadow after every frame.
Also feeds random garbage to make sure the decoder never writes out of bounds.

Usage: ../.venv/bin/python3 codec_test.py [video] [budget_bytes]
"""
import os
import random
import struct
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402

HARNESS = os.path.join(HERE, "decode_harness")
W, H = gw.SCREEN_W, gw.VIDEO_H


def build_harness() -> None:
    source = os.path.join(HERE, "decode_harness.cpp")
    include = os.path.join(HERE, "..", "firmware", "epsilon", "apps", "numtube")
    subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-fsanitize=address,undefined",
                    "-I", include, source, "-o", HARNESS], check=True)


def frames(video: str | None, count: int):
    if video is None:
        video = "testsrc2=size=640x360:rate=15"
        src = ["-f", "lavfi", "-i", video]
    else:
        src = ["-i", video]
    cmd = ["ffmpeg", "-loglevel", "error", *src, "-frames:v", str(count),
           "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=area,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps=15,format=rgb24",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = W * H * 3
    while True:
        raw = proc.stdout.read(size)
        if len(raw) < size:
            break
        yield np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)
    proc.wait()


def shadow_picture(encoder: gw.TileEncoder) -> np.ndarray:
    return encoder.shadow.transpose(0, 2, 1, 3).reshape(H, W).astype(np.uint16)


def to_rgb(picture: np.ndarray) -> np.ndarray:
    r = ((picture >> 11) & 31) * 255 // 31
    g = ((picture >> 5) & 63) * 255 // 63
    b = (picture & 31) * 255 // 31
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(float) - b.astype(float)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def main() -> int:
    video = sys.argv[1] if len(sys.argv) > 1 else None
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    build_harness()
    harness = subprocess.Popen([HARNESS], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    encoder = gw.TileEncoder()
    sizes, scores = [], []
    last_source = None
    for index, rgb in enumerate(frames(video, 90)):
        chunks = encoder.encode(rgb, budget)
        size = 0
        for start, ops in chunks:
            harness.stdin.write(b"\x01" + struct.pack("<HI", start, len(ops)) + ops)
            size += len(ops) + 19
            assert len(ops) + 9 <= gw.MAX_PAYLOAD
        harness.stdin.write(b"\x02")
        harness.stdin.flush()
        decoded = np.frombuffer(harness.stdout.read(W * H * 2), dtype="<u2").reshape(H, W)
        expected = shadow_picture(encoder)
        if not np.array_equal(decoded, expected):
            diff = np.argwhere(decoded != expected)
            print(f"MISMATCH frame {index}: {len(diff)} pixels, first {diff[:3].tolist()}")
            return 1
        sizes.append(size)
        scores.append(psnr(to_rgb(decoded), rgb))
        last_source = (rgb, decoded)
    harness.stdin.close()
    if harness.wait() != 0:
        print("decoder harness failed")
        return 1

    # Garbage must never crash or write out of bounds (ASan is on).
    harness = subprocess.Popen([HARNESS], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    rng = random.Random(1)
    for _ in range(3000):
        ops = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 600)))
        start = rng.randint(0, 5000)
        harness.stdin.write(b"\x01" + struct.pack("<HI", start, len(ops)) + ops)
    harness.stdin.close()
    if harness.wait() != 0:
        print("decoder crashed on random input")
        return 1

    from_png = os.path.join(HERE, "codec_preview.png")
    rgb, decoded = last_source
    side = np.concatenate([rgb, to_rgb(decoded)], axis=1)
    subprocess.run(["magick", "-size", f"{W * 2}x{H}", "-depth", "8", "rgb:-", from_png],
                   input=side.tobytes(), check=True)
    print(f"OK: {len(sizes)} images identiques au bit pres entre encodeur et decodeur")
    print(f"taille moyenne {np.mean(sizes) / 1024:.1f} Ko/image (max {max(sizes) / 1024:.1f}), "
          f"PSNR moyen {np.mean(scores[5:]):.1f} dB, budget {budget} o")
    print(f"apercu (source | calculatrice): {from_png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
