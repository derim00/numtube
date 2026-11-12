"""Installs NumSocial (NumTube, NumChat and/or NumScreen) on a calculator in "connected" (DFU) mode.

Uses NumWorks' own DFU code (numworks_dfu.py, from the Epsilon repository).
The firmware images are built once; the version string the official kernel
checks is patched in to match the calculator's official software.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import tempfile

import usb.core
import usb.util

import numworks_dfu as dfu

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FIRMWARE_DIR = os.path.join(HERE, "firmware")
NUMWORKS_VID, DFU_PID = 0x0483, 0xA291
SUPPORTED_MODELS = ("n0115", "n0120")
# Slot start addresses of the official kernel, whose header holds the version.
KERNEL_STARTS = {"A": 0x90000000, "B": 0x90400000}
KERNEL_HEADER_MAGIC = struct.pack("<I", 0xDEC00DF0)
USERLAND_HEADER_MAGIC = struct.pack("<I", 0xDEC0EDFE)
VERSION_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}$")
# Mask of the homebrew apps hidden on the Home screen, found by its magic
# (see firmware epsilon/apps/home/homebrew_apps.cpp).
HOMEBREW_MAGIC = b"NUMAPPS-HIDDEN1\0"
HOMEBREW_BITS = {"numtube": 1, "numchat": 2, "numscreen": 4}
# External apps, installed with the firmware in its slot's external apps area (see nwa/build_nwa.py).
EXTERNAL_APPS = {"numblocks": "NumBlocks"}
NAMES = {"numtube": "NumTube", "numchat": "NumChat", "numscreen": "NumScreen", **EXTERNAL_APPS}


def _usb_backend():
    try:
        import libusb_package

        return libusb_package.get_libusb1_backend()
    except Exception:
        return None


# Every USB lookup (including inside numworks_dfu) uses the bundled libusb.
_original_find = usb.core.find


def _find(*args, **kwargs):
    kwargs.setdefault("backend", _usb_backend())
    return _original_find(*args, **kwargs)


usb.core.find = _find

# Windows: WinUSB refuses SET_CONFIGURATION, even to the configuration the device already has (macOS
# and Linux accept it). NumWorks' DFU code always sets it; keep the current one when there is one.
_original_set_configuration = usb.core.Device.set_configuration


def _set_configuration(self, *args, **kwargs):
    try:
        if self.get_active_configuration() is not None and not args and not kwargs:
            return
    except usb.core.USBError:
        pass
    try:
        _original_set_configuration(self, *args, **kwargs)
    except (usb.core.USBError, NotImplementedError):
        if sys.platform != "win32":
            raise


usb.core.Device.set_configuration = _set_configuration


class InstallError(Exception):
    pass


_known: dict = {}   # the calculator last read: its USB address -> (model, slot)


def _release(device) -> None:
    """Closes our handle on the device. Windows lets one program at a time open a USB device: a
    handle left open makes it busy for everyone (the installer itself, NumWorks' website)."""
    try:
        usb.util.dispose_resources(device)
    except Exception:
        pass


def find_calculator():
    """Returns (device, info) for a calculator in DFU mode, or (None, None).

    Listing devices does not open them; reading the model and slot does, so that is done once per
    calculator plugged in, and the device is released right after."""
    devices = _find(idVendor=NUMWORKS_VID, idProduct=DFU_PID, find_all=True)
    devices = list(devices or [])
    if not devices:
        _known.clear()
        return None, None
    device = devices[0]
    key = (getattr(device, "bus", None), getattr(device, "address", None), device.bcdDevice)
    if key in _known:
        return device, dict(_known[key])
    try:
        model = dfu.get_model(device)
        active = dfu.get_active_slot(device)
    except Exception as error:  # permission problems surface here on Linux; on Windows, a busy device
        return device, {"error": str(error)}
    finally:
        _release(device)
    _known.clear()
    _known[key] = {"model": model, "active_slot": active}
    return device, {"model": model, "active_slot": active}


def forget_calculator() -> None:
    """(after an install: the calculator restarts, read it again when it comes back)"""
    _known.clear()


def _inactive_slot(active: str) -> str:
    if active == "A":
        return "B"
    if active == "B":
        return "A"
    raise InstallError(
        "La calculatrice est dans un état inattendu (slot « %s »). "
        "Fais un reset au dos puis rebranche-la." % active
    )


def read_official_version(active_slot: str) -> str | None:
    """Best effort: reads the official kernel header over DFU. Calculators that
    refuse to be read return None, and the user types the version instead."""
    try:
        dfu.init()
        address = KERNEL_STARTS[active_slot]
        dfu.set_address(address)
        dfu.abort_request()
        data = bytes(dfu.__dict__["__dev"].ctrl_transfer(0xA1, 2, 2, 0, 2048, 4000))
        dfu.abort_request()
    except Exception:
        return None
    finally:
        try:
            usb.util.dispose_resources(dfu.__dict__["__dev"])
        except Exception:
            pass
    index = data.find(KERNEL_HEADER_MAGIC)
    if index < 0:
        return None
    raw = data[index + 4:index + 12].split(b"\0")[0].decode("ascii", "ignore")
    return raw if VERSION_RE.match(raw) else None


def patched_image(model: str, slot: str, version: str, apps=("numtube", "numchat", "numscreen")) -> str:
    """Writes the firmware for this model and slot, with `version` as the
    expected official version and only `apps` shown, to a temporary .dfu file."""
    if not any(app in HOMEBREW_BITS or app in EXTERNAL_APPS for app in apps):
        raise InstallError("Choisis au moins une appli à installer.")
    apps = [app for app in apps if app in HOMEBREW_BITS]
    if not VERSION_RE.match(version) or len(version) > 7:
        raise InstallError("Version invalide : « %s » (exemple : 26.4.0)." % version)
    path = os.path.join(FIRMWARE_DIR, f"numsocial-{model}-{slot}.dfu")
    if not os.path.exists(path):
        raise InstallError(f"Pas de firmware pour le modèle {model}.")
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    index = data.find(USERLAND_HEADER_MAGIC)
    if index < 0:
        raise InstallError("Image de firmware illisible.")
    data[index + 4:index + 12] = version.encode("ascii").ljust(8, b"\0")
    mask_at = data.find(HOMEBREW_MAGIC)
    if mask_at < 0 or data.find(HOMEBREW_MAGIC, mask_at + 1) >= 0:
        raise InstallError("Image de firmware sans choix des applis (ancienne version ?).")
    hidden = sum(bit for app, bit in HOMEBREW_BITS.items() if app not in apps)
    mask_at += len(HOMEBREW_MAGIC)
    data[mask_at:mask_at + 4] = struct.pack("<I", hidden)
    data[-4:] = struct.pack("<I", dfu.compute_crc(bytes(data[:-4])))
    handle, out_path = tempfile.mkstemp(suffix=".dfu")
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    return out_path


def external_elements(model: str, slot: str, apps) -> list[dict]:
    """The DFU elements that put the chosen external apps in the slot's external apps area: the apps,
    one after the other on 64 KB sectors, then a word that is not an app (none of what was there
    before stays listed)."""
    import json

    elements, address, end = [], None, None
    for app in apps:
        if app not in EXTERNAL_APPS:
            continue
        base = os.path.join(FIRMWARE_DIR, f"{app}-{model}-{slot}")
        if not (os.path.exists(base + ".bin") and os.path.exists(base + ".json")):
            raise InstallError(f"{EXTERNAL_APPS[app]} n'est pas compilé pour le modèle {model}.")
        with open(base + ".json") as f:
            meta = json.load(f)
        if address is None:
            address, end = meta["address"], meta["end"]
        elif meta["address"] != address:
            # (each is linked for the start of the area: one external app at a time for now)
            raise InstallError("Une seule appli externe à la fois.")
        with open(base + ".bin", "rb") as f:
            data = f.read()
        elements.append({"addr": address, "data": data, "size": len(data)})
        address = (address + len(data) + 0xFFFF) & ~0xFFFF
    if address is not None and address + 4 <= end:
        elements.append({"addr": address, "data": b"\0\0\0\0", "size": 4})
    return elements


def _erase_then_write(elements: list[dict], log, progress) -> None:
    """Erases every page the elements cover, then writes them: two steps the page shows (the bar goes
    0-30 % erasing, 30-100 % writing). progress(percent, step)."""
    layout = dfu.get_memory_layout(dfu.__dict__["__dev"])
    pages: list[int] = []
    for elem in elements:
        addr, end = elem["addr"], elem["addr"] + elem["size"]
        while addr < end:
            segment = next((s for s in layout if s["addr"] <= addr <= s["last_addr"]), None)
            if segment is None:
                raise InstallError(f"Adresse 0x{addr:08x} hors de la mémoire de la calculatrice.")
            page = addr & ~(segment["page_size"] - 1)
            if not pages or pages[-1] != page:
                pages.append(page)
            addr = page + segment["page_size"]
    pages = sorted(set(pages))
    log(f"Effacement ({len(pages)} pages de 64 Ko)...")
    for i, page in enumerate(pages):
        progress(i * 30 // len(pages), "Effacement")
        dfu.page_erase(page)
    total = sum(e["size"] for e in elements) or 1
    log(f"Écriture ({total // 1024} Ko)...")
    done = [0]
    last = [-1]

    def report(base, offset, size):
        percent = 30 + (done[0] + offset) * 70 // total
        if percent != last[0]:
            last[0] = percent
            progress(min(percent, 99), "Écriture")

    for elem in elements:
        dfu.write_memory(elem["addr"], elem["data"], report, elem["addr"], elem["size"])
        done[0] += elem["size"]
    progress(100, "Terminé")


def install(version: str, log, progress, apps=("numtube", "numchat", "numscreen")) -> None:
    """Flashes the chosen apps into the inactive slot and starts them."""
    device, info = find_calculator()
    if device is None:
        raise InstallError("Calculatrice introuvable en mode « connectée ».")
    if "error" in info:
        raise InstallError("Accès USB refusé : " + info["error"])
    model = info["model"]
    if model not in SUPPORTED_MODELS:
        raise InstallError(
            f"Modèle {model.upper()} non pris en charge (N0115 et N0120 seulement)."
        )
    slot = _inactive_slot(info["active_slot"])
    names = " et ".join(NAMES[a] for a in apps if a in NAMES)
    log(f"Modèle {model.upper()}, firmware officiel dans le slot {info['active_slot']}, "
        f"{names} {'iront' if len(apps) > 1 else 'ira'} dans le slot {slot}.")
    image = patched_image(model, slot, version, apps)
    try:
        elements = dfu.read_dfu_file(image)
        if not elements:
            raise InstallError("Image de firmware invalide.")
        elements += external_elements(model, slot, apps)
        leave_address = elements[0]["addr"]
        dfu.init()
        _erase_then_write(elements, log, progress)
        log("Redémarrage de la calculatrice...")
        dfu.exit_dfu(leave_address)
    finally:
        os.unlink(image)
        try:
            _release(dfu.__dict__["__dev"])
        except Exception:
            pass
        forget_calculator()
