"""Builds the NumSocial app (installer + NumTube/NumChat gateway) for this OS.

    python build.py

Needs: pip install pyinstaller pillow numpy pyusb libusb-package imageio-ffmpeg yt-dlp certifi playwright mss paho-mqtt cryptography segno yt-dlp-ejs deno
Output: dist/NumSocial.app (macOS), dist/NumSocial/NumSocial.exe (Windows), dist/NumSocial (Linux),
then zipped as dist/NumSocial-<os>.zip. It embeds firmware built from
NumWorks code: keep it for yourself, share the repository instead.
"""
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEP = os.pathsep  # PyInstaller's --add-data separator


def main() -> int:
    os.chdir(HERE)
    for folder in ("build", "dist"):
        shutil.rmtree(folder, ignore_errors=True)
    firmware = [f for f in os.listdir("firmware") if f.endswith(".dfu")]
    if not firmware:
        print("firmware/*.dfu manquant")
        return 1

    args = [
        sys.executable, "-m", "PyInstaller", "numsocial_app.py",
        "--name", "NumSocial", "--noconfirm", "--clean", "--windowed",
        "--icon", os.path.join("assets", "icon.png"),
        "--paths", os.path.join(ROOT, "gateway"),
        "--add-data", f"page.html{SEP}.",
        "--add-data", f"firmware{SEP}firmware",
        "--hidden-import", "numtube_gateway",
        "--hidden-import", "numworks_dfu",
        "--hidden-import", "numchat_service",
        "--hidden-import", "numchat_snap",
        "--hidden-import", "numchat_media",
        "--hidden-import", "numscreen_service",
        "--hidden-import", "numscreen_input",
        "--hidden-import", "mss",
        "--hidden-import", "numblocks_service",
        "--hidden-import", "numcam_service",
        "--hidden-import", "segno",
        "--hidden-import", "paho.mqtt.client",
        # NumChat drives Chrome through Playwright's driver (no browser bundled:
        # it attaches to the Chrome already installed on the computer).
        "--collect-all", "playwright",
        "--collect-submodules", "yt_dlp",
        "--collect-all", "libusb_package",
        "--collect-all", "imageio_ffmpeg",
        "--collect-data", "certifi",
    ]
    # YouTube's challenges: yt-dlp's solver scripts, and the JavaScript runtime that runs them (deno, from
    # PyPI). Without them YouTube only gives throttled videos (a minute or more to start, or nothing).
    import deno

    args += ["--collect-all", "yt_dlp_ejs", "--add-binary", f"{deno.find_deno_bin()}{SEP}deno"]
    system = platform.system()
    # A folder rather than a single exe: the gateway runs yt-dlp as a copy of
    # the app, and a one-file build would unpack 100 MB on every call.
    if system == "Darwin":
        args += ["--osx-bundle-identifier", "fr.numsocial.app"]
    subprocess.run(args, check=True)

    tag = {"Darwin": "mac", "Windows": "windows", "Linux": "linux"}[system]
    name = f"NumSocial-{tag}-{platform.machine().lower()}"
    stage = os.path.join("dist", name)
    os.makedirs(stage)
    if system == "Darwin":
        # ditto keeps the symlinks and permissions inside the .app bundle.
        subprocess.run(["ditto", os.path.join("dist", "NumSocial.app"),
                        os.path.join(stage, "NumSocial.app")], check=True)
    else:
        shutil.copytree(os.path.join("dist", "NumSocial"), os.path.join(stage, "NumSocial"))
        if system == "Linux":
            shutil.copy2("installer-regle-usb.sh", stage)
    shutil.copy2("LISEZMOI.txt", stage)
    archive = os.path.join("dist", name + ".zip")
    if system == "Darwin":
        subprocess.run(["ditto", "-c", "-k", "--keepParent", stage, archive], check=True)
    else:
        shutil.make_archive(os.path.join("dist", name), "zip", "dist", name)
    print("OK:", archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
