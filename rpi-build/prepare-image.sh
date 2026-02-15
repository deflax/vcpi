#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

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
USERCONF_PATH="${USERCONF_PATH:-$SCRIPT_DIR/userconf.txt}"
WPA_CONF_PATH="${WPA_CONF_PATH:-$SCRIPT_DIR/wpa_supplicant.conf}"
WORK_IMAGE="$SCRIPT_DIR/src.img"
WORK_IMAGE_XZ="$SCRIPT_DIR/src.img.xz"
OUTPUT_IMAGE="${OUTPUT_IMAGE:-$SCRIPT_DIR/vcpi.img}"

# Legacy default from older repository versions. Block this value unless
# explicitly allowed for backwards compatibility.
INSECURE_DEFAULT_USERCONF='pi:$6$J3VR90uJ/TxGhcPf$OzVHJSqmGWsJlDFxCumcwftgv2okaHZlbrTyu5MX0YXKrDrVxMxsexbroXUt5CkSu0xedQAcfvHm5CDpkiiDu0'

REFRESH_CACHE=0

if [ "${1:-}" = "--refresh" ] || [ "${1:-}" = "-r" ]; then
  REFRESH_CACHE=1
  shift
fi

if [ -z "${1:-}" ]; then
  echo "Usage: $0 [--refresh|-r] <image-url>"
  exit 1
fi

IMAGE_URL="$1"
CACHE_DIR="${IMAGE_CACHE_DIR:-$SCRIPT_DIR/.image-cache}"
mkdir -p "$CACHE_DIR"

if [ ! -d "$REPO_ROOT/linkvst" ] || [ ! -f "$REPO_ROOT/vst_host.py" ] || [ ! -f "$REPO_ROOT/requirements.txt" ]; then
  echo "ERROR: LinkVST project files not found at repo root: $REPO_ROOT"
  exit 1
fi

if [ ! -f "$USERCONF_PATH" ]; then
  echo "ERROR: $USERCONF_PATH not found"
  echo "Create it from rpi-build/userconf.txt.dist with your own SHA-512 password hash"
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

rm -f "$WORK_IMAGE" "$WORK_IMAGE_XZ"
cp -v "$CACHED_IMAGE" "$WORK_IMAGE_XZ"
xz -v -T 0 -d "$WORK_IMAGE_XZ"

# WiFi (NetworkManager for Bookworm+)
if [ ! -f "$WPA_CONF_PATH" ]; then
  echo "ERROR: $WPA_CONF_PATH not found (create it locally; it is gitignored)"
  exit 1
fi

WIFI_SSID=$(grep -oP '(?<=ssid=").*(?=")' "$WPA_CONF_PATH" || true)
WIFI_PSK=$(grep -oP '(?<=psk=").*(?=")' "$WPA_CONF_PATH" || true)

if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PSK" ]; then
  echo "ERROR: Could not read SSID or PSK from $WPA_CONF_PATH"
  exit 1
fi

# Setup loopback mounts.
MOUNT_DIR=$(mktemp -d /tmp/rpi-img.XXXXXX)
BOOT_MOUNT="$MOUNT_DIR/boot"
mkdir -p "$BOOT_MOUNT"

LOOP_DEV=$(losetup --show -fP "$WORK_IMAGE")
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
cp -v "$SCRIPT_DIR/services/firstboot.service" "$MOUNT_DIR/lib/systemd/system/firstboot.service"
ln -v -sf /lib/systemd/system/firstboot.service "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"

# disable built-in audio
sed -i 's/^dtparam=audio=on/#&/' "$BOOT_MOUNT/config.txt"

# disable hdmi audio
sed -i 's/dtoverlay=vc4-kms-v3d/dtoverlay=vc4-kms-v3d,noaudio/' "$BOOT_MOUNT/config.txt"

# setup LinkVST payload
cp -v "$SCRIPT_DIR/services/payload.service" "$MOUNT_DIR/lib/systemd/system/payload.service"
ln -v -sf /lib/systemd/system/payload.service "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"

#provision project files
cp -v "$SCRIPT_DIR/setup.sh" "$MOUNT_DIR/root/setup.sh"
cp -rv "$REPO_ROOT/linkvst" "$MOUNT_DIR/root/linkvst"
cp -v "$REPO_ROOT/vst_host.py" "$MOUNT_DIR/root/vst_host.py"
cp -v "$REPO_ROOT/requirements.txt" "$MOUNT_DIR/root/requirements.txt"

sync

# Cleanup mounted resources before moving final image.
cleanup
trap - EXIT

#write image
mv -i "$WORK_IMAGE" "$OUTPUT_IMAGE"
