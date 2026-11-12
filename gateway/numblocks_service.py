"""NumBlocks multiplayer: the public list of games, and the game server.

The NumBlocks fork (github.com/derim00/numblocks-mp) talks to this service
through the NumSocial link. The player who opens their world ("Open to LAN")
makes their own computer the server of the game: it keeps the blocks changed
and the players, welcomes those who join and passes everything around.
Computers find each other through a public MQTT broker (no account, no
router setting): the list of open games is a set of retained messages. Games
are also announced on the local network (UDP broadcast) and played there over
TCP, straight between the computers: on the same Wi-Fi, no Internet needed.

Calculator <-> computer frames (little endian; str8 = length byte + UTF-8):
  0x90 HELLO      c->p  u8 protocol, str8 name
  0x91 HELLO_OK   p->c  u8 online
  0x92 LIST       c->p
  0x93 SERVER     p->c  u8 index, str8 id, str8 world, str8 host, u8 players, u8 max, u8 mode
  0x94 LIST_DONE  p->c  u8 count
  0x95 HOST       c->p  i64 seed, u8 type, u8 mode, u32 time, str8 world
  0x96 DUMP       c->p  the host's changed blocks: entries; none = the end
  0x97 HOST_OK    p->c  str8 id (empty: failed), then text
  0x98 JOIN       c->p  str8 id
  0x99 WORLD      p->c  i64 seed, u8 type, u8 mode, u32 time, u8 your id, f32 x y z, str8 world
  0x9A BLOCKS     p->c  entries (changed blocks)
  0x9B READY      p->c  all the blocks of the join were sent
  0x9C STATE      c->p  f32 x y z yaw pitch, u8 flags, u16 held
  0x9D PLAYERS    p->c  n x (u8 id, f32 x y z yaw pitch, u8 flags, u16 held): the others
  0x9E PLAYER     p->c  u8 id, u8 here, str8 name
  0x9F BLOCK      c->p  entries changed by this calculator
  0xA0 CHAT       both  UTF-8 (to the computer: what was typed; back: the line to show)
  0xA1 LEAVE      c->p
  0xA2 END        p->c  u8 reason, text
  0xA3 TIME       both  u32 game time (the host's, every few seconds)
An entry is i32 x, u8 y, i32 z, u16 block (11 bytes). Only the Overworld is
shared.
"""
from __future__ import annotations

import json
import os
import random
import string
import struct
import threading
import time

T_HELLO, T_HELLO_OK, T_LIST, T_SERVER, T_LIST_DONE = 0x90, 0x91, 0x92, 0x93, 0x94
T_HOST, T_DUMP, T_HOST_OK, T_JOIN, T_WORLD, T_BLOCKS, T_READY = 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0x9B
T_STATE, T_PLAYERS, T_PLAYER, T_BLOCK, T_CHAT, T_LEAVE, T_END, T_TIME = (
    0x9C, 0x9D, 0x9E, 0x9F, 0xA0, 0xA1, 0xA2, 0xA3)
T_FIRST, T_LAST = 0x90, 0xA3

PROTOCOL = 1
MAX_PLAYERS = 8
ENTRY = struct.Struct("<iBiH")
STATE = struct.Struct("<fffffBH")
ENTRIES_PER_FRAME = 300          # 3.3 KB, under the 4 KB frame limit
BROKERS = [("broker.emqx.io", 1883), ("broker.hivemq.com", 1883), ("test.mosquitto.org", 1883)]
if os.environ.get("NUMBLOCKS_OFFLINE"):   # (tests: as if the computer had no Internet)
    BROKERS = []
# Tests set their own prefix so that they never show in the real list.
PREFIX = os.environ.get("NUMBLOCKS_TOPICS", "numsocial/numblocks/1")
LOBBY_FRESH = 40                 # s: older lobby entries are games that died

END_HOST_LEFT, END_NOT_FOUND, END_FULL, END_NETWORK, END_KICKED = 1, 2, 3, 4, 5


def str8(text: str, limit: int = 40) -> bytes:
    data = text.encode("utf-8")[:limit]
    while data and (data[-1] & 0xC0) == 0x80:  # never half a character
        data = data[:-1]
    return bytes([len(data)]) + data


def read_str8(payload: bytes, at: int) -> tuple[str, int]:
    n = payload[at] if at < len(payload) else 0
    return payload[at + 1:at + 1 + n].decode("utf-8", "replace"), at + 1 + n


def entries_of(payload: bytes) -> list[tuple[int, int, int, int]]:
    return [ENTRY.unpack_from(payload, i) for i in range(0, len(payload) - len(payload) % ENTRY.size, ENTRY.size)]


def pack_entries(items) -> list[bytes]:
    items = list(items)
    return [b"".join(ENTRY.pack(*e) for e in items[i:i + ENTRIES_PER_FRAME])
            for i in range(0, len(items), ENTRIES_PER_FRAME)] or []


def clean_name(name: str) -> str:
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:16] or "Joueur"


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
class Broker:
    """One connection to a public broker, shared by the service."""

    def __init__(self, log) -> None:
        self.log = log
        self.client = None
        self.handlers: dict[str, callable] = {}
        self.lock = threading.Lock()
        self.connected = threading.Event()

    def ensure(self) -> bool:
        with self.lock:
            if self.client is not None and self.connected.is_set():
                return True
            import paho.mqtt.client as mqtt

            for host, port in BROKERS:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     client_id="nb-" + "".join(random.choices(string.ascii_lowercase, k=12)))
                client.on_message = self._message
                client.on_connect = self._connected
                client.on_disconnect = lambda *a: self.connected.clear()
                try:
                    client.connect(host, port, 20)
                except Exception as error:
                    self.log(f"numblocks: broker {host} injoignable ({error})")
                    continue
                client.loop_start()
                self.client = client
                if self.connected.wait(6):
                    self.log(f"numblocks: connecte a {host}")
                    return True
                client.loop_stop()
                self.client = None
            return False

    def _connected(self, client, userdata, flags, reason, properties=None) -> None:
        self.connected.set()
        for topic in list(self.handlers):
            client.subscribe(topic, qos=0)

    def _message(self, client, userdata, message) -> None:
        for pattern, handler in list(self.handlers.items()):
            if _matches(pattern, message.topic):
                try:
                    handler(message.topic, message.payload, message.retain)
                except Exception as error:  # a bad message must not kill the loop
                    self.log(f"numblocks: message ignore ({error})")

    def subscribe(self, topic: str, handler) -> None:
        self.handlers[topic] = handler
        if self.client is not None:
            self.client.subscribe(topic, qos=0)

    def unsubscribe(self, topic: str) -> None:
        self.handlers.pop(topic, None)
        if self.client is not None:
            self.client.unsubscribe(topic)

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self.client is not None:
            self.client.publish(topic, payload, qos=0, retain=retain)


def _matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


# ---------------------------------------------------------------------------
# The local network: the host's computer is a small hub, found by broadcast
# ---------------------------------------------------------------------------
LAN_PORT = int(os.environ.get("NUMBLOCKS_LAN_PORT", "47654"))   # UDP, the announcements
LAN_GROUP = "239.255.77.42"   # multicast: every gateway hears it, even several on one computer
LAN_FRESH = 6                                                   # s


def _lan_frame(topic: str, payload: bytes) -> bytes:
    t = topic.encode()
    return struct.pack("<HI", len(t), len(payload)) + t + payload


def _lan_read(sock, buf: bytearray):
    """Frames from a TCP stream: yields (topic, payload); returns when the peer is gone."""
    while True:
        while len(buf) >= 6:
            tl, pl = struct.unpack_from("<HI", buf)
            if len(buf) < 6 + tl + pl:
                break
            topic = bytes(buf[6:6 + tl]).decode("utf-8", "replace")
            payload = bytes(buf[6 + tl:6 + tl + pl])
            del buf[:6 + tl + pl]
            yield topic, payload
        try:
            data = sock.recv(65536)
        except OSError:
            return
        if not data:
            return
        buf += data


class LanHub:
    """The host's side: the players on the local network connect here over TCP; what the server
    publishes goes to all of them, what they publish comes to the server. The game is announced
    by UDP broadcast every 2 seconds."""

    def __init__(self, log) -> None:
        import socket

        self.log = log
        self.handlers: dict[str, callable] = {}
        self.clients: list = []
        self.lock = threading.Lock()
        self.announce: bytes | None = None
        self.stopped = threading.Event()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._beacon, daemon=True).start()

    def _accept(self) -> None:
        while not self.stopped.is_set():
            try:
                sock, addr = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(sock)
            threading.Thread(target=self._client, args=(sock,), daemon=True).start()

    def _client(self, sock) -> None:
        for topic, payload in _lan_read(sock, bytearray()):
            for pattern, handler in list(self.handlers.items()):
                if _matches(pattern, topic):
                    try:
                        handler(topic, payload, False)
                    except Exception as error:
                        self.log(f"numblocks: message LAN ignore ({error})")
        with self.lock:
            if sock in self.clients:
                self.clients.remove(sock)

    def _beacon(self) -> None:
        import socket

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        first = True
        while not self.stopped.wait(0.2 if first else 2):
            first = False
            if self.announce:
                for address in (LAN_GROUP, "255.255.255.255"):
                    try:
                        udp.sendto(self.announce, (address, LAN_PORT))
                    except OSError:
                        pass
        udp.close()

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if "/lobby/" in topic:
            if payload:
                info = json.loads(payload)
                info["gid"], info["port"] = topic.rsplit("/", 1)[-1], self.port
                self.announce = json.dumps(info).encode()
            else:
                self.announce = None
            return
        frame = _lan_frame(topic, payload)
        with self.lock:
            for sock in list(self.clients):
                try:
                    sock.sendall(frame)
                except OSError:
                    self.clients.remove(sock)

    def subscribe(self, topic: str, handler) -> None:
        self.handlers[topic] = handler

    def unsubscribe(self, topic: str) -> None:
        self.handlers.pop(topic, None)

    def close(self) -> None:
        self.stopped.set()
        try:
            self.server.close()
        except OSError:
            pass
        with self.lock:
            for sock in self.clients:
                try:
                    sock.close()
                except OSError:
                    pass
            self.clients.clear()


class LanClient:
    """A player's side, connected to a host's hub on the local network: the same publish/subscribe
    as the broker."""

    def __init__(self, log, address: str, port: int) -> None:
        import socket

        self.log = log
        self.handlers: dict[str, callable] = {}
        self.sock = socket.create_connection((address, port), timeout=5)
        self.sock.settimeout(None)
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for topic, payload in _lan_read(self.sock, bytearray()):
            for pattern, handler in list(self.handlers.items()):
                if _matches(pattern, topic):
                    try:
                        handler(topic, payload, False)
                    except Exception as error:
                        self.log(f"numblocks: message LAN ignore ({error})")

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        with self.lock:
            try:
                self.sock.sendall(_lan_frame(topic, payload))
            except OSError:
                pass

    def subscribe(self, topic: str, handler) -> None:
        self.handlers[topic] = handler

    def unsubscribe(self, topic: str) -> None:
        self.handlers.pop(topic, None)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Both:
    """The host publishes to the public broker (when it is there) and to the local network."""

    def __init__(self, broker: Broker, hub: LanHub) -> None:
        self.broker, self.hub = broker, hub

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self.broker.connected.is_set():
            self.broker.publish(topic, payload, retain)
        self.hub.publish(topic, payload, retain)

    def subscribe(self, topic: str, handler) -> None:
        self.broker.subscribe(topic, handler)
        self.hub.subscribe(topic, handler)

    def unsubscribe(self, topic: str) -> None:
        self.broker.unsubscribe(topic)
        self.hub.unsubscribe(topic)


class LanListener:
    """Hears the games announced on the local network."""

    def __init__(self, log) -> None:
        import socket

        self.log = log
        self.games: dict[str, tuple[dict, str, float]] = {}   # gid -> (info, address, when)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            self.sock.bind(("", LAN_PORT))
            membership = socket.inet_aton(LAN_GROUP) + socket.inet_aton("0.0.0.0")
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError as error:
            log(f"numblocks: parties LAN invisibles ({error})")
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                data, (address, _) = self.sock.recvfrom(4096)
                info = json.loads(data)
            except (OSError, ValueError):
                continue
            if info.get("v") == PROTOCOL and info.get("gid") and info.get("port"):
                # (the same game heard on 127.0.0.1 and the network: the network's address wins)
                old = self.games.get(info["gid"])
                if old and old[1] != address and address.startswith("127.") and time.time() - old[2] < LAN_FRESH:
                    continue
                self.games[info["gid"]] = (info, address, time.time())

    def fresh(self) -> dict[str, tuple[dict, str]]:
        now = time.time()
        return {gid: (info, address) for gid, (info, address, when) in self.games.items() if now - when < LAN_FRESH}


# ---------------------------------------------------------------------------
# The game server, on the host's computer
# ---------------------------------------------------------------------------
class Member:
    def __init__(self, pid: int, name: str, cid: str | None) -> None:
        self.pid, self.name, self.cid = pid, name, cid
        self.state = STATE.pack(0, 0, 0, 0, 0, 0, 0)
        self.seen = time.monotonic()


class Server:
    def __init__(self, service, link, name: str, world: dict) -> None:
        self.service, self.link, self.log = service, link, service.log
        self.hub = LanHub(service.log)
        self.broker = Both(service.broker, self.hub)   # (both: the public list, and the local network)
        self.gid = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        self.world = world            # seed, type, mode, time, name
        self.blocks: dict[tuple[int, int, int], int] = {}
        self.members: dict[int, Member] = {0: Member(0, name, None)}
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.topic = f"{PREFIX}/g/{self.gid}"

    # -- to everyone
    def _broadcast(self, kind: int, body: bytes, skip_pid: int = -1) -> None:
        self.broker.publish(f"{self.topic}/s", bytes([kind, skip_pid & 0xFF]) + body)
        if skip_pid != 0:
            self.link.send(kind, body)

    def _to(self, member: Member, kind: int, body: bytes) -> None:
        if member.cid is None:
            self.link.send(kind, body)
        else:
            self.broker.publish(f"{self.topic}/c/{member.cid}", bytes([kind]) + body)

    def start(self) -> None:
        self.broker.subscribe(f"{self.topic}/u", self._from_client)
        self._announce()
        threading.Thread(target=self._loop, daemon=True).start()

    def _announce(self, closing: bool = False) -> None:
        topic = f"{PREFIX}/lobby/{self.gid}"
        if closing:
            self.broker.publish(topic, b"", retain=True)
            return
        info = {"world": self.world["name"], "host": self.members[0].name, "players": len(self.members),
                "max": MAX_PLAYERS, "mode": self.world["mode"], "v": PROTOCOL, "t": int(time.time())}
        self.broker.publish(topic, json.dumps(info).encode(), retain=True)

    def _loop(self) -> None:
        last_announce = 0.0
        online = self.service.broker.connected.is_set()
        while not self.stopped.wait(0.15):
            now = time.monotonic()
            if self.service.broker.connected.is_set() and not online:
                last_announce = 0.0   # (the broker reached at last: in the public list now)
            online = self.service.broker.connected.is_set()
            with self.lock:
                for pid in [p for p, m in self.members.items() if m.cid and now - m.seen > 15]:
                    self._remove(pid, "a perdu la connexion")
                states = b"".join(bytes([m.pid]) + m.state for m in self.members.values())
            # Everyone gets everyone; each calculator skips its own id.
            self.broker.publish(f"{self.topic}/s", bytes([T_PLAYERS, 255]) + states)
            others = b"".join(bytes([m.pid]) + m.state for m in self.members.values() if m.pid != 0)
            try:
                self.link.send(T_PLAYERS, others)
            except OSError:
                self.stop()
                return
            if now - last_announce > 10:
                last_announce = now
                self._announce()

    def _remove(self, pid: int, why: str) -> None:
        member = self.members.pop(pid, None)
        if member is None:
            return
        self._broadcast(T_PLAYER, bytes([pid, 0]) + str8(member.name))
        self._broadcast(T_CHAT, f"{member.name} {why}".encode())
        self.log(f"numblocks: {member.name} {why}")

    # -- from the host's own calculator
    def local(self, kind: int, payload: bytes) -> None:
        with self.lock:
            if kind == T_STATE and len(payload) >= STATE.size:
                self.members[0].state = payload[:STATE.size]
            elif kind == T_BLOCK:
                self._blocks_changed(payload, 0)
            elif kind == T_CHAT:
                self._broadcast(T_CHAT, f"<{self.members[0].name}> ".encode() + payload[:100])
            elif kind == T_TIME and len(payload) >= 4:
                self.world["time"] = struct.unpack_from("<I", payload)[0]
                self.broker.publish(f"{self.topic}/s", bytes([T_TIME, 0]) + payload[:4])

    def _blocks_changed(self, payload: bytes, pid: int) -> None:
        fresh = []
        for x, y, z, b in entries_of(payload):
            if self.blocks.get((x, y, z)) != b:
                self.blocks[(x, y, z)] = b
                fresh.append((x, y, z, b))
        for frame in pack_entries(fresh):
            self._broadcast(T_BLOCKS, frame, skip_pid=pid)

    # -- from the players who joined
    def _from_client(self, topic: str, payload: bytes, retained: bool) -> None:
        if len(payload) < 2:
            return
        kind = payload[0]
        cid, at = read_str8(payload, 1)
        body = payload[at:]
        with self.lock:
            member = next((m for m in self.members.values() if m.cid == cid), None)
            if kind == T_JOIN:
                self._join(cid, clean_name(read_str8(body, 0)[0]), member)
                return
            if member is None:
                self.broker.publish(f"{self.topic}/c/{cid}", bytes([T_END, END_KICKED]) + "Tu n'es plus dans la partie".encode())
                return
            member.seen = time.monotonic()
            if kind == T_STATE and len(body) >= STATE.size:
                member.state = body[:STATE.size]
            elif kind == T_BLOCK:
                self._blocks_changed(body, member.pid)
            elif kind == T_CHAT:
                self._broadcast(T_CHAT, f"<{member.name}> ".encode() + body[:100])
            elif kind == T_LEAVE:
                self._remove(member.pid, "a quitte la partie")

    def _join(self, cid: str, name: str, member: Member | None) -> None:
        if member is None:
            if len(self.members) >= MAX_PLAYERS:
                self.broker.publish(f"{self.topic}/c/{cid}", bytes([T_END, END_FULL]) + "La partie est pleine".encode())
                return
            taken = {m.name for m in self.members.values()}
            base, k = name, 2
            while name in taken:
                name, k = f"{base[:13]}{k}", k + 1
            pid = next(p for p in range(1, 255) if p not in self.members)
            member = Member(pid, name, cid)
            self.members[pid] = member
        w = self.world
        host_state = STATE.unpack(self.members[0].state)
        spawn = host_state[:3] if any(host_state[:3]) else (0.0, 0.0, 0.0)
        self._to(member, T_WORLD, struct.pack("<qBBIBfff", w["seed"], w["type"], w["mode"], w["time"],
                                              member.pid, *spawn) + str8(w["name"]))
        for frame in pack_entries((x, y, z, b) for (x, y, z), b in self.blocks.items()):
            self._to(member, T_BLOCKS, frame)
        for m in self.members.values():
            if m.pid != member.pid:
                self._to(member, T_PLAYER, bytes([m.pid, 1]) + str8(m.name))
        self._to(member, T_READY, b"")
        self._broadcast(T_PLAYER, bytes([member.pid, 1]) + str8(member.name), skip_pid=member.pid)
        self._broadcast(T_CHAT, f"{member.name} a rejoint la partie".encode(), skip_pid=member.pid)
        self.log(f"numblocks: {member.name} a rejoint ({len(self.members)} joueurs)")
        self._announce()

    def stop(self) -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.broker.publish(f"{self.topic}/s", bytes([T_END, 255, END_HOST_LEFT]) + "L'hote a ferme la partie".encode())
        self._announce(closing=True)
        self.broker.unsubscribe(f"{self.topic}/u")
        time.sleep(0.2)   # (the last message out before the local players are cut off)
        self.hub.close()
        self.log("numblocks: partie fermee")


# ---------------------------------------------------------------------------
# A player who joined someone else's game
# ---------------------------------------------------------------------------
class Guest:
    def __init__(self, service, link, gid: str, name: str, net=None) -> None:
        self.service, self.link, self.log = service, link, service.log
        self.broker = net or service.broker   # (the public broker, or a LanClient)
        self.gid, self.name = gid, name
        self.cid = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        self.topic = f"{PREFIX}/g/{gid}"
        self.pid = -1
        self.heard = time.monotonic()
        self.stopped = threading.Event()

    def start(self) -> None:
        self.broker.subscribe(f"{self.topic}/s", self._shared)
        self.broker.subscribe(f"{self.topic}/c/{self.cid}", self._mine)
        self._up(T_JOIN, str8(self.name, 16))
        threading.Thread(target=self._watch, daemon=True).start()

    def _up(self, kind: int, body: bytes) -> None:
        self.broker.publish(f"{self.topic}/u", bytes([kind]) + str8(self.cid) + body)

    def _watch(self) -> None:
        joined_at = time.monotonic()
        while not self.stopped.wait(1):
            silent = time.monotonic() - self.heard
            if (self.pid < 0 and time.monotonic() - joined_at > 12) or silent > 15:
                self._end(END_NOT_FOUND if self.pid < 0 else END_NETWORK,
                          "Partie introuvable" if self.pid < 0 else "Plus de nouvelles de l'hote")

    def _end(self, reason: int, text: str) -> None:
        if self.stopped.is_set():
            return
        try:
            self.link.send(T_END, bytes([reason]) + text.encode())
        except OSError:
            pass
        self.stop(send_leave=False)

    def _mine(self, topic: str, payload: bytes, retained: bool) -> None:
        if not payload:
            return
        self.heard = time.monotonic()
        kind, body = payload[0], payload[1:]
        if kind == T_WORLD and len(body) >= 15:
            self.pid = body[14]   # after i64 seed, u8 type, u8 mode, u32 time
        if kind == T_END:
            self._end(body[0] if body else END_KICKED, body[1:].decode("utf-8", "replace"))
            return
        self.link.send(kind, body)

    def _shared(self, topic: str, payload: bytes, retained: bool) -> None:
        if len(payload) < 2 or self.pid < 0:
            return
        self.heard = time.monotonic()
        kind, skip, body = payload[0], payload[1], payload[2:]
        if skip == self.pid:
            return
        if kind == T_PLAYERS:
            size = 1 + STATE.size
            body = b"".join(body[i:i + size] for i in range(0, len(body) - size + 1, size) if body[i] != self.pid)
        elif kind == T_END:
            self._end(body[0] if body else END_HOST_LEFT, body[1:].decode("utf-8", "replace"))
            return
        self.link.send(kind, body)

    def local(self, kind: int, payload: bytes) -> None:
        if kind in (T_STATE, T_BLOCK, T_CHAT):
            self._up(kind, payload)
        elif kind == T_LEAVE:
            self.stop()

    def stop(self, send_leave: bool = True) -> None:
        if send_leave and not self.stopped.is_set():
            self._up(T_LEAVE, b"")
        self.stopped.set()
        self.broker.unsubscribe(f"{self.topic}/s")
        self.broker.unsubscribe(f"{self.topic}/c/{self.cid}")
        if isinstance(self.broker, LanClient):
            self.broker.close()


# ---------------------------------------------------------------------------
# The service, one per gateway
# ---------------------------------------------------------------------------
class Outbox:
    """Frames to the calculator go through here: a thread of their own writes them, so that a calculator
    busy (making its world) never holds up the gateway, and only the latest positions are kept."""

    def __init__(self, link) -> None:
        self.link = link
        self.queue: list[tuple[int, bytes]] = []
        self.cond = threading.Condition()
        self.closed = False
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, kind: int, payload: bytes = b"") -> None:
        with self.cond:
            if kind == T_PLAYERS:
                self.queue = [f for f in self.queue if f[0] != T_PLAYERS]
            if len(self.queue) > 400:   # (gone for good: drop the oldest)
                self.queue.pop(0)
            self.queue.append((kind, payload))
            self.cond.notify()

    def _run(self) -> None:
        while True:
            with self.cond:
                while not self.queue and not self.closed:
                    self.cond.wait()
                if self.closed:
                    return
                kind, payload = self.queue.pop(0)
            try:
                self.link.send(kind, payload)
            except OSError:
                with self.cond:
                    self.closed = True
                return

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()


class NumBlocksService:
    def __init__(self, log) -> None:
        self.log = log
        self.broker = Broker(log)
        self.lan = LanListener(log)
        if os.environ.get("NUMBLOCKS_NO_LAN"):   # (tests: only the public broker)
            self.lan.fresh = lambda: {}
        # (the public broker is reached in the background: the calculator never waits for it)
        threading.Thread(target=self.broker.ensure, daemon=True).start()
        self.outbox: Outbox | None = None
        self.link = None
        self.name = "Joueur"
        self.game: Server | Guest | None = None
        self.hosting: dict | None = None   # the HOST request, while the dump arrives
        self.dump: list = []

    def handle(self, link, kind: int, payload: bytes) -> None:
        if self.outbox is None or self.outbox.link is not link or self.outbox.closed:
            self.outbox = Outbox(link)
        self.link = link
        link = self.outbox
        if os.environ.get("NUMBLOCKS_DEBUG") and kind not in (T_STATE,):
            self.log(f"numblocks: recu 0x{kind:02x} ({len(payload)} octets)")
        if kind == T_HELLO:
            self.name = clean_name(read_str8(payload, 1)[0])
            # Always playable: the local network needs no Internet; the public list comes when reached.
            link.send(T_HELLO_OK, b"\x01")
            if not self.broker.connected.is_set():
                threading.Thread(target=self.broker.ensure, daemon=True).start()
        elif kind == T_LIST:
            threading.Thread(target=self._list, args=(link,), daemon=True).start()
        elif kind == T_HOST and len(payload) >= 15:
            self._stop_game()
            seed, wtype, mode, gtime = struct.unpack_from("<qBBI", payload)
            self.hosting = {"seed": seed, "type": wtype, "mode": mode, "time": gtime,
                            "name": read_str8(payload, 14)[0] or "Monde"}
            self.dump = []
        elif kind == T_DUMP and self.hosting is not None:
            if payload:
                self.dump += entries_of(payload)
            else:
                self._open_game(link)
        elif kind == T_JOIN:
            self._stop_game()
            gid = read_str8(payload, 0)[0]
            net = None
            lan = self.lan.fresh().get(gid)
            if lan:
                try:
                    net = LanClient(self.log, lan[1], int(lan[0]["port"]))
                except OSError as error:
                    self.log(f"numblocks: partie LAN injoignable ({error})")
            if net is None and not self.broker.ensure():
                link.send(T_END, bytes([END_NETWORK]) + "Pas d'Internet sur l'ordinateur".encode())
                return
            self.game = Guest(self, link, gid, self.name, net)
            self.game.start()
            self.log(f"numblocks: {self.name} rejoint la partie {gid}" + (" (LAN)" if net else ""))
        elif kind == T_LEAVE:
            self._stop_game()
        elif self.game is not None:
            self.game.local(kind, payload)

    def _open_game(self, link) -> None:
        world, self.hosting = self.hosting, None
        # (without Internet the game is on the local network only, and in the public list as soon as the
        # broker is reached: it is announced again every 10 s)
        if not self.broker.connected.is_set():
            threading.Thread(target=self.broker.ensure, daemon=True).start()
        server = Server(self, link, self.name, world)
        for x, y, z, b in self.dump:
            server.blocks[(x, y, z)] = b
        self.dump = []
        self.game = server
        server.start()
        link.send(T_HOST_OK, str8(server.gid))
        self.log(f"numblocks: partie {server.gid} ouverte ({world['name']}, {len(server.blocks)} blocs modifies)")

    def _list(self, link) -> None:
        found: dict[str, dict] = {}

        def entry(topic: str, payload: bytes, retained: bool) -> None:
            gid = topic.rsplit("/", 1)[-1]
            if not payload:
                found.pop(gid, None)
                return
            try:
                info = json.loads(payload)
            except ValueError:
                return
            if info.get("v") == PROTOCOL and time.time() - info.get("t", 0) < LOBBY_FRESH:
                found[gid] = info

        topic = f"{PREFIX}/lobby/+"
        self.broker.connected.wait(6)   # (just started: the broker is being reached)
        if self.broker.connected.is_set():
            self.broker.subscribe(topic, entry)
            time.sleep(1.5)   # retained entries arrive at once
            self.broker.unsubscribe(topic)
        else:
            time.sleep(1.5)   # (the local network's announcements, every 2 s)
        for gid, (info, address) in self.lan.fresh().items():
            info = dict(info)
            info["world"] = f"{str(info.get('world', '?'))[:18]} [LAN]"
            found[gid] = info
        mine = self.game.gid if isinstance(self.game, Server) else None
        games = [(gid, info) for gid, info in found.items() if gid != mine]
        games.sort(key=lambda g: -g[1].get("t", 0))
        for i, (gid, info) in enumerate(games[:20]):
            link.send(T_SERVER, bytes([i]) + str8(gid) + str8(str(info.get("world", "?")), 24)
                      + str8(str(info.get("host", "?")), 16)
                      + bytes([min(int(info.get("players", 1)), 255), min(int(info.get("max", MAX_PLAYERS)), 255),
                               1 if info.get("mode") else 0]))
        link.send(T_LIST_DONE, bytes([min(len(games), 20)]))

    def _stop_game(self) -> None:
        if self.game is not None:
            self.game.stop()
            self.game = None

    def detach(self, link) -> None:
        if self.link is link:
            self._stop_game()
            self.link = None
            if self.outbox is not None:
                self.outbox.close()
