"""NumCam: the phone's camera on the calculator.

The phone opens a page that this computer serves on the local network, over
HTTPS (phones only give the camera to secure pages; the certificate is made
here, so the phone asks once to trust it). The page sends small pictures of
what the camera sees, about 15 a second; the calculator shows them (through
NumScreen's client, see numscreen_service.py). Photos and videos are taken by
the phone at its own resolution and saved on this computer, in Pictures/NumCam.

A secret code in the address keeps the others on the Wi-Fi away from it.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import random
import socket
import ssl
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("NUMCAM_PORT", "8738"))
MAX_UPLOAD = 1_000_000_000   # a long video


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / "NumSocial"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_dir() -> Path:
    path = Path(os.environ.get("NUMCAM_DIR", Path.home() / "Pictures" / "NumCam"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def lan_address() -> str:
    """This computer's address on the Wi-Fi (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def certificate(address: str) -> tuple[str, str]:
    """A self-signed certificate for this address, made once (again if the address changes)."""
    folder = data_dir()
    cert, key = folder / "numcam-cert.pem", folder / "numcam-key.pem"
    stamp = folder / "numcam-cert.address"
    if cert.exists() and key.exists() and stamp.exists() and stamp.read_text() == address:
        return str(cert), str(key)
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NumSocial NumCam")])
    now = datetime.datetime.now(datetime.timezone.utc)
    alt = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        alt.append(x509.IPAddress(ipaddress.ip_address(address)))
    except ValueError:
        pass
    built = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=3650))
             .add_extension(x509.SubjectAlternativeName(alt), critical=False)
             .sign(private, hashes.SHA256()))
    cert.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    stamp.write_text(address)
    return str(cert), str(key)


class Phone:
    """What the phone sends, and what it is asked to do."""

    def __init__(self, log) -> None:
        self.log = log
        self.token = self._token()
        self.lock = threading.Lock()
        self.frame: bytes | None = None
        self.frame_time = 0.0
        self.commands: list[dict] = []
        self.rotate = 0
        self.facing = "environment"
        self.saved: list[str] = []
        self.on_saved = None        # callback(kind, path)
        self.address = lan_address()
        self.port = PORT
        self.server = None
        self.frames_in = 0   # (pictures received: the journal says how many a second)
        self.enc_ms = self.rtt_ms = 0   # (and how long the phone takes to make one, and to hear back)

    @staticmethod
    def _token() -> str:
        """The secret code of the address, kept: the phone's bookmark keeps working."""
        path = data_dir() / "numcam-code"
        try:
            code = path.read_text().strip()
            if len(code) == 6 and code.isalnum():
                return code
        except OSError:
            pass
        code = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        try:
            path.write_text(code)
        except OSError:
            pass
        return code

    @property
    def connected(self) -> bool:
        return time.monotonic() - self.frame_time < 4

    @property
    def url(self) -> str:
        return f"https://{self.address}:{self.port}/?k={self.token}"

    def command(self, **cmd) -> None:
        with self.lock:
            self.commands.append(cmd)

    def flip(self) -> None:
        self.facing = "user" if self.facing == "environment" else "environment"

    def turn(self) -> None:
        self.rotate = (self.rotate + 90) % 360

    def take_commands(self) -> list[dict]:
        with self.lock:
            out, self.commands = self.commands, []
        return out

    def save(self, kind: str, data: bytes, ext: str) -> str:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S")
        path = media_dir() / f"NumCam {stamp}{'' if kind == 'photo' else ' video'}.{ext}"
        k = 2
        while path.exists():
            path = path.with_name(f"{path.stem} ({k}){path.suffix}")
            k += 1
        path.write_bytes(data)
        self.saved.append(str(path))
        self.log(f"numcam: {'photo' if kind == 'photo' else 'video'} enregistree : {path}")
        if self.on_saved:
            self.on_saved(kind, str(path))
        return str(path)

    # -- the HTTPS server
    def start(self) -> bool:
        if self.server is not None:
            return True
        cert, key = certificate(self.address)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        phone = self

        class Handler(BaseHTTPRequestHandler):
            # (one connection for every picture: no new TLS handshake 15 times a second)
            protocol_version = "HTTP/1.1"
            timeout = 20   # (a connection that says nothing is dropped, never waited for)

            def log_message(self, *args) -> None:
                pass

            def setup(self) -> None:
                # (small answers go out at once: no waiting for the phone's acknowledgement)
                try:
                    self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                super().setup()
                # The TLS handshake, here in this connection's own thread (see start below).
                try:
                    self.request.do_handshake()
                except (OSError, ssl.SSLError):
                    self.close_connection = True
                    raise

            def _allowed(self) -> bool:
                from urllib.parse import parse_qs, urlparse

                ok = parse_qs(urlparse(self.path).query).get("k", [""])[0] == phone.token
                if not ok:
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("Code manquant : ouvre l'adresse complete affichee par NumSocial.".encode())
                return ok

            def _send(self, code: int, body: bytes, kind: str) -> None:
                # (the headers and the body in one write: one TLS record, one packet)
                self.send_response_only(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                head = b"".join(self._headers_buffer) + b"\r\n"
                self._headers_buffer = []
                self.wfile.write(head + body)
                self.wfile.flush()

            def do_GET(self) -> None:
                if not self._allowed():
                    return
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")

            def do_POST(self) -> None:
                if not self._allowed():
                    return
                from urllib.parse import parse_qs, urlparse

                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_UPLOAD:
                    self._send(413, b"", "text/plain")
                    return
                body = self.rfile.read(length) if length else b""
                if url.path == "/frame":
                    query = parse_qs(url.query)
                    try:
                        phone.enc_ms += int(query.get("enc", ["0"])[0])
                        phone.rtt_ms += int(query.get("rtt", ["0"])[0])
                    except ValueError:
                        pass
                    if "flip" in query:
                        phone.flip()
                    if "turn" in query:
                        phone.turn()
                    if body:
                        with phone.lock:
                            if not phone.connected:
                                phone.log("numcam: telephone connecte")
                            phone.frame, phone.frame_time = body, time.monotonic()
                            phone.frames_in += 1
                    reply = {"commands": phone.take_commands(), "rotate": phone.rotate, "facing": phone.facing}
                    self._send(200, json.dumps(reply).encode(), "application/json")
                elif url.path == "/photo":
                    phone.save("photo", body, "jpg")
                    self._send(200, b"{}", "application/json")
                elif url.path == "/video":
                    kind = parse_qs(url.query).get("type", ["webm"])[0]
                    phone.save("video", body, "mp4" if "mp4" in kind else "webm")
                    self._send(200, b"{}", "application/json")
                else:
                    self._send(404, b"", "text/plain")

        for port in (PORT, PORT + 1, PORT + 2, 0):
            try:
                server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
                break
            except OSError:
                continue
        # do_handshake_on_connect=False: otherwise the handshake happens in accept(), in the server's
        # only loop, and one connection that never speaks (browsers open spare ones) blocks them all.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        server.daemon_threads = True
        self.port = server.server_address[1]
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.log(f"numcam: sur le telephone, ouvre {self.url}")
        return True

    def qr_svg(self) -> str:
        import segno

        out = io.BytesIO()
        segno.make(self.url, error="m").save(out, kind="svg", scale=5, border=2, dark="#111", light="#fff")
        return out.getvalue().decode()


_PHONE: Phone | None = None
_LOCK = threading.Lock()


def phone(log=print) -> Phone:
    """The one phone server of this computer (started on first use)."""
    global _PHONE
    with _LOCK:
        if _PHONE is None:
            _PHONE = Phone(log)
            try:
                _PHONE.start()
            except Exception as error:
                log(f"numcam: serveur pour le telephone impossible ({error})")
        return _PHONE


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>NumCam</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 16px -apple-system, system-ui, sans-serif; background: #000; color: #eee; text-align: center; }
  video { width: 100%; max-height: 62vh; background: #111; object-fit: contain; }
  .bar { display: flex; gap: 10px; justify-content: center; padding: 12px; flex-wrap: wrap; }
  button { font: inherit; padding: 12px 18px; border-radius: 12px; border: 0; background: #2e7df6; color: #fff; }
  button.rec { background: #e94b3c; }
  #state { padding: 6px 16px; color: #bbb; }
  #go { margin-top: 30vh; font-size: 20px; }
  .hint { color: #888; font-size: 14px; padding: 0 20px 20px; }
</style></head><body>
<button id="go">Ouvrir la caméra</button>
<div id="app" hidden>
  <video id="v" playsinline muted autoplay></video>
  <div id="state">Connexion à la calculatrice…</div>
  <div class="bar">
    <button id="shot">Photo</button><button id="rec">Vidéo</button>
    <button id="flip">Avant / arrière</button><button id="turn">Pivoter</button>
  </div>
  <div class="hint">Laisse cette page ouverte et l'écran allumé. La calculatrice déclenche avec OK (photo)
    et EXE (vidéo). Photos et vidéos vont dans Images/NumCam sur l'ordinateur.</div>
</div>
<script>
const key = new URLSearchParams(location.search).get("k");
const $ = (id) => document.getElementById(id);
const video = $("v"), small = document.createElement("canvas");
let stream = null, facing = "environment", rotate = 0, recorder = null, chunks = [], recStart = 0, busy = false;

async function open() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  const video_ = { facingMode: { ideal: facing }, width: { ideal: 1920 }, height: { ideal: 1080 } };
  try { stream = await navigator.mediaDevices.getUserMedia({ video: video_, audio: true }); }
  catch (e) { stream = await navigator.mediaDevices.getUserMedia({ video: video_ }); }
  video.srcObject = stream;
  await video.play();
}

/* the camera's picture, turned (0, 90, 180, 270), on a canvas at most `size` wide or high */
function draw(canvas, size) {
  const w = video.videoWidth, h = video.videoHeight;
  if (!w || !h) return false;
  const side = rotate % 180 !== 0;
  const W = side ? h : w, H = side ? w : h, s = Math.min(1, size / Math.max(W, H));
  canvas.width = Math.round(W * s), canvas.height = Math.round(H * s);
  const c = canvas.getContext("2d");
  c.save();
  c.translate(canvas.width / 2, canvas.height / 2);
  c.rotate(rotate * Math.PI / 180);
  c.drawImage(video, -w * s / 2, -h * s / 2, w * s, h * s);
  c.restore();
  return true;
}
const blob = (canvas, q) => new Promise((ok) => canvas.toBlob(ok, "image/jpeg", q));
/* (a request that hangs is given up after 4 s for pictures; photos and videos have all the time they need) */
const post = (path, body, extra = "", ms = 0) => {
  const options = { method: "POST", body };
  if (ms && window.AbortSignal && AbortSignal.timeout) options.signal = AbortSignal.timeout(ms);
  return fetch(`${path}?k=${key}${extra}`, options);
};

async function photo() {
  const big = document.createElement("canvas");
  if (!draw(big, 4096)) return;
  $("state").textContent = "Photo…";
  await post("/photo", await blob(big, 0.92));
  $("state").textContent = "Photo enregistrée sur l'ordinateur";
}
function recording(on) {
  if (on && !recorder) {
    const type = ["video/mp4", "video/webm;codecs=vp9", "video/webm"].find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t));
    if (!type) { $("state").textContent = "Ce navigateur ne sait pas filmer"; return; }
    chunks = [];
    recorder = new MediaRecorder(stream, { mimeType: type });
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = async () => {
      $("state").textContent = "Envoi de la vidéo…";
      await post("/video", new Blob(chunks, { type }), `&type=${encodeURIComponent(type)}`);
      $("state").textContent = "Vidéo enregistrée sur l'ordinateur";
    };
    recorder.start(1000);
    recStart = Date.now();
    $("rec").classList.add("rec");
  } else if (!on && recorder) {
    recorder.stop();
    recorder = null;
    $("rec").classList.remove("rec");
  }
}
async function obey(cmd) {
  if (cmd.do === "photo") await photo();
  if (cmd.do === "rec_start") recording(true);
  if (cmd.do === "rec_stop") recording(false);
}

/* About 15 small pictures a second to the computer, up to 3 on their way at once (waiting for each
   answer made it a few a second over Wi-Fi); each answer says what the calculator asked. The page
   tells the computer how long making a picture (enc) and an answer (rtt) take. */
let inflight = 0, lastEnc = 0, lastRtt = 0, failures = 0;
async function sendOne() {
  const t0 = performance.now();
  if (!draw(small, 320)) return;
  const jpg = await blob(small, 0.55);
  const t1 = performance.now();
  lastEnc = Math.round(t1 - t0);
  try {
    const r = await (await post("/frame", jpg, `&enc=${lastEnc}&rtt=${lastRtt}`, 4000)).json();
    lastRtt = Math.round(performance.now() - t1);
    failures = 0;
    if (r.rotate !== rotate) rotate = r.rotate;
    if (r.facing !== facing) { facing = r.facing; recording(false); await open(); }
    for (const c of r.commands) await obey(c);
    if (!recorder) $("state").textContent = "Connecté : la calculatrice voit la caméra";
    else $("state").textContent = `● Vidéo ${Math.round((Date.now() - recStart) / 1000)} s`;
  } catch (e) {
    if (++failures > 3) $("state").textContent = "L'ordinateur ne répond pas (NumSocial est-il ouvert ?)";
  }
}
async function loop() {
  for (;;) {
    if (inflight < 3) {
      inflight++;
      sendOne().finally(() => inflight--);
    }
    await new Promise((ok) => setTimeout(ok, 66));
  }
}

$("go").onclick = async () => {
  try { await open(); }
  catch (e) { $("go").textContent = "Caméra refusée : autorise-la dans les réglages du navigateur, puis recharge"; return; }
  $("go").hidden = true, $("app").hidden = false;
  try { await navigator.wakeLock?.request("screen"); } catch (e) {}
  loop();
};
$("shot").onclick = photo;
$("rec").onclick = () => recording(!recorder);
$("flip").onclick = () => post("/frame", new Blob(), "&flip=1");
$("turn").onclick = () => post("/frame", new Blob(), "&turn=1");
</script></body></html>
"""
