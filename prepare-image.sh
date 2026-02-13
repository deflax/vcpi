#!/bin/bash

set -euo pipefail

cleanup() {
  set +e
  if [ -n "${BOOT_MOUNT:-}" ] && mountpoint -q "$BOOT_MOUNT"; then
    umount "$BOOT_MOUNT"
  fi
  if [ -n "${MOUNT_DIR:-}" ] && mountpoint -q "$MOUNT_DIR"; then
    umount "$MOUNT_DIR"
  fi
  if [ -n "${LOOP_DEV:-}" ]; then
    losetup -d "$LOOP_DEV" 2>/dev/null || true
  fi
  if [ -n "${BOOT_MOUNT:-}" ] && [ -d "$BOOT_MOUNT" ]; then
    rmdir "$BOOT_MOUNT" 2>/dev/null || true
  fi
  if [ -n "${MOUNT_DIR:-}" ] && [ -d "$MOUNT_DIR" ]; then
    rmdir "$MOUNT_DIR" 2>/dev/null || true
  fi
}

trap cleanup EXIT

MOUNT_DIR=""
BOOT_MOUNT=""
LOOP_DEV=""
USERCONF_PATH="${USERCONF_PATH:-userconf.txt}"

# Legacy default from older repository versions. Block this value unless
# explicitly allowed for backwards compatibility.
INSECURE_DEFAULT_USERCONF='pi:$6$J3VR90uJ/TxGhcPf$OzVHJSqmGWsJlDFxCumcwftgv2okaHZlbrTyu5MX0YXKrDrVxMxsexbroXUt5CkSu0xedQAcfvHm5CDpkiiDu0'

REFRESH_CACHE=0

if [ "${1:-}" = "--refresh" ] || [ "${1:-}" = "-r" ]; then
  REFRESH_CACHE=1
  shift
fi

if [ -z "$1" ]; then
  echo "Usage: $0 [--refresh|-r] <image-url>"
  exit 1
fi

IMAGE_URL="$1"
CACHE_DIR="${IMAGE_CACHE_DIR:-.image-cache}"
mkdir -p "$CACHE_DIR"

if [ ! -f "$USERCONF_PATH" ]; then
  echo "ERROR: $USERCONF_PATH not found"
  echo "Create it from userconf.example.txt with your own SHA-512 password hash"
  exit 1
fi

USERCONF_LINE=$(head -n 1 "$USERCONF_PATH" || true)
if [ -z "$USERCONF_LINE" ]; then
  echo "ERROR: $USERCONF_PATH is empty"
  exit 1
fi

if [ "$USERCONF_LINE" = "$INSECURE_DEFAULT_USERCONF" ] && [ "${ALLOW_INSECURE_DEFAULTS:-0}" != "1" ]; then
  echo "ERROR: insecure default credentials detected in $USERCONF_PATH"
  echo "Set a unique password hash (or ALLOW_INSECURE_DEFAULTS=1 to override)"
  exit 1
fi

IMAGE_FILENAME=$(basename "${IMAGE_URL%%\?*}")
if [ -z "$IMAGE_FILENAME" ]; then
  IMAGE_FILENAME="source.img.xz"
fi

CACHED_IMAGE="$CACHE_DIR/$IMAGE_FILENAME"
TMP_DOWNLOAD="$CACHED_IMAGE.part"

# Download source once and reuse cache.
if [ "$REFRESH_CACHE" -eq 1 ] && [ -f "$CACHED_IMAGE" ]; then
  echo "Refreshing cached image: $CACHED_IMAGE"
  rm -f "$CACHED_IMAGE"
fi

if [ ! -f "$CACHED_IMAGE" ]; then
  echo "Downloading image to cache: $CACHED_IMAGE"
  if ! curl -fL "$IMAGE_URL" --output "$TMP_DOWNLOAD"; then
    rm -f "$TMP_DOWNLOAD"
    echo "ERROR: image download failed"
    exit 1
  fi
  mv -f "$TMP_DOWNLOAD" "$CACHED_IMAGE"
else
  echo "Using cached image: $CACHED_IMAGE"
fi

rm -f src.img src.img.xz
cp -v "$CACHED_IMAGE" src.img.xz
xz -v -T 0 -d src.img.xz

# WiFi (NetworkManager for Bookworm+)
if [ ! -f wpa_supplicant.conf ]; then
  echo "ERROR: wpa_supplicant.conf not found (create it locally; it is gitignored)"
  exit 1
fi

WIFI_SSID=$(grep -oP '(?<=ssid=").*(?=")' wpa_supplicant.conf || true)
WIFI_PSK=$(grep -oP '(?<=psk=").*(?=")' wpa_supplicant.conf || true)

if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PSK" ]; then
  echo "ERROR: Could not read SSID or PSK from wpa_supplicant.conf"
  exit 1
fi

# Setup loopback mounts.
MOUNT_DIR=$(mktemp -d /tmp/rpi-img.XXXXXX)
BOOT_MOUNT="$MOUNT_DIR/boot"
mkdir -p "$BOOT_MOUNT"

LOOP_DEV=$(losetup --show -fP src.img)
mount "${LOOP_DEV}p2" "$MOUNT_DIR"
mount "${LOOP_DEV}p1" "$BOOT_MOUNT"

# Enable SSH.
touch "$BOOT_MOUNT/ssh"
cp -v "$USERCONF_PATH" "$BOOT_MOUNT/userconf"

# WiFi profile.
NM_DIR="$MOUNT_DIR/etc/NetworkManager/system-connections"
mkdir -p "$NM_DIR"
cat > "$NM_DIR/wifi.nmconnection" <<NMEOF
[connection]
id=$WIFI_SSID
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=$WIFI_SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PSK

[ipv4]
method=auto

[ipv6]
method=auto
NMEOF
chmod 600 "$NM_DIR/wifi.nmconnection"
echo "WiFi configured for SSID: $WIFI_SSID"

# pi user keys
mkdir -p "$MOUNT_DIR/home/pi/.ssh"
touch "$MOUNT_DIR/home/pi/.ssh/authorized_keys"
chmod 700 "$MOUNT_DIR/home/pi/.ssh"
chmod 600 "$MOUNT_DIR/home/pi/.ssh/authorized_keys"
chown 1000:1000 "$MOUNT_DIR/home/pi/.ssh"
chown 1000:1000 "$MOUNT_DIR/home/pi/.ssh/authorized_keys"

#enable systemd-time-wait-sync
ln -v -sf /lib/systemd/system/systemd-time-wait-sync.service "$MOUNT_DIR/etc/systemd/system/sysinit.target.wants/systemd-time-wait-sync.service"

#setup autorun on first boot
cp -v ./services/firstboot.service "$MOUNT_DIR/lib/systemd/system/firstboot.service"
ln -v -sf /lib/systemd/system/firstboot.service "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"

# disable built-in audio
sed -i 's/^dtparam=audio=on/#&/' "$BOOT_MOUNT/config.txt"

# disable hdmi audio
sed -i 's/dtoverlay=vc4-kms-v3d/dtoverlay=vc4-kms-v3d,noaudio/' "$BOOT_MOUNT/config.txt"

# setup vcpi payload
cp -v ./services/payload.service "$MOUNT_DIR/lib/systemd/system/payload.service"
ln -v -sf /lib/systemd/system/payload.service "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"

#provision project files
cp -v ./src/setup.sh "$MOUNT_DIR/root/setup.sh"
cp -rv ./src/linkvst "$MOUNT_DIR/root/linkvst"
cp -v ./src/vst_host.py "$MOUNT_DIR/root/vst_host.py"
cp -v ./src/requirements.txt "$MOUNT_DIR/root/requirements.txt"

sync

# Cleanup mounted resources before moving final image.
cleanup
trap - EXIT

#write image
mv -i src.img vcpi.img
