# Raspberry Pi Image Build Tools

This folder contains tooling only for building and provisioning a Raspberry Pi
OS image that auto-starts vcpi.

## Files

- `prepare-image.sh` - image download, mount, provisioning, and packing
- `setup.sh` - first boot provisioning script executed on the Pi
- `services/firstboot.service` - runs `setup.sh` once
- `services/payload.service` - starts vcpi daemon on boot
- `services/journald-console.conf` - mirrors journal logs to root console (tty1)
- `userconf.txt.dist` - template for Raspberry Pi credentials
- `wpa_supplicant.conf.dist` - template Wi-Fi config

## Quick Build

From repository root:

```bash
cp rpi-build/wpa_supplicant.conf.dist rpi-build/wpa_supplicant.conf
cp rpi-build/userconf.txt.dist rpi-build/userconf.txt

# Replace password hash with your own SHA-512 hash
HASH=$(openssl passwd -6 'your-strong-password')
printf 'pi:%s\n' "$HASH" > rpi-build/userconf.txt

sudo ./rpi-build/prepare-image.sh <raspios-image-url>
```

From inside this folder:

```bash
sudo ./prepare-image.sh <raspios-image-url>
```

Output image path:

```text
rpi-build/vcpi.img
```

The local secrets/config files (`userconf.txt`, `wpa_supplicant.conf`) are kept
inside `rpi-build/` and are gitignored.

## Script Options

```bash
# Force re-download of cached source archive
sudo ./rpi-build/prepare-image.sh --refresh <raspios-image-url>

# Custom cache directory
sudo IMAGE_CACHE_DIR=/var/cache/vcpi ./rpi-build/prepare-image.sh <raspios-image-url>

# Custom credentials or Wi-Fi file paths
sudo USERCONF_PATH=/secure/userconf.txt WPA_CONF_PATH=/secure/wpa_supplicant.conf \
  ./rpi-build/prepare-image.sh <raspios-image-url>
```

## On First Boot

- `firstboot.service` runs `/root/setup.sh`
- vcpi is installed under `/home/pi/vcpi`
- Python packages are under `/home/pi/vcpi/core` and `/home/pi/vcpi/controllers`
- Python venv is created at `/home/pi/vcpi/venv`
- `payload.service` sets `LOG_LEVEL=DEBUG`
- journald forwards logs to `/dev/tty1` at `debug` level
- `payload.service` starts:

```text
/home/pi/vcpi/venv/bin/python /home/pi/vcpi/main.py serve --sock /run/vcpi/vcpi.sock
```

## Debugging

```bash
journalctl -u firstboot
journalctl -u payload

# follow vcpi service logs live
journalctl -u payload -f
```

On a connected display, service logs are also mirrored to the root console
on `tty1` during startup.
