# bag-drive: USB bag drives on the robots

Mounts any USB drive plugged into a robot at `/mnt/bag_drives/<label or UUID>`,
with no desktop login needed. The console's Drives page (API.md §22) uses it over
ssh to list drives, copy bags onto them and eject them.

Install once per robot, as the robot's user (needs sudo):

    scp -r scripts/bag_drive swarm@<robot>:bag_drive_install
    ssh -t swarm@<robot> 'sudo ~/bag_drive_install/install.sh'

On the robot:

    bag-drive status            # drives, free space, link speed, where to write
    bag-drive status --json     # the same, for the console
    sudo bag-drive eject NAME   # sync, unmount, power down (no password)

Files: `bag-drive` (the helper, installed to `/usr/local/sbin`),
`99-bag-drive.rules` (udev: USB filesystems only, never internal disks),
`bag-drive@.service` (one unit per mounted partition), `install.sh`,
`uninstall.sh`.

A drive whose root folder is root-owned (ext4) gets a `bags/` folder the robot
user can write to; `write_dir` in the status says where to write. A link under
5 Gbit/s is reported as `slow_link`: use a USB-C port and a USB 3 cable.
