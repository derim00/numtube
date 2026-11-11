#!/usr/bin/env bash
# Builds the NumSocial firmware (NumTube + NumChat) on YOUR computer, from NumWorks' official sources.
#
#   1. downloads Epsilon from github.com/numworks/epsilon at the pinned commit
#   2. applies patches/numsocial-epsilon.patch (Termia's USB link + NumTube + NumChat)
#   3. compiles the firmware for N0120 and N0115, slots A and B
#   4. puts them in app/firmware/, where the NumSocial app installer finds them
#
# Nothing from NumWorks is stored in this repository: their code is fetched
# from their own repository and only ever lives on your machine.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMMIT="$(cat "$ROOT/EPSILON_COMMIT")"
SRC="$ROOT/firmware"
VENV="$ROOT/.venv"
MODELS="${MODELS:-n0120 n0115}"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

for tool in git make python3; do
  command -v "$tool" >/dev/null || { echo "Il manque « $tool »."; exit 1; }
done

say "1/4  Sources officielles d'Epsilon ($COMMIT)"
if [ ! -d "$SRC/.git" ]; then
  # Without blobs first: only the files of the pinned commit get downloaded.
  git clone --filter=blob:none --no-checkout https://github.com/numworks/epsilon.git "$SRC"
fi
git -C "$SRC" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$SRC" checkout --quiet --force "$COMMIT"
git -C "$SRC" clean -fdq -e output

say "2/4  Application du patch NumSocial"
git -C "$SRC" apply --whitespace=nowarn "$ROOT/patches/numsocial-epsilon.patch"

if ! command -v arm-none-eabi-gcc >/dev/null; then
  say "Le compilateur ARM est absent."
  echo "Installe le kit officiel de NumWorks, puis relance ce script :"
  echo "    (cd \"$SRC\" && chmod +x tools/setup.sh && tools/setup.sh)"
  exit 1
fi

say "3/4  Environnement Python de compilation"
[ -x "$VENV/bin/python3" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install lz4 pypng pyelftools pyusb stringcase numpy \
  libusb-package imageio-ffmpeg yt-dlp certifi pyinstaller pillow playwright mss

say "4/4  Compilation du firmware (quelques minutes par image)"
mkdir -p "$ROOT/app/firmware"
for model in $MODELS; do
  for slot in A B; do
    echo "  -> $model, slot $slot"
    PATH="$VENV/bin:$PATH" make -C "$SRC/epsilon" PLATFORM="$model" \
      PYTHON="$VENV/bin/python3" -j"$JOBS" "userland.$slot.dfu" >"$ROOT/.build-$model-$slot.log" 2>&1 \
      || { echo "Échec, voir .build-$model-$slot.log"; exit 1; }
    cp "$SRC/epsilon/output/release/$model/userland.$slot.dfu" "$ROOT/app/firmware/numsocial-$model-$slot.dfu"
  done
done
# NumWorks' DFU tool, used by the installer. Copied locally, never committed.
cp "$SRC/tools/device/dfu.py" "$ROOT/app/numworks_dfu.py"

say "Firmware prêt dans app/firmware/."
echo "Fabrique maintenant l'appli : $VENV/bin/python3 app/build.py"
