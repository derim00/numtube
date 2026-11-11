"""End-to-end test of NumScreen: a fake calculator talks to the real gateway
over a pty, the way the firmware does. The gateway runs with --numscreen demo:
it sends a test picture (not the real screen) and only logs the mouse and
keyboard actions, so nothing is clicked or typed on this computer.

Checks: the picture arrives and decodes with the calculator's decoder
(Python copy), frames are paced by the acks, keys become the right actions
(mouse, shift, alpha, caps lock, game mode), and Stop releases everything.

Usage: ../.venv/bin/python3 fake_numscreen.py [--save picture.png]
"""
import os
import struct
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402
import numscreen_service as ns  # noqa: E402
from fake_numchat import FakeCalculator  # noqa: E402


def decode(picture: np.ndarray, start: int, ops: bytes) -> None:
    """Same algorithm as apps/numtube/tile_decoder.h, on an RGB565 array."""
    tile, pos, count_tiles = start, 0, ns.ROWS * 80
    while pos < len(ops) and tile < count_tiles:
        op = ops[pos]
        pos += 1
        n = (op & 0x3F) + 1
        kind = op >> 6
        if kind == 0:
            tile += n
        elif kind == 1:
            color = struct.unpack_from("<H", ops, pos)[0]
            pos += 2
            for _ in range(n):
                r, c = divmod(tile, 80)
                picture[r * 4:r * 4 + 4, c * 4:c * 4 + 4] = color
                tile += 1
        elif kind == 2:
            for _ in range(n):
                c0, c1 = struct.unpack_from("<HH", ops, pos)
                bits = ops[pos + 4:pos + 8]
                pos += 8

                def mix(a, b, wa, wb):
                    ra, ga, ba = a >> 11, (a >> 5) & 63, a & 31
                    rb, gb, bb = b >> 11, (b >> 5) & 63, b & 31
                    return (((wa * ra + wb * rb) // 3) << 11) | (((wa * ga + wb * gb) // 3) << 5) | ((wa * ba + wb * bb) // 3)
                palette = [c0, c1, mix(c0, c1, 2, 1), mix(c0, c1, 1, 2)]
                r, c = divmod(tile, 80)
                for y in range(4):
                    for x in range(4):
                        picture[r * 4 + y, c * 4 + x] = palette[(bits[y] >> (2 * x)) & 3]
                tile += 1
        else:
            return


def main() -> int:
    save = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else None
    calc = FakeCalculator()
    gateway = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"),
         "--port", calc.slave_path, "--numscreen", "demo"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    picture = np.zeros((ns.PICTURE_H, ns.PICTURE_W), dtype=np.uint16)
    statuses = []
    try:
        calc.send(gw.T_HELLO)
        calc.wait_for(gw.T_HELLO_ACK, 10)
        calc.send(ns.T_START)

        def pump(seconds: float, ack: bool = True) -> int:
            frames = 0
            for frame_type, payload in calc.frames(seconds):
                if frame_type == ns.T_FRAME:
                    frame_id, start, flags = struct.unpack_from("<HHB", payload)
                    decode(picture, start, payload[5:])
                    if flags & ns.END_OF_FRAME:
                        frames += 1
                        if ack:
                            calc.send(ns.T_ACK, payload[:2])
                elif frame_type == ns.T_STATUS:
                    statuses.append(payload.decode())
            return frames

        frames = pump(2.0)
        print(f"images recues en 2 s : {frames}")
        assert frames >= 15, "le flux d'images est trop lent"
        assert picture.any(), "image vide"
        assert statuses and statuses[-1].startswith("MODE SOURIS"), statuses
        print("etat :", statuses[-1].replace("\n", " | "))

        # Without acks the gateway must stop after 2 frames in flight.
        stalled = pump(1.0, ack=False)
        print(f"images sans accuse de reception en 1 s : {stalled}")
        assert stalled <= 2 + 1, "le controle de flux ne marche pas"
        pump(2.0)

        def press(key: int, hold: float = 0.05) -> None:
            calc.send(ns.T_KEY, bytes([key, 1]))
            pump(hold)
            calc.send(ns.T_KEY, bytes([key, 0]))
            pump(0.05)

        # Mouse: OK clicks, arrows move.
        press(ns.OK)
        press(ns.RIGHT, 0.6)
        press(ns.TOOLBOX)     # right click
        # Letters by default, alpha gives one capital, twice locks.
        press(ns.ONE)
        press(ns.ALPHA)
        press(ns.ONE)
        press(ns.ALPHA)
        press(ns.ALPHA)       # caps lock
        press(ns.EXP)
        press(ns.FIVE)
        press(ns.ALPHA)       # leaves caps lock
        # Shift: digits, alpha + key: yellow symbol, shift again: letters.
        press(ns.SHIFT)
        press(ns.ONE)
        press(ns.ALPHA)
        press(ns.PI)
        press(ns.SHIFT)
        press(ns.BACK)
        # var: Z Q S D on 8 4 5 6, held while the key is.
        press(ns.VAR)
        press(ns.EIGHT, 0.3)
        press(ns.FOUR)
        press(ns.VAR)
        # Game mode: arrows are keys, OK jumps.
        press(ns.XNT)
        press(ns.UP, 0.3)
        press(ns.OK)
        press(ns.XNT)
        press(ns.SHIFT)       # digits: exp is the computer's shift key
        calc.send(ns.T_KEY, bytes([ns.EXP, 1]))  # PC shift held...
        pump(0.1)
        calc.send(ns.T_STOP)                      # ...released by Stop
        pump(0.8)
    finally:
        gateway.terminate()
        out, _ = gateway.communicate(timeout=5)

    actions = [line.split("[entree] ", 1)[1].strip() for line in out.splitlines() if "[entree]" in line]
    print("\n".join("  " + a for a in actions))
    expected = [
        "bouton left bas", "bouton left haut",
        "bouton right bas", "bouton right haut",
        "touche w bas", "touche w haut",
        "touche W bas", "touche W haut",
        "touche A bas", "touche A haut",
        "touche S bas", "touche S haut",
        "touche 1 bas", "touche 1 haut",
        "touche = bas", "touche = haut",
        "touche esc bas", "touche esc haut",
        "touche z bas", "touche z haut",
        "touche q bas", "touche q haut",
        "touche up bas", "touche up haut",
        "touche space bas", "touche space haut",
        "touche shift bas", "touche shift haut",
    ]
    got = [" ".join(a.split()[:3]) for a in actions]
    assert got == expected, f"actions inattendues:\n{got}"
    assert any("MODE JEU" in s for s in statuses), "le mode jeu n'apparait pas"
    assert any("MAJ verrouille" in s for s in statuses), "le verrouillage majuscule n'apparait pas"
    assert any("ZQSD" in s for s in statuses), "ZQSD n'apparait pas"
    assert any("chiffres" in s for s in statuses), "le mode chiffres n'apparait pas"
    if save:
        from PIL import Image
        rgb = np.stack([(picture >> 11) << 3, ((picture >> 5) & 63) << 2, (picture & 31) << 3], axis=-1)
        Image.fromarray(rgb.astype(np.uint8)).save(save)
        print("image enregistree :", save)
    print("OK : NumScreen fonctionne avec la fausse calculatrice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
