#!/bin/bash

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

CARDINAL_VERSION="${CARDINAL_VERSION:-26.01}"
CARDINAL_ARCHIVE="Cardinal-linux-aarch64-${CARDINAL_VERSION}.tar.gz"
CARDINAL_URL="${CARDINAL_URL:-https://github.com/DISTRHO/Cardinal/releases/download/${CARDINAL_VERSION}/${CARDINAL_ARCHIVE}}"

install_cardinal_vst3() {
  local tmp_dir
  local archive_path
  local extract_dir
  local cardinal_dir

  tmp_dir="$(mktemp -d /var/tmp/cardinal.XXXXXX)"
  archive_path="$tmp_dir/$CARDINAL_ARCHIVE"
  extract_dir="$tmp_dir/extract"
  mkdir -p "$extract_dir"

  echo "Downloading Cardinal VST3: $CARDINAL_URL"
  if ! curl -fL --retry 6 --retry-delay 2 --retry-all-errors "$CARDINAL_URL" -o "$archive_path"; then
    echo "ERROR: failed to download Cardinal archive"
    rm -rf "$tmp_dir"
    return 1
  fi

  tar -xzf "$archive_path" -C "$extract_dir"

  cardinal_dir="$(find "$extract_dir" -type d -name "Cardinal.vst3" -print -quit)"
  if [ -z "$cardinal_dir" ]; then
    echo "ERROR: Cardinal.vst3 not found in archive: $CARDINAL_ARCHIVE"
    rm -rf "$tmp_dir"
    return 1
  fi

  mkdir -p /usr/local/lib/vst3
  rm -rf /usr/local/lib/vst3/Cardinal.vst3
  cp -a "$cardinal_dir" /usr/local/lib/vst3/Cardinal.vst3
  chmod -R a+rX /usr/local/lib/vst3/Cardinal.vst3

  echo "Installed Cardinal VST3 to /usr/local/lib/vst3/Cardinal.vst3"
  rm -rf "$tmp_dir"
}

# upgrade system
apt-get update
apt-get upgrade -y

# setup apps
apt-get install htop \
  wget \
  curl \
  vim \
  -y
  #cpufrequtils -y

# setup packages
apt-get install \
  build-essential \
  cmake \
  libjack-jackd2-dev \
  libsndfile1-dev \
  libavahi-client-dev \
  libudev-dev \
  libasound2-dev \
  libreadline-dev \
  libportaudio2 \
  portaudio19-dev \
  python3-pip \
  python3-venv \
  jackd2 -y

# install Cardinal (aarch64 release) for `load vcv`
install_cardinal_vst3

# grant device access via groups instead of world-writable USB rules
usermod -aG audio,plugdev pi

# setup firewall
apt-get install ufw -y
ufw allow ssh
ufw --force enable

#setup jack
echo /usr/bin/jackd -P75 -p16 -dalsa -dhw:0 -p1024 -n3 > /home/pi/.jackdrc

# deploy runtime sources
PROJECT_DIR=/home/pi/vcpi
mkdir -p "$PROJECT_DIR"
mv -v /root/core "$PROJECT_DIR/core"
mv -v /root/controllers "$PROJECT_DIR/controllers"
mv -v /root/graph "$PROJECT_DIR/graph"
mv -v /root/main.py "$PROJECT_DIR/main.py"
mv -v /root/requirements.txt "$PROJECT_DIR/requirements.txt"
mkdir -p "$PROJECT_DIR/patches"

python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

chown -R pi:pi "$PROJECT_DIR"

# cleanup
mv /root/setup.sh /root/setup.sh.done
chmod -x /root/setup.sh.done

echo "done :)"

# reboot
exit 0
