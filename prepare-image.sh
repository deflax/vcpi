#!/bin/bash
#

if [ -z "$1" ]
  then
    echo "No image name provided"
    exit
fi

#download source
curl $1 --output src.img.xz
xz -v -T 0 -d src.img.xz

#setup
losetup -P /dev/loop8 src.img
mkdir -v /tmp/rpi-img
mount /dev/loop8p2 /tmp/rpi-img
mount /dev/loop8p1 /tmp/rpi-img/boot

#enable ssh
touch /tmp/rpi-img/boot/ssh
cp -v userconf.txt /tmp/rpi-img/boot/userconf

# pi user keys
mkdir /tmp/rpi-img/home/pi/.ssh
#echo "" > /tmp/rpi-img/home/pi/.ssh/authorized_keys
chown 1000:1000 /tmp/rpi-img/home/pi/.ssh
chown 1000:1000 /tmp/rpi-img/home/pi/.ssh/authorized_keys

#provision scripts
cp -v ./src/setup.sh /tmp/rpi-img/root/setup.sh

#enable systemd-time-wait-sync
ln -v -s /lib/systemd/system/systemd-time-wait-sync.service /tmp/rpi-img/etc/systemd/system/sysinit.target.wants/systemd-time-wait-sync.service

#setup autorun on first boot
cp -v ./src/firstboot.service /tmp/rpi-img/lib/systemd/system/firstboot.service
ln -v -s /lib/systemd/system/firstboot.service /tmp/rpi-img/etc/systemd/system/multi-user.target.wants

# disable built-in audio
sed -i 's/^dtparam=audio=on/#&/' /tmp/rpi-img/boot/config.txt

# setup Cardinal
mkdir -v /tmp/rpi-img/opt/Cardinal
wget https://github.com/DISTRHO/Cardinal/releases/download/24.09/Cardinal-linux-aarch64-24.09.tar.gz -O /tmp/rpi-img/opt/Cardinal/Cardinal-linux-aarch64.tar.gz
tar -xzvf /tmp/rpi-img/opt/Cardinal/Cardinal-linux-aarch64.tar.gz -C /tmp/rpi-img/opt/Cardinal/ CardinalNative

# setup GUI payload
ln -v -s /lib/systemd/system/payload.service /tmp/rpi-img/etc/systemd/system/graphical.target.wants

# deploy native patch
cp -v ./patch/native.vcv /home/pi/Documents/templates/native.vcv

echo "] press enter to write the image"
read

sync

#cleanup
losetup -d /dev/loop8
umount /tmp/rpi-img/boot
umount /tmp/rpi-img
rmdir -v /tmp/rpi-img

#write image
mv -v src.img vcpi.img
