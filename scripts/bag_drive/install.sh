#!/bin/bash
# Install USB bag-drive auto-mounting on this machine. Run with sudo:
#   sudo ./install.sh [user]
# [user] is who should be able to write to the drives and eject them, from a
# terminal or from the desktop. It defaults to whoever ran sudo.
# Rerunning it upgrades an existing install.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $0" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRIVE_USER="${1:-${SUDO_USER:-swarm}}"
id "$DRIVE_USER" >/dev/null

install -m 0755 "$HERE/bag-drive" /usr/local/sbin/bag-drive
install -m 0644 "$HERE/99-bag-drive.rules" /etc/udev/rules.d/99-bag-drive.rules
install -m 0644 "$HERE/bag-drive@.service" /etc/systemd/system/bag-drive@.service
install -d -m 0755 /mnt/bag_drives
echo "BAG_DRIVE_USER=$DRIVE_USER" > /etc/default/bag-drive

# Let the drive user eject without a password, and nothing else.
SUDOERS_TMP="$(mktemp)"
echo "$DRIVE_USER ALL=(root) NOPASSWD: /usr/local/sbin/bag-drive eject *" > "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP" >/dev/null
install -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/bag-drive
rm -f "$SUDOERS_TMP"

# Let the drive user eject from the desktop's file manager without an admin
# password. Needs polkit 0.106+ (Ubuntu 23.10 and later), which reads .rules.
PK_VERSION="$(pkaction --version 2>/dev/null | awk '{print $NF}')"
if [ -z "$PK_VERSION" ] || [ "$PK_VERSION" = "0.105" ]; then
    echo "note: polkit ${PK_VERSION:-not found} has no .rules support; the desktop will ask for"
    echo "      an admin password to eject. bag-drive eject still works without one."
else
    install -d -m 0755 /etc/polkit-1/rules.d
    sed "s/@USER@/$DRIVE_USER/g" "$HERE/bag-drive.rules.in" > /etc/polkit-1/rules.d/50-bag-drive.rules
    chmod 0644 /etc/polkit-1/rules.d/50-bag-drive.rules
fi

# So any user can run "bag-drive status" without typing the sbin path.
ln -sf /usr/local/sbin/bag-drive /usr/local/bin/bag-drive

if ! command -v fsck.exfat >/dev/null; then
    echo "note: exfatprogs is not installed. exFAT drives still mount, but cannot be repaired here"
    echo "      if one is unplugged without ejecting. To add it: sudo apt install exfatprogs"
fi
systemctl daemon-reload
udevadm control --reload
# Pick up any USB drive that is already plugged in.
udevadm trigger --action=add --subsystem-match=block
udevadm settle || true
sleep 1

echo "installed for user '$DRIVE_USER'. Drives mount under /mnt/bag_drives."
echo "Drives that were already mounted show in the file manager after they are replugged."
/usr/local/sbin/bag-drive status
