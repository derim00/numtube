"""Mouse and keyboard injection for NumScreen, on macOS and Windows.

Only the standard system interfaces are used, through ctypes (no extra
package): CoreGraphics events on macOS, SendInput on Windows.

Keys are named with the strings below, or given as a single character:
  enter esc tab backspace delete space left up down right shift ctrl alt cmd
`cmd` is Command on macOS and Ctrl on Windows (the shortcut key).

macOS asks for two permissions, granted by the user in System Settings >
Privacy & Security: Screen Recording (to see the screen) and Accessibility
(to move the mouse and type).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
import time

NAMED_KEYS = ("enter", "esc", "tab", "backspace", "delete", "space",
              "left", "up", "down", "right", "shift", "ctrl", "alt", "cmd")


class Input:
    """Interface. Coordinates are in screen points (what the OS calls the
    screen size, before Retina scaling)."""

    name = "none"

    def input_allowed(self) -> bool:
        return True

    def capture_allowed(self) -> bool:
        return True

    def request_permissions(self) -> None:
        pass

    def screen_size(self) -> tuple[int, int]:
        return (1440, 900)

    def cursor(self) -> tuple[float, float]:
        return (0.0, 0.0)

    def key(self, key: str, down: bool, shift: bool = False) -> None:
        raise NotImplementedError

    def text(self, text: str) -> None:
        raise NotImplementedError

    def move(self, dx: float, dy: float) -> None:
        raise NotImplementedError

    def button(self, which: str, down: bool) -> None:
        raise NotImplementedError

    def scroll(self, lines: int) -> None:
        raise NotImplementedError


class DryInput(Input):
    """Records what would be done instead of doing it (tests, demo)."""

    name = "dry"

    def __init__(self, log=print) -> None:
        self.log = log
        self.events: list[tuple] = []
        self.x, self.y = 720.0, 450.0

    def _record(self, *event) -> None:
        self.events.append(event)
        self.log("  [entree] " + " ".join(str(e) for e in event))

    def cursor(self) -> tuple[float, float]:
        return (self.x, self.y)

    def key(self, key: str, down: bool, shift: bool = False) -> None:
        self._record("touche", key, "bas" if down else "haut", "maj" if shift else "")

    def text(self, text: str) -> None:
        self._record("texte", text)

    def move(self, dx: float, dy: float) -> None:
        w, h = self.screen_size()
        self.x = min(max(self.x + dx, 0), w - 1)
        self.y = min(max(self.y + dy, 0), h - 1)
        self.events.append(("souris", round(self.x), round(self.y)))

    def button(self, which: str, down: bool) -> None:
        self._record("bouton", which, "bas" if down else "haut")

    def scroll(self, lines: int) -> None:
        self._record("molette", lines)


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------
class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


# Virtual key codes of the US layout (Carbon kVK_*), for keys that do not
# depend on the keyboard layout, and for the number row.
_MAC_NAMED = {"enter": 36, "esc": 53, "tab": 48, "backspace": 51, "delete": 117,
              "space": 49, "left": 123, "up": 126, "down": 125, "right": 124,
              "shift": 56, "ctrl": 59, "alt": 58, "cmd": 55}
_MAC_DIGITS = {"1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
               "8": 28, "9": 25, "0": 29}
_MAC_US_LETTERS = dict(zip("asdfhgzxcvbqweryt", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17]))
_MAC_US_LETTERS.update({"o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
                        "k": 40, "n": 45, "m": 46})
_FLAG_SHIFT = 0x20000
_FLAG_CTRL = 0x40000
_FLAG_ALT = 0x80000
_FLAG_CMD = 0x100000
_MOD_FLAGS = {"shift": _FLAG_SHIFT, "ctrl": _FLAG_CTRL, "alt": _FLAG_ALT, "cmd": _FLAG_CMD}

_MOUSE_DOWN = {"left": 1, "right": 3}
_MOUSE_UP = {"left": 2, "right": 4}
_MOUSE_MOVED, _LEFT_DRAGGED, _RIGHT_DRAGGED = 5, 6, 7
_FIELD_CLICK_STATE, _FIELD_DELTA_X, _FIELD_DELTA_Y = 1, 4, 5


def _letter_keycodes_from_layout() -> dict[str, int]:
    """Key codes that type each letter with the current keyboard layout
    (AZERTY, QWERTZ...). Falls back to the US layout on any problem."""
    letters = dict(_MAC_US_LETTERS)
    try:
        carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        vp = ctypes.c_void_p
        carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = vp
        carbon.TISGetInputSourceProperty.restype = vp
        carbon.TISGetInputSourceProperty.argtypes = [vp, vp]
        carbon.LMGetKbdType.restype = ctypes.c_uint8
        carbon.UCKeyTranslate.restype = ctypes.c_int32
        carbon.UCKeyTranslate.argtypes = [
            vp, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16)]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFRelease.argtypes = [vp]
        prop = vp.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
        if not source:
            return letters
        data = carbon.TISGetInputSourceProperty(source, prop)
        if data:
            layout = cf.CFDataGetBytePtr(data)
            kbd = carbon.LMGetKbdType()
            found: dict[str, int] = {}
            for code in range(0, 51):
                dead = ctypes.c_uint32(0)
                length = ctypes.c_ulong(0)
                chars = (ctypes.c_uint16 * 4)()
                status = carbon.UCKeyTranslate(layout, code, 3, 0, kbd, 1, ctypes.byref(dead),
                                               4, ctypes.byref(length), chars)
                if status == 0 and length.value == 1:
                    ch = chr(chars[0])
                    if "a" <= ch <= "z" and ch not in found:
                        found[ch] = code
            if len(found) == 26:
                letters = found
        cf.CFRelease(source)
    except Exception:
        pass
    return letters


class MacInput(Input):
    name = "mac"

    def __init__(self) -> None:
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        vp = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.restype = vp
        cg.CGEventCreateKeyboardEvent.argtypes = [vp, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateMouseEvent.restype = vp
        cg.CGEventCreateMouseEvent.argtypes = [vp, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
        cg.CGEventCreateScrollWheelEvent2.restype = vp
        cg.CGEventCreateScrollWheelEvent2.argtypes = [vp, ctypes.c_uint32, ctypes.c_uint32,
                                                      ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
        cg.CGEventCreate.restype = vp
        cg.CGEventCreate.argtypes = [vp]
        cg.CGEventGetLocation.restype = _CGPoint
        cg.CGEventGetLocation.argtypes = [vp]
        cg.CGEventSetFlags.argtypes = [vp, ctypes.c_uint64]
        cg.CGEventSetIntegerValueField.argtypes = [vp, ctypes.c_uint32, ctypes.c_int64]
        cg.CGEventKeyboardSetUnicodeString.argtypes = [vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, vp]
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayBounds.restype = _CGRect
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGPreflightPostEventAccess.restype = ctypes.c_bool
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestPostEventAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [vp]
        self.cg, self.cf = cg, cf
        self.letters = _letter_keycodes_from_layout()
        self.mods: set[str] = set()  # modifier keys held down
        self.buttons: set[str] = set()
        self.last_click = (0.0, "", 0.0, 0.0, 0)  # time, button, x, y, count
        self.lock = threading.Lock()

    # -- permissions
    def input_allowed(self) -> bool:
        return bool(self.cg.CGPreflightPostEventAccess())

    def capture_allowed(self) -> bool:
        return bool(self.cg.CGPreflightScreenCaptureAccess())

    def request_permissions(self) -> None:
        # Each opens the system prompt once (macOS remembers the answer).
        if not self.capture_allowed():
            self.cg.CGRequestScreenCaptureAccess()
        if not self.input_allowed():
            self.cg.CGRequestPostEventAccess()

    # -- geometry
    def screen_size(self) -> tuple[int, int]:
        rect = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
        return (int(rect.size.width), int(rect.size.height))

    def cursor(self) -> tuple[float, float]:
        event = self.cg.CGEventCreate(None)
        point = self.cg.CGEventGetLocation(event)
        self.cf.CFRelease(event)
        return (point.x, point.y)

    def _post(self, event) -> None:
        self.cg.CGEventPost(0, event)  # kCGHIDEventTap
        self.cf.CFRelease(event)

    def _flags(self) -> int:
        flags = 0
        for mod in self.mods:
            flags |= _MOD_FLAGS[mod]
        return flags

    # -- keyboard
    def key(self, key: str, down: bool, shift: bool = False) -> None:
        with self.lock:
            text = None
            if key in _MAC_NAMED:
                code = _MAC_NAMED[key]
                if key in _MOD_FLAGS:
                    (self.mods.add if down else self.mods.discard)(key)
            elif key.lower() in self.letters:
                code = self.letters[key.lower()]
                text = key
            elif key in _MAC_DIGITS:
                code = _MAC_DIGITS[key]
                text = key
            else:
                if down:
                    self._type(key)
                return
            event = self.cg.CGEventCreateKeyboardEvent(None, code, down)
            flags = self._flags() | (_FLAG_SHIFT if shift else 0)
            self.cg.CGEventSetFlags(event, flags)
            if text is not None and not (self._flags() & (_FLAG_CMD | _FLAG_CTRL)):
                # The exact character, whatever the layout (AZERTY digits...).
                buf = (ctypes.c_uint16 * 1)(ord(text))
                self.cg.CGEventKeyboardSetUnicodeString(event, 1, buf)
            self._post(event)

    def _type(self, text: str) -> None:
        for ch in text:
            units = ch.encode("utf-16-le")
            count = len(units) // 2
            buf = (ctypes.c_uint16 * count).from_buffer_copy(units)
            for down in (True, False):
                event = self.cg.CGEventCreateKeyboardEvent(None, 0, down)
                self.cg.CGEventSetFlags(event, 0)
                self.cg.CGEventKeyboardSetUnicodeString(event, count, buf)
                self._post(event)

    def text(self, text: str) -> None:
        with self.lock:
            self._type(text)

    # -- mouse
    def move(self, dx: float, dy: float) -> None:
        with self.lock:
            x, y = self.cursor()
            w, h = self.screen_size()
            nx, ny = min(max(x + dx, 0), w - 1), min(max(y + dy, 0), h - 1)
            kind = _MOUSE_MOVED
            button = 0
            if "left" in self.buttons:
                kind = _LEFT_DRAGGED
            elif "right" in self.buttons:
                kind, button = _RIGHT_DRAGGED, 1
            event = self.cg.CGEventCreateMouseEvent(None, kind, _CGPoint(nx, ny), button)
            # Games that lock the pointer (Roblox camera) read these deltas.
            self.cg.CGEventSetIntegerValueField(event, _FIELD_DELTA_X, int(round(dx)))
            self.cg.CGEventSetIntegerValueField(event, _FIELD_DELTA_Y, int(round(dy)))
            self.cg.CGEventSetFlags(event, self._flags())
            self._post(event)

    def button(self, which: str, down: bool) -> None:
        with self.lock:
            x, y = self.cursor()
            now = time.monotonic()
            if down:
                last_time, last_button, lx, ly, count = self.last_click
                # Cocoa needs the click count to see a double click.
                close = abs(x - lx) < 6 and abs(y - ly) < 6
                count = count + 1 if (which == last_button and now - last_time < 0.45 and close) else 1
                self.last_click = (now, which, x, y, count)
                self.buttons.add(which)
            else:
                count = self.last_click[4] if self.last_click[1] == which else 1
                self.buttons.discard(which)
            kind = (_MOUSE_DOWN if down else _MOUSE_UP)[which]
            event = self.cg.CGEventCreateMouseEvent(None, kind, _CGPoint(x, y), 1 if which == "right" else 0)
            self.cg.CGEventSetIntegerValueField(event, _FIELD_CLICK_STATE, count)
            self.cg.CGEventSetFlags(event, self._flags())
            self._post(event)

    def scroll(self, lines: int) -> None:
        with self.lock:
            event = self.cg.CGEventCreateScrollWheelEvent2(None, 1, 1, lines, 0, 0)  # line units
            self._post(event)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    from ctypes import wintypes

    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

_WIN_NAMED = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
              "space": 0x20, "left": 0x25, "up": 0x26, "down": 0x28, "right": 0x27,
              "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "cmd": 0x11}
_WIN_EXTENDED = {0x2E, 0x25, 0x26, 0x27, 0x28}
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE, _KEYEVENTF_SCANCODE = 1, 2, 4, 8
_MOUSEEVENTF_MOVE, _MOUSEEVENTF_WHEEL = 0x0001, 0x0800
_WIN_BUTTON = {("left", True): 0x0002, ("left", False): 0x0004,
               ("right", True): 0x0008, ("right", False): 0x0010}


class WinInput(Input):
    name = "windows"

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            # Real pixels, the same ones the screen capture sees.
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass
        self.user32.VkKeyScanW.restype = ctypes.c_short
        self.user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        self.lock = threading.Lock()
        self.remainder = [0.0, 0.0]

    def screen_size(self) -> tuple[int, int]:
        return (self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1))

    def cursor(self) -> tuple[float, float]:
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return (float(point.x), float(point.y))

    def _send(self, *inputs) -> None:
        array = (_INPUT * len(inputs))(*inputs)
        self.user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))

    def _vk(self, vk: int, down: bool):
        scan = self.user32.MapVirtualKeyW(vk, 0)
        flags = 0 if down else _KEYEVENTF_KEYUP
        if vk in _WIN_EXTENDED:
            flags |= _KEYEVENTF_EXTENDEDKEY
        if scan:
            flags |= _KEYEVENTF_SCANCODE  # games read scan codes
        return _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))

    def _unicode(self, ch: str) -> list:
        out = []
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = units[i] | (units[i + 1] << 8)
            for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                out.append(_INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags))))
        return out

    def key(self, key: str, down: bool, shift: bool = False) -> None:
        with self.lock:
            if key in _WIN_NAMED:
                self._send(self._vk(_WIN_NAMED[key], down))
                return
            scan = self.user32.VkKeyScanW(key) if len(key) == 1 else -1
            if scan == -1 or (scan >> 8) & 0x6:  # no key, or needs Ctrl/Alt (AltGr)
                if down:
                    self._send(*self._unicode(key))
                return
            vk = scan & 0xFF
            need_shift = bool((scan >> 8) & 1) or shift
            events = []
            if need_shift and down:
                events.append(self._vk(0x10, True))
            events.append(self._vk(vk, down))
            if need_shift and down:
                # Release shift right away so a held letter does not keep it.
                events.append(self._vk(0x10, False))
            self._send(*events)

    def text(self, text: str) -> None:
        with self.lock:
            events = []
            for ch in text:
                events += self._unicode(ch)
            if events:
                self._send(*events)

    def move(self, dx: float, dy: float) -> None:
        with self.lock:
            # Relative motion, the way games with a locked pointer want it.
            self.remainder[0] += dx
            self.remainder[1] += dy
            ix, iy = int(self.remainder[0]), int(self.remainder[1])
            self.remainder[0] -= ix
            self.remainder[1] -= iy
            if ix or iy:
                self._send(_INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(dx=ix, dy=iy, dwFlags=_MOUSEEVENTF_MOVE))))

    def button(self, which: str, down: bool) -> None:
        with self.lock:
            self._send(_INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(dwFlags=_WIN_BUTTON[(which, down)]))))

    def scroll(self, lines: int) -> None:
        with self.lock:
            data = ctypes.c_uint32(lines * 120).value
            self._send(_INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(mouseData=data, dwFlags=_MOUSEEVENTF_WHEEL))))


def create(kind: str = "real", log=print) -> Input:
    if kind == "dry":
        return DryInput(log)
    if sys.platform == "darwin":
        return MacInput()
    if sys.platform == "win32":
        return WinInput()
    return DryInput(log)
