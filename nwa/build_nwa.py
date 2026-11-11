"""Builds an external app (.nwa sources) for the NumSocial firmware, without
NumWorks' online tools: compiles it with EADK (from the Epsilon checkout),
links it at the addresses of our own userland, and writes a raw image to
flash at the start of the external apps area.

Usage: build_nwa.py --userland userland.B.elf --icon icon.png --out app.bin
                    [--meta app.json] [--cflags "..."] sources...
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EPSILON = os.path.join(HERE, "..", "firmware", "epsilon")
SHARED = os.path.join(HERE, "..", "firmware", "shared")
ARCH = ["-mcpu=cortex-m7", "-mthumb", "-mfloat-abi=hard", "-mfpu=fpv5-sp-d16"]


def symbols(elf: str) -> dict:
    out = subprocess.run(["arm-none-eabi-nm", elf], capture_output=True, text=True, check=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            table[parts[2]] = int(parts[0], 16)
    return table


def icon_source(png: str, path: str) -> None:
    """The Home screen icon: 55x56 RGB565, LZ4 compressed (like Escher's
    image inliner), over a white background."""
    import lz4.block
    from PIL import Image

    image = Image.open(png).convert("RGBA").resize((55, 56))
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    rgb = Image.alpha_composite(background, image).convert("RGB")
    pixels = bytearray()
    for r, g, b in (rgb.get_flattened_data() if hasattr(rgb, "get_flattened_data") else rgb.getdata()):
        value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        pixels += value.to_bytes(2, "little")
    data = lz4.block.compress(bytes(pixels), mode="high_compression", compression=12, store_size=False)
    with open(path, "w") as f:
        f.write("#include <stdint.h>\n")
        f.write('const uint8_t eadk_app_icon[%d] __attribute__((section(".rodata.eadk_app_icon"))) = {' % len(data))
        f.write(",".join(str(b) for b in data))
        f.write("};\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--userland", required=True)
    ap.add_argument("--icon", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cflags", default="")
    ap.add_argument("--meta", help="writes where the app goes (JSON), for the installer")
    ap.add_argument("sources", nargs="+")
    opts = ap.parse_args()

    sym = symbols(opts.userland)
    values = {
        "flashStart": sym["_external_apps_flash_start"],
        "flashLength": sym["_external_apps_flash_end"] - sym["_external_apps_flash_start"],
        "ramStart": sym["_external_apps_RAM_start"],
        "ramLength": sym["_external_apps_RAM_end"] - sym["_external_apps_RAM_start"],
        # The trampoline table starts at drawString, after the stack and reset entries.
        "trampolineStart": sym["InitialisationVector"] + 8,
    }
    with open(os.path.join(EPSILON, "eadk", "src", "linker.ld")) as f:
        script = re.sub(r"\{\{ (\w+) \}\}", lambda m: hex(values[m.group(1)]), f.read())

    work = tempfile.mkdtemp(prefix="nwa-")
    ld = os.path.join(work, "app.ld")
    with open(ld, "w") as f:
        f.write(script)
    icon_c = os.path.join(work, "icon.c")
    icon_source(opts.icon, icon_c)

    common = ARCH + ["-Os", "-ffunction-sections", "-fdata-sections", "-fno-common",
                     "-I", os.path.join(EPSILON, "eadk", "include", "eadk"),
                     "-I", SHARED, "-I", os.path.join(SHARED, "ion", "src", "device"),
                     "-I", os.path.join(SHARED, "ion", "include"),
                     "-I", os.path.join(SHARED, "ion", "src", "device", "shared")]
    common += ["-DPLATFORM_DEVICE=1"] + opts.cflags.split()
    eadk = [os.path.join(EPSILON, "eadk", "src", n) for n in ("platform.c", "svc.cpp", "trampoline.cpp")]
    objects = []
    for i, source in enumerate(eadk + [icon_c] + opts.sources):
        obj = os.path.join(work, f"{i}.o")
        if source.endswith((".cpp", ".cc")):
            cmd = ["arm-none-eabi-g++", *common, "-std=c++17", "-fno-exceptions", "-fno-rtti", "-c", source, "-o", obj]
        else:
            cmd = ["arm-none-eabi-gcc", *common, "-std=gnu11", "-c", source, "-o", obj]
        subprocess.run(cmd, check=True)
        objects.append(obj)

    elf = os.path.join(work, "app.elf")
    subprocess.run(["arm-none-eabi-gcc", *ARCH, "-nostartfiles", "--specs=nano.specs",
                    "-Wl,--gc-sections", "-Wl,-u,eadk_app_name", "-Wl,-u,eadk_app_icon",
                    "-Wl,-u,eadk_api_level", "-Wl,--defsym,_eadk_external_data_start=0",
                    "-Wl,--defsym,_eadk_external_data_size=0",
                    "-T", ld, *objects, "-lm", "-o", elf], check=True)
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", elf, opts.out], check=True)
    size = os.path.getsize(opts.out)
    sym_app = symbols(elf)
    ram = sym_app["_heap_start"] - values["ramStart"]
    print(f"{opts.out}: {size} octets, a ecrire a 0x{values['flashStart']:08x}")
    print(f"RAM statique {ram} octets sur {values['ramLength']} (reste {values['ramLength'] - ram} pour le tas)")
    if ram > values["ramLength"]:
        print("trop de RAM pour ce firmware")
        return 1
    if opts.meta:
        import json

        with open(opts.meta, "w") as f:
            json.dump({"address": values["flashStart"], "size": size, "end": values["flashStart"] + values["flashLength"]}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
