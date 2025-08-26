#!/bin/bash

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
  libfftw3-dev \
  libxt-dev \
  libavahi-client-dev \
  libudev-dev \
  libasound2-dev \
  libreadline-dev \
  libxkbcommon-dev \
  libqt6widgets6 \
  jackd2 -y
  # Accept realtime permissions for jackd when asked

# setup sonic-pi
wget https://sonic-pi.net/files/releases/v4.6.0/sonic-pi_4.6.0_1_bookworm.arm64.deb
dpkg -i sonic-pi_4.6.0_1_bookworm.arm64.deb

# setup udev
echo "SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", MODE=\"0666\"" > /etc/udev/rules.d/50-udev-default.rules

# setup firewall
apt-get install ufw -y
ufw allow ssh
ufw enable

# #build supercolider
# echo "building in:"
# pwd
# git clone --branch main --recurse-submodules https://github.com/supercollider/supercollider.git
# cd supercollider
# mkdir build && cd build
# cmake -DCMAKE_BUILD_TYPE=Release -DSUPERNOVA=OFF -DSC_EL=OFF -DSC_VIM=ON -DNATIVE=ON ..
# make -j3
# make install
# ldconfig

#setup jack
echo /usr/bin/jackd -P75 -p16 -dalsa -dhw:0 -p1024 -n3 > /home/pi/.jackdrc

# deploy patch
mv -v /root/autorun.rb /home/pi/.sonic-pi/config/init.rb
chown -R pi:pi /home/pi/.sonic-pi

# cleanup
mv /root/setup.sh /root/setup.sh.done
chmod -x /root/setup.sh.done

echo "done :)"

# reboot
exit 0
