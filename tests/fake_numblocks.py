"""End-to-end test of NumBlocks multiplayer: two fake calculators, each with
its own gateway (as two computers would be), through a real public MQTT
broker, under a test topic prefix (never in the real list of games).

Alice opens her world, Bob finds it in the list and joins: the world, the
changed blocks, the players, positions, blocks both ways, chat, then Alice
closes the game and Bob is told.

Usage: ../.venv/bin/python3 fake_numblocks.py          (through the public broker)
       ../.venv/bin/python3 fake_numblocks.py --lan    (no Internet: the local network only)
"""
import os
import struct
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numtube_gateway as gw  # noqa: E402
import numblocks_service as nb  # noqa: E402
from fake_numchat import FakeCalculator  # noqa: E402


def gateway(calc: FakeCalculator, env: dict):
    return subprocess.Popen([sys.executable, os.path.join(HERE, "..", "gateway", "numtube_gateway.py"),
                             "--port", calc.slave_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)


def collect(calc, seconds, into):
    for frame in calc.frames(seconds):
        into.append(frame)
    return into


def find(frames, kind):
    return [p for k, p in frames if k == kind]


def main() -> int:
    lan = "--lan" in sys.argv
    env = dict(os.environ, NUMBLOCKS_TOPICS=f"numsocial/test/{uuid.uuid4().hex[:8]}")
    if not lan:
        env["NUMBLOCKS_NO_LAN"] = "1"
    if lan:
        env["NUMBLOCKS_OFFLINE"] = "1"
        env["NUMBLOCKS_LAN_PORT"] = str(47000 + os.getpid() % 900)   # (never a real game nearby)
    alice, bob = FakeCalculator(), FakeCalculator()
    ga, gb = gateway(alice, env), gateway(bob, env)
    ok = False
    try:
        for calc, name in ((alice, "Alice"), (bob, "Bob")):
            calc.send(gw.T_HELLO)
            calc.wait_for(gw.T_HELLO_ACK, 10)
            calc.send(nb.T_HELLO, bytes([1]) + nb.str8(name))
            online = calc.wait_for(nb.T_HELLO_OK, 20)
            assert online == b"\x01", "pas de broker joignable"
        print("deux calculatrices en ligne")

        # Alice opens her world, with two blocks she changed.
        alice.send(nb.T_HOST, struct.pack("<qBBI", 1234, 0, 0, 500) + nb.str8("Monde d'Alice"))
        alice.send(nb.T_DUMP, nb.ENTRY.pack(10, 64, -3, 1) + nb.ENTRY.pack(11, 64, -3, 4))
        alice.send(nb.T_DUMP, b"")
        gid, _ = nb.read_str8(alice.wait_for(nb.T_HOST_OK, 20), 0)
        assert gid, "la partie ne s'est pas ouverte"
        print("partie ouverte :", gid)

        # Bob lists the games and finds hers.
        bob.send(nb.T_LIST)
        frames = []
        bob.wait_for(nb.T_LIST_DONE, 15, frames)
        servers = [nb.read_str8(p, 1)[0] for p in find(frames, nb.T_SERVER)]
        assert gid in servers, f"partie absente de la liste: {servers}"
        print("dans la liste" + (" (reseau local)" if lan else " publique"), ":", len(servers), "partie(s)")

        bob.send(nb.T_JOIN, nb.str8(gid))
        frames = []
        bob.wait_for(nb.T_READY, 20, frames)
        world = find(frames, nb.T_WORLD)[0]
        seed, wtype, mode, gtime, pid = struct.unpack_from("<qBBIB", world)
        assert seed == 1234 and gtime == 500 and pid >= 1, (seed, gtime, pid)
        blocks = [e for p in find(frames, nb.T_BLOCKS) for e in nb.entries_of(p)]
        assert sorted(blocks) == [(10, 64, -3, 1), (11, 64, -3, 4)], blocks
        names = [nb.read_str8(p, 2)[0] for p in find(frames, nb.T_PLAYER)]
        assert "Alice" in names, names
        print("Bob a rejoint : monde, blocs et joueurs recus")

        frames = collect(alice, 2, [])
        assert any(nb.read_str8(p, 2)[0] == "Bob" for p in find(frames, nb.T_PLAYER)), "Alice ne voit pas Bob arriver"
        assert any(b"a rejoint" in p for p in find(frames, nb.T_CHAT))

        # Positions both ways.
        bob.send(nb.T_STATE, nb.STATE.pack(5.5, 70, 8.25, 90, 10, 1, 3))
        alice.send(nb.T_STATE, nb.STATE.pack(-2, 65, 1, 0, 0, 0, 0))
        frames = collect(alice, 2, [])
        seen = [p for p in find(frames, nb.T_PLAYERS) if p]
        last = seen[-1]
        states = {last[i]: nb.STATE.unpack_from(last, i + 1) for i in range(0, len(last), 1 + nb.STATE.size)}
        assert pid in states and abs(states[pid][0] - 5.5) < 1e-6 and 0 not in states, states
        frames = collect(bob, 1, [])
        last = [p for p in find(frames, nb.T_PLAYERS) if p][-1]
        ids = [last[i] for i in range(0, len(last), 1 + nb.STATE.size)]
        assert ids == [0], ids
        print("positions : chacun voit l'autre, pas lui-meme")

        # Blocks both ways, chat.
        bob.send(nb.T_BLOCK, nb.ENTRY.pack(1, 70, 2, 0))
        frames = collect(alice, 2, [])
        assert (1, 70, 2, 0) in [e for p in find(frames, nb.T_BLOCKS) for e in nb.entries_of(p)]
        alice.send(nb.T_BLOCK, nb.ENTRY.pack(3, 71, 4, 5))
        alice.send(nb.T_CHAT, "salut Bob".encode())
        frames = collect(bob, 2, [])
        assert (3, 71, 4, 5) in [e for p in find(frames, nb.T_BLOCKS) for e in nb.entries_of(p)]
        assert any(p == "<Alice> salut Bob".encode() for p in find(frames, nb.T_CHAT)), find(frames, nb.T_CHAT)
        print("blocs et chat dans les deux sens")

        # Alice closes the game.
        alice.send(nb.T_LEAVE)
        end = bob.wait_for(nb.T_END, 20)
        print("fin de partie pour Bob :", end[1:].decode())
        ok = True
    finally:
        for g in (ga, gb):
            g.terminate()
        out_a, _ = ga.communicate(timeout=5)
        out_b, _ = gb.communicate(timeout=5)
        if not ok:
            print("--- passerelle Alice\n" + out_a[-2000:] + "\n--- passerelle Bob\n" + out_b[-2000:])
    print("OK : multijoueur NumBlocks entre deux passerelles" + (", sans Internet" if lan else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
