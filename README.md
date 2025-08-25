1. Execute the `prepare-image.sh [image]` script as `root` where `[image]` is the url to raspiOS.

2. Flash the `vcpi.img` to SD card. You can use Rpi Imager to set additional settings like wifi.

3. Boot Rpi on DHCP enabled network. The boot script should run `setup.sh` on first boot and reboot the system when its done.

4. Login using user: `pi` pass: `vcpi`

5. Debug the initial setup process with `journalctl -u firstboot`. The `setup.sh` should be automatically renamed to `setup.sh.done` if setup is successful.
