#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
MOUNT_DIR=""
LOOP_DEV=""
BOOT_MOUNT=""
BINDS=()
RESOLV_SAVED=0
POLICY_SAVED=0
QEMU_COPIED=0

restore_chroot() {
  if [ "$RESOLV_SAVED" -eq 1 ]; then
    rm -f "$MOUNT_DIR/etc/resolv.conf"
    if [ -e "$MOUNT_DIR/root/vcpi-resolv.conf" ] || [ -L "$MOUNT_DIR/root/vcpi-resolv.conf" ]; then
      mv "$MOUNT_DIR/root/vcpi-resolv.conf" "$MOUNT_DIR/etc/resolv.conf"
    fi
    RESOLV_SAVED=0
  fi
  if [ "$POLICY_SAVED" -eq 1 ]; then
    rm -f "$MOUNT_DIR/usr/sbin/policy-rc.d"
    if [ -e "$MOUNT_DIR/root/vcpi-policy-rc.d" ] || [ -L "$MOUNT_DIR/root/vcpi-policy-rc.d" ]; then
      mv "$MOUNT_DIR/root/vcpi-policy-rc.d" "$MOUNT_DIR/usr/sbin/policy-rc.d"
    fi
    POLICY_SAVED=0
  fi
  if [ "$QEMU_COPIED" -eq 1 ]; then
    rm -f "$MOUNT_DIR/usr/bin/qemu-aarch64-static"
    QEMU_COPIED=0
  fi
}

cleanup() {
  local index
  set +e
  restore_chroot
  for ((index=${#BINDS[@]}-1; index>=0; index--)); do
    if mountpoint -q "${BINDS[index]}"; then umount -R "${BINDS[index]}"; fi
  done
  if [ -n "$BOOT_MOUNT" ] && mountpoint -q "$BOOT_MOUNT"; then umount "$BOOT_MOUNT"; fi
  if [ -n "$MOUNT_DIR" ] && mountpoint -q "$MOUNT_DIR"; then umount "$MOUNT_DIR"; fi
  if [ -n "$LOOP_DEV" ]; then losetup -d "$LOOP_DEV"; fi
  if [ -n "$MOUNT_DIR" ]; then rmdir "$MOUNT_DIR"; fi
}
trap cleanup EXIT

USERCONF_PATH="${USERCONF_PATH:-$SCRIPT_DIR/userconf.txt}"
WPA_CONF_PATH="${WPA_CONF_PATH:-$SCRIPT_DIR/wpa_supplicant.conf}"
WORK_IMAGE="$SCRIPT_DIR/src.img"
WORK_IMAGE_XZ="$SCRIPT_DIR/src.img.xz"
OUTPUT_IMAGE="${OUTPUT_IMAGE:-$SCRIPT_DIR/vcpi.img}"
# Space for Debian packages, Python wheels and the JUCE build, retained as free
# space in the final filesystem. The Pi can expand further on first boot.
IMAGE_EXTRA_GIB="${IMAGE_EXTRA_GIB:-8}"
REFRESH_CACHE=0
if [ "${1:-}" = "--refresh" ] || [ "${1:-}" = "-r" ]; then
  REFRESH_CACHE=1
  shift
fi
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [--refresh|-r] <64-bit-raspios-image-url>"
  exit 1
fi
if [ "$EUID" -ne 0 ]; then echo "ERROR: run as root (sudo)"; exit 1; fi
for tool in curl xz losetup mount umount mountpoint sfdisk e2fsck resize2fs chroot tar truncate; do
  command -v "$tool" >/dev/null || { echo "ERROR: missing build tool: $tool"; exit 1; }
done
if ! [[ "$IMAGE_EXTRA_GIB" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: IMAGE_EXTRA_GIB must be a positive integer"; exit 1
fi
HOST_ARCH=$(uname -m)
if [ "$HOST_ARCH" != aarch64 ]; then
  command -v qemu-aarch64-static >/dev/null || {
    echo "ERROR: install qemu-user-static and enable aarch64 binfmt support"; exit 1;
  }
fi
if [ ! -f "$USERCONF_PATH" ] || [ ! -f "$WPA_CONF_PATH" ]; then
  echo "ERROR: create $USERCONF_PATH and $WPA_CONF_PATH from the .dist templates"
  exit 1
fi
IFS= read -r USERCONF_LINE < "$USERCONF_PATH" || true
if [[ "$USERCONF_LINE" != pi:\$* ]]; then
  echo "ERROR: userconf must contain pi:<encrypted password hash>"; exit 1
fi
WIFI_SSID=$(grep -oP '(?<=ssid=").*(?=")' "$WPA_CONF_PATH" || true)
WIFI_PSK=$(grep -oP '(?<=psk=").*(?=")' "$WPA_CONF_PATH" || true)
WIFI_COUNTRY=$(grep -oP '(?<=country=)\w+' "$WPA_CONF_PATH" || true)
if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PSK" ] || [ -z "$WIFI_COUNTRY" ]; then
  echo "ERROR: Wi-Fi config must include SSID, PSK and country"; exit 1
fi
IMAGE_URL="$1"
CACHE_DIR="${IMAGE_CACHE_DIR:-$SCRIPT_DIR/.image-cache}"
mkdir -p "$CACHE_DIR"
IMAGE_FILENAME=$(basename "${IMAGE_URL%%\?*}")
CACHED_IMAGE="$CACHE_DIR/${IMAGE_FILENAME:-source.img.xz}"
if [ "$REFRESH_CACHE" -eq 1 ]; then rm -f "$CACHED_IMAGE"; fi
if [ ! -f "$CACHED_IMAGE" ]; then
  echo "Downloading image to cache: $CACHED_IMAGE"
  if ! curl -fL "$IMAGE_URL" --output "$CACHED_IMAGE.part"; then
    rm -f "$CACHED_IMAGE.part"; exit 1
  fi
  mv "$CACHED_IMAGE.part" "$CACHED_IMAGE"
fi
rm -f "$WORK_IMAGE" "$WORK_IMAGE_XZ"
cp "$CACHED_IMAGE" "$WORK_IMAGE_XZ"
xz -T 0 -d "$WORK_IMAGE_XZ"
truncate -s "+${IMAGE_EXTRA_GIB}G" "$WORK_IMAGE"
# Raspberry Pi OS has a FAT boot partition followed by the ext4 root partition.
printf ',+\n' | sfdisk --no-reread --no-tell-kernel -N 2 "$WORK_IMAGE"
LOOP_DEV=$(losetup --show -fP "$WORK_IMAGE")
fsck_status=0
e2fsck -f -p "${LOOP_DEV}p2" || fsck_status=$?
if [ "$fsck_status" -gt 1 ]; then echo "ERROR: root filesystem check failed"; exit 1; fi
resize2fs "${LOOP_DEV}p2"
MOUNT_DIR=$(mktemp -d /tmp/rpi-img.XXXXXX)
mount "${LOOP_DEV}p2" "$MOUNT_DIR"
BOOT_MOUNT="$MOUNT_DIR/boot/firmware"
mkdir -p "$BOOT_MOUNT"
mount "${LOOP_DEV}p1" "$BOOT_MOUNT"
if [ ! -e "$MOUNT_DIR/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1" ] && \
   [ ! -e "$MOUNT_DIR/lib/ld-linux-aarch64.so.1" ]; then
  echo "ERROR: expected a 64-bit ARM Raspberry Pi OS image"; exit 1
fi

# SSH and Wi-Fi work on the local network; neither gates audio startup.
touch "$BOOT_MOUNT/ssh"
NM_DIR="$MOUNT_DIR/etc/NetworkManager/system-connections"
mkdir -p "$NM_DIR"
cat > "$NM_DIR/wifi.nmconnection" <<NMEOF
[connection]
id=$WIFI_SSID
type=wifi
autoconnect=true
autoconnect-priority=100

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
if ! grep -q 'cfg80211.ieee80211_regdom=' "$BOOT_MOUNT/cmdline.txt"; then
  sed -i "s/$/ cfg80211.ieee80211_regdom=$WIFI_COUNTRY/" "$BOOT_MOUNT/cmdline.txt"
fi
sed -i 's/^dtparam=audio=on/#&/' "$BOOT_MOUNT/config.txt"
sed -i 's/dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,noaudio/' "$BOOT_MOUNT/config.txt"

# Build the checked-out source, including local changes, not a fresh remote clone.
mkdir -p "$MOUNT_DIR/home/pi/vcpi"
tar -C "$REPO_DIR" --exclude='__pycache__' --exclude='core/_vcpi_native*.so' -cf - \
  core controllers graph sampler patches sessions vst3 native \
  main.py build_native.py requirements.txt vcpi vcpid README.md USAGE.md \
  LICENSE LICENSE.GPL-3.0 | tar -C "$MOUNT_DIR/home/pi/vcpi" -xf -

if [ -e "$MOUNT_DIR/etc/resolv.conf" ] || [ -L "$MOUNT_DIR/etc/resolv.conf" ]; then
  mv "$MOUNT_DIR/etc/resolv.conf" "$MOUNT_DIR/root/vcpi-resolv.conf"
fi
RESOLV_SAVED=1
cp -L /etc/resolv.conf "$MOUNT_DIR/etc/resolv.conf"
if [ -e "$MOUNT_DIR/usr/sbin/policy-rc.d" ] || [ -L "$MOUNT_DIR/usr/sbin/policy-rc.d" ]; then
  mv "$MOUNT_DIR/usr/sbin/policy-rc.d" "$MOUNT_DIR/root/vcpi-policy-rc.d"
fi
POLICY_SAVED=1
printf '#!/bin/sh\nexit 101\n' > "$MOUNT_DIR/usr/sbin/policy-rc.d"
chmod 755 "$MOUNT_DIR/usr/sbin/policy-rc.d"
if [ "$HOST_ARCH" != aarch64 ] && [ ! -e "$MOUNT_DIR/usr/bin/qemu-aarch64-static" ]; then
  cp "$(command -v qemu-aarch64-static)" "$MOUNT_DIR/usr/bin/"
  QEMU_COPIED=1
fi
for directory in dev proc sys; do
  mount --rbind "/$directory" "$MOUNT_DIR/$directory"
  BINDS+=("$MOUNT_DIR/$directory")
  mount --make-rslave "$MOUNT_DIR/$directory"
done
if ! chroot "$MOUNT_DIR" /bin/true; then
  echo "ERROR: cannot execute ARM64 binaries; enable qemu aarch64 binfmt on the host"
  exit 1
fi
install -m 0644 "$SCRIPT_DIR/internal/provision-target.sh" "$MOUNT_DIR/root/provision-target.sh"
touch "$MOUNT_DIR/root/.vcpi-image-build"
VCPI_IMAGE_BUILD=1 \
VCPI_USERCONF="$USERCONF_LINE" \
CARDINAL_VERSION="${CARDINAL_VERSION:-26.01}" \
CARDINAL_URL="${CARDINAL_URL:-}" \
CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-2}" \
  chroot "$MOUNT_DIR" /bin/bash /root/provision-target.sh
rm "$MOUNT_DIR/root/provision-target.sh" "$MOUNT_DIR/root/.vcpi-image-build"
restore_chroot

install -m 0644 "$SCRIPT_DIR/services/payload.service" "$MOUNT_DIR/lib/systemd/system/payload.service"
install -m 0644 "$SCRIPT_DIR/services/wifi-unblock.service" "$MOUNT_DIR/lib/systemd/system/wifi-unblock.service"
mkdir -p "$MOUNT_DIR/etc/systemd/journald.conf.d"
install -m 0644 "$SCRIPT_DIR/services/journald-console.conf" "$MOUNT_DIR/etc/systemd/journald.conf.d/console.conf"
# No provisioning, time synchronization or network-online wait on first boot.
systemctl --root="$MOUNT_DIR" enable payload.service ssh.service wifi-unblock.service
systemctl --root="$MOUNT_DIR" disable NetworkManager-wait-online.service systemd-time-wait-sync.service
sync
# Do not publish an image unless every unmount/detach succeeds.
set -e
for ((index=${#BINDS[@]}-1; index>=0; index--)); do umount -R "${BINDS[index]}"; done
BINDS=()
umount "$BOOT_MOUNT"
umount "$MOUNT_DIR"
losetup -d "$LOOP_DEV"
LOOP_DEV=""
rmdir "$MOUNT_DIR"
MOUNT_DIR=""
BOOT_MOUNT=""
trap - EXIT
mv -f "$WORK_IMAGE" "$OUTPUT_IMAGE"
echo "Offline-ready image: $OUTPUT_IMAGE"
