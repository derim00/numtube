#!/usr/bin/env bash
# Builds the NumSocial firmware (NumTube, NumChat, NumScreen) and NumBlocks Multiplayer on YOUR computer,
# from NumWorks' official sources.
#
#   1. downloads Epsilon from github.com/numworks/epsilon at the pinned commit
#   2. applies patches/numsocial-epsilon.patch (Termia's USB link + NumTube + NumChat)
#   3. compiles the firmware for N0120 and N0115, slots A and B
#   4. puts them in app/firmware/, where the NumSocial app installer finds them
#   5. downloads NumBlocks Multiplayer (github.com/derim00/numblocks-multiplayer, GPL) at its pinned
#      commit and builds it for each image (an external app, linked at that image's addresses)
#
# Nothing from NumWorks is stored in this repository: their code is fetched
# from their own repository and only ever lives on your machine.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMMIT="$(cat "$ROOT/EPSILON_COMMIT")"
NB_COMMIT="$(cat "$ROOT/NUMBLOCKS_COMMIT")"
NB_SRC="$ROOT/numblocks"
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
  libusb-package imageio-ffmpeg yt-dlp certifi pyinstaller pillow playwright mss paho-mqtt cryptography segno

say "NumBlocks Multiplayer ($NB_COMMIT)"
if [ ! -d "$NB_SRC/.git" ]; then
  git clone --quiet https://github.com/derim00/numblocks-multiplayer.git "$NB_SRC"
fi
git -C "$NB_SRC" fetch --quiet origin "$NB_COMMIT" 2>/dev/null || true
git -C "$NB_SRC" checkout --quiet --force "$NB_COMMIT"
NB="$NB_SRC/games/numblocks/src"
NB_FILES=""
for f in main world edits render player phys inv entity mob tick gui hand save names command mp gen data_dims dims end plat_eadk; do
  NB_FILES="$NB_FILES $NB/$f.c"
done

say "4/4  Compilation du firmware (quelques minutes par image)"
mkdir -p "$ROOT/app/firmware"
for model in $MODELS; do
  for slot in A B; do
    echo "  -> $model, slot $slot"
    # (allow3rdparty: external apps, such as NumBlocks, show on the Home screen)
    PATH="$VENV/bin:$PATH" make -C "$SRC/epsilon" PLATFORM="$model" \
      PYTHON="$VENV/bin/python3" -j"$JOBS" "userland.allow3rdparty.$slot.dfu" >"$ROOT/.build-$model-$slot.log" 2>&1 \
      || { echo "Échec, voir .build-$model-$slot.log"; exit 1; }
    cp "$SRC/epsilon/output/release/$model/userland.allow3rdparty.$slot.dfu" "$ROOT/app/firmware/numsocial-$model-$slot.dfu"
    echo "     NumBlocks"
    # shellcheck disable=SC2086
    "$VENV/bin/python3" "$ROOT/nwa/build_nwa.py" \
      --userland "$SRC/epsilon/output/release/$model/userland.allow3rdparty.$slot.elf" --icon "$NB/icon.png" \
      --out "$ROOT/app/firmware/numblocks-$model-$slot.bin" --meta "$ROOT/app/firmware/numblocks-$model-$slot.json" \
      --cflags "-DNB_DIMS=1 -Oz -fno-math-errno -fsingle-precision-constant -ffast-math -fno-tree-loop-distribute-patterns -I$NB -w" \
      $NB_FILES >>"$ROOT/.build-$model-$slot.log" 2>&1 \
      || { echo "Échec de NumBlocks, voir .build-$model-$slot.log"; exit 1; }
  done
done
# NumWorks' DFU tool, used by the installer. Copied locally, never committed.
cp "$SRC/tools/device/dfu.py" "$ROOT/app/numworks_dfu.py"

say "Firmware prêt dans app/firmware/."
echo "Fabrique maintenant l'appli : $VENV/bin/python3 app/build.py"
