# bag-drive: USB bag drives on the robots

Mounts any USB drive plugged into a robot at `/mnt/bag_drives/<label or UUID>`,
with no desktop login needed. The console's Drives page (API.md §22) uses it over
ssh to list drives, copy bags onto them and eject them.

Install once per robot, as the robot's user (needs sudo). Rerun to upgrade:

    scp -r scripts/bag_drive swarm@<robot>:bag_drive_install
    ssh -t swarm@<robot> 'sudo ~/bag_drive_install/install.sh'

On the robot:

    bag-drive status            # drives, free space, link speed, where to write
    bag-drive status --json     # the same, for the console
    bag-drive eject NAME        # sync, unmount, power down (no password)
    bag-drive eject --all       # every bag drive
    bag-drive unmount NAME      # the same as eject

NAME is the name `status` shows, or the drive's label, UUID, mount path or
`/dev` partition; Tab completes commands and drive names in bash and zsh
(open a new shell after installing). `eject` gets root through sudo itself (the installer adds a
password-free rule for `bag-drive eject` only). It prints what it did and
exits 0 only once the drive is unmounted; an unknown name or a busy drive is
an error that says why, with the processes holding the drive open.

From the desktop: bag drives show in the file manager's sidebar with the usual
eject button, which works without an admin password (a polkit rule limited to
USB drives and the drive user).

Files: `bag-drive` (the helper, installed to `/usr/local/sbin`),
`99-bag-drive.rules` (udev: USB filesystems only, never internal disks),
`bag-drive@.service` (one unit per mounted partition, runs the helper's
`_hook-mount`/`_hook-unmount`), `bag-drive.rules.in` (the polkit rule; needs
Ubuntu 23.10 or later), `bag-drive.bash` and `_bag-drive` (Tab completion for
bash and zsh), `install.sh`, `uninstall.sh`.

A drive whose root folder is root-owned (ext4) gets a `bags/` folder the robot
user can write to; `write_dir` in the status says where to write. A link under
5 Gbit/s is reported as `slow_link`: use a USB-C port and a USB 3 cable.
