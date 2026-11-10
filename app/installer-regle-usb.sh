#!/bin/sh
# Linux only: lets your user account talk to the NumWorks calculator over USB
# (both the "connected" mode used for installing and NumTube itself).
# Run once with: sudo ./installer-regle-usb.sh
set -e
cat > /etc/udev/rules.d/50-numtube.rules <<'RULES'
# NumWorks calculator: DFU ("connected") mode and NumTube
SUBSYSTEM=="usb", ATTR{idVendor}=="0483", ATTR{idProduct}=="a291", MODE="0666"
SUBSYSTEM=="usb", ATTR{idVendor}=="0483", ATTR{idProduct}=="a292", MODE="0666"
RULES
udevadm control --reload-rules
udevadm trigger
echo "C'est fait. Débranche puis rebranche la calculatrice."
