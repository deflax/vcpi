#!/bin/bash
#

REFRESH_CACHE=0

if [ "$1" = "--refresh" ] || [ "$1" = "-r" ]; then
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

IMAGE_FILENAME=$(basename "${IMAGE_URL%%\?*}")
if [ -z "$IMAGE_FILENAME" ]; then
  IMAGE_FILENAME="source.img.xz"
fi

CACHED_IMAGE="$CACHE_DIR/$IMAGE_FILENAME"
TMP_DOWNLOAD="$CACHED_IMAGE.part"

# download source once and reuse cache
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

cp -v "$CACHED_IMAGE" src.img.xz
xz -v -T 0 -d src.img.xz

#setup
losetup -P /dev/loop8 src.img
mkdir -v /tmp/rpi-img
mount /dev/loop8p2 /tmp/rpi-img
mount /dev/loop8p1 /tmp/rpi-img/boot

#enable ssh
touch /tmp/rpi-img/boot/ssh
cp -v userconf.txt /tmp/rpi-img/boot/userconf

#wifi (NetworkManager for Bookworm+)
if [ ! -f wpa_supplicant.conf ]; then
  echo "ERROR: wpa_supplicant.conf not found (create it locally; it is gitignored)"
  exit 1
fi
WIFI_SSID=$(grep -oP '(?<=ssid=").*(?=")' wpa_supplicant.conf)
WIFI_PSK=$(grep -oP '(?<=psk=").*(?=")' wpa_supplicant.conf)

if [ -z "$WIFI_SSID" ] || [ -z "$WIFI_PSK" ]; then
  echo "ERROR: Could not read SSID or PSK from wpa_supplicant.conf"
  exit 1
fi

NM_DIR=/tmp/rpi-img/etc/NetworkManager/system-connections
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
mkdir /tmp/rpi-img/home/pi/.ssh
#echo "" > /tmp/rpi-img/home/pi/.ssh/authorized_keys
chown 1000:1000 /tmp/rpi-img/home/pi/.ssh
chown 1000:1000 /tmp/rpi-img/home/pi/.ssh/authorized_keys

#enable systemd-time-wait-sync
ln -v -s /lib/systemd/system/systemd-time-wait-sync.service /tmp/rpi-img/etc/systemd/system/sysinit.target.wants/systemd-time-wait-sync.service

#setup autorun on first boot
cp -v ./services/firstboot.service /tmp/rpi-img/lib/systemd/system/firstboot.service
ln -v -s /lib/systemd/system/firstboot.service /tmp/rpi-img/etc/systemd/system/multi-user.target.wants

# disable built-in audio
sed -i 's/^dtparam=audio=on/#&/' /tmp/rpi-img/boot/config.txt

# disable hdmi audio
sed -i 's/dtoverlay=vc4-kms-v3d/dtoverlay=vc4-kms-v3d,noaudio/' /tmp/rpi-img/boot/config.txt

# setup vcpi payload
cp -v ./services/payload.service /tmp/rpi-img/lib/systemd/system/payload.service
ln -v -s /lib/systemd/system/payload.service /tmp/rpi-img/etc/systemd/system/multi-user.target.wants

#provision project files
cp -v ./src/setup.sh /tmp/rpi-img/root/setup.sh
cp -rv ./src/linkvst /tmp/rpi-img/root/linkvst
cp -v ./src/vst_host.py /tmp/rpi-img/root/vst_host.py
cp -v ./src/requirements.txt /tmp/rpi-img/root/requirements.txt

sync

#cleanup
umount /tmp/rpi-img/boot
umount /tmp/rpi-img
losetup -d /dev/loop8
rmdir -v /tmp/rpi-img

#write image
mv -i src.img vcpi.img
