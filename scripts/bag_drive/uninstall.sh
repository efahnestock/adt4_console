#!/bin/bash
# Remove USB bag-drive auto-mounting. Run with sudo: sudo ./uninstall.sh
# Eject any mounted drives first (bag-drive status shows them).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $0" >&2
    exit 1
fi

if ls /run/bag_drives/*.json >/dev/null 2>&1; then
    echo "bag drives are still plugged in; eject them first (bag-drive eject --all):" >&2
    /usr/local/sbin/bag-drive status >&2
    exit 1
fi

rm -f /etc/udev/rules.d/99-bag-drive.rules /etc/systemd/system/bag-drive@.service \
      /etc/sudoers.d/bag-drive /etc/default/bag-drive \
      /usr/local/sbin/bag-drive /usr/local/bin/bag-drive \
      /etc/polkit-1/rules.d/50-bag-drive.rules
rmdir /mnt/bag_drives 2>/dev/null || true
systemctl daemon-reload
udevadm control --reload
echo "bag-drive removed"
