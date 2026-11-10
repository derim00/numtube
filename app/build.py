"""Builds the NumTube app for the current operating system.

    python build.py

Needs: pip install pyinstaller pillow numpy pyusb libusb-package imageio-ffmpeg yt-dlp certifi
Output: dist/NumTube.app (macOS), dist/NumTube.exe (Windows), dist/NumTube (Linux),
then zipped as dist/NumTube-<os>.zip. It embeds firmware built from
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
        sys.executable, "-m", "PyInstaller", "numtube_app.py",
        "--name", "NumTube", "--noconfirm", "--clean", "--windowed",
        "--icon", os.path.join("assets", "icon.png"),
        "--paths", os.path.join(ROOT, "gateway"),
        "--add-data", f"page.html{SEP}.",
        "--add-data", f"firmware{SEP}firmware",
        "--hidden-import", "numtube_gateway",
        "--hidden-import", "numworks_dfu",
        "--collect-submodules", "yt_dlp",
        "--collect-all", "libusb_package",
        "--collect-all", "imageio_ffmpeg",
        "--collect-data", "certifi",
    ]
    system = platform.system()
    if system != "Darwin":
        args.append("--onefile")  # a single file to send on Windows and Linux
    else:
        args += ["--osx-bundle-identifier", "fr.numtube.app"]
    subprocess.run(args, check=True)

    tag = {"Darwin": "mac", "Windows": "windows", "Linux": "linux"}[system]
    name = f"NumTube-{tag}-{platform.machine().lower()}"
    stage = os.path.join("dist", name)
    os.makedirs(stage)
    if system == "Darwin":
        # ditto keeps the symlinks and permissions inside the .app bundle.
        subprocess.run(["ditto", os.path.join("dist", "NumTube.app"),
                        os.path.join(stage, "NumTube.app")], check=True)
    else:
        binary = "NumTube.exe" if system == "Windows" else "NumTube"
        shutil.copy2(os.path.join("dist", binary), os.path.join(stage, binary))
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
