"""NumCam open, then NumTube connects (Hello) and searches: does the search answer? (The
calculator went to NumTube without the gateway hearing NumCam stop.)

Usage: ../.venv/bin/python3 cam_then_tube.py"""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw
import numscreen_service as ns
from fake_numchat import FakeCalculator
calc = FakeCalculator()
env = dict(os.environ, NUMCAM_PORT="9877", NUMCAM_DIR="/tmp/numcam-test")
g = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"), "--port", calc.slave_path],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
try:
    calc.send(gw.T_HELLO); calc.wait_for(gw.T_HELLO_ACK, 10)
    calc.send(ns.T_START, b"\x01")
    n = 0
    for k, p in calc.frames(3):
        if k == ns.T_FRAME and p[4] & 1:
            calc.send(ns.T_ACK, p[:2]); n += 1
    print("NumCam: images", n)
    # as the calculator does when leaving NumCam (Home) then opening NumTube: Stop... or not, then Hello
    calc.send(gw.T_HELLO)
    t = time.time()
    calc.wait_for(gw.T_HELLO_ACK, 10)
    print("HelloAck en", round(time.time() - t, 2), "s")
    calc.send(gw.T_SEARCH, "numworks".encode())
    t = time.time()
    calc.wait_for(gw.T_SEARCH_DONE, 60)
    print("recherche OK en", round(time.time() - t, 1), "s")
finally:
    g.terminate(); out, _ = g.communicate(timeout=5); print(out[-1500:])
