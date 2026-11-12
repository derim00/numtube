"""End-to-end test of NumCam: a fake calculator (over a pty) and a fake phone (HTTPS requests, as the
page would make them) with the real gateway. Pictures go in a temporary folder.

Checks: the page needs the secret code, the phone's pictures reach the calculator, OK takes a photo,
EXE starts and stops a video, shift turns to the other camera, var turns the picture, the files land
in the folder.

Usage: ../.venv/bin/python3 fake_numcam.py
"""
import io
import json
import os
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402
import numscreen_service as ns  # noqa: E402
from fake_numchat import FakeCalculator  # noqa: E402
from fake_numscreen import decode  # noqa: E402

UNVERIFIED = ssl._create_unverified_context()


def jpeg(color) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(out, "JPEG", quality=80)
    return out.getvalue()


def main() -> int:
    folder = tempfile.mkdtemp(prefix="numcam-")
    port = 9300 + os.getpid() % 500
    env = dict(os.environ, NUMCAM_DIR=folder, NUMCAM_PORT=str(port), NUMSCREEN="demo")
    calc = FakeCalculator()
    gateway = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"),
                                "--port", calc.slave_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=env)
    picture = np.zeros((ns.PICTURE_H, ns.PICTURE_W), dtype=np.uint16)
    statuses = []
    ok = False

    def pump(seconds):
        for kind, payload in calc.frames(seconds):
            if kind == ns.T_FRAME:
                decode(picture, struct.unpack_from("<H", payload, 2)[0], payload[5:])
                if payload[4] & ns.END_OF_FRAME:
                    calc.send(ns.T_ACK, payload[:2])
            elif kind == ns.T_STATUS:
                statuses.append(payload.decode())

    def press(key):
        calc.send(ns.T_KEY, bytes([key, 1]))
        pump(0.05)
        calc.send(ns.T_KEY, bytes([key, 0]))

    try:
        calc.send(gw.T_HELLO)
        calc.wait_for(gw.T_HELLO_ACK, 10)
        calc.send(ns.T_START, b"\x01")   # NumCam
        pump(2)
        assert statuses and statuses[-1].startswith("Sur le telephone"), statuses
        address = statuses[-1].split("\n")[1]
        token = address.split("k=")[1]
        print("la calculatrice affiche l'adresse :", address)
        base = f"https://127.0.0.1:{port}"

        def call(path, body=None):
            request = urllib.request.Request(base + path, data=body, method="POST" if body is not None else "GET")
            with urllib.request.urlopen(request, context=UNVERIFIED, timeout=10) as r:
                return r.read()

        try:
            call("/?k=mauvais")
            raise AssertionError("page ouverte sans le bon code")
        except urllib.error.HTTPError as error:
            assert error.code == 403
        assert b"getUserMedia" in call(f"/?k={token}")
        print("la page demande le code secret, et s'ouvre avec")

        def frame(color=(200, 30, 30)):
            return json.loads(call(f"/frame?k={token}", jpeg(color)))

        for _ in range(10):
            frame()
            pump(0.07)
        pump(0.5)
        red = (picture >> 11) > 20
        assert red.mean() > 0.5, "l'image du telephone n'arrive pas"
        assert statuses[-1].startswith("OK : photo"), statuses[-1]
        print("l'image du telephone arrive sur la calculatrice")

        press(ns.OK)
        pump(0.2)
        r = frame()
        assert {"do": "photo"} in r["commands"], r
        call(f"/photo?k={token}", jpeg((10, 200, 10)))
        press(ns.EXE)
        pump(0.2)
        assert {"do": "rec_start"} in frame()["commands"]
        pump(1.2)
        assert "REC 00:0" in statuses[-1], statuses[-1]
        press(ns.EXE)
        pump(0.2)
        assert {"do": "rec_stop"} in frame()["commands"]
        call(f"/video?k={token}&type=video%2Fmp4", b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000)
        press(ns.SHIFT)
        press(ns.VAR)
        pump(0.3)
        r = frame()
        assert r["facing"] == "user" and r["rotate"] == 90, r
        files = sorted(os.listdir(folder))
        assert any(f.endswith(".jpg") for f in files) and any(f.endswith(".mp4") for f in files), files
        print("photo, video, camera avant, image pivotee :", files)
        ok = True
    finally:
        gateway.terminate()
        out, _ = gateway.communicate(timeout=5)
        if not ok:
            print(out[-2000:])
    print("OK : NumCam fonctionne avec un faux telephone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
