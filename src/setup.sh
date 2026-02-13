#!/bin/bash

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# upgrade system
apt-get update
apt-get upgrade -y

# setup apps
apt-get install htop \
  wget \
  curl \
  vim \
  cpufrequtils -y

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

# grant device access via groups instead of world-writable USB rules
usermod -aG audio,plugdev pi

# setup firewall
apt-get install ufw -y
ufw allow ssh
ufw --force enable

#setup jack
echo /usr/bin/jackd -P75 -p16 -dalsa -dhw:0 -p1024 -n3 > /home/pi/.jackdrc

# deploy vcpi
VCPI_DIR=/home/pi/vcpi
mkdir -p "$VCPI_DIR"
mv -v /root/linkvst "$VCPI_DIR/linkvst"
mv -v /root/vst_host.py "$VCPI_DIR/vst_host.py"
mv -v /root/requirements.txt "$VCPI_DIR/requirements.txt"

python3 -m venv "$VCPI_DIR/venv"
"$VCPI_DIR/venv/bin/pip" install --upgrade pip
"$VCPI_DIR/venv/bin/pip" install -r "$VCPI_DIR/requirements.txt"

chown -R pi:pi "$VCPI_DIR"

# cleanup
mv /root/setup.sh /root/setup.sh.done
chmod -x /root/setup.sh.done

echo "done :)"

# reboot
exit 0
