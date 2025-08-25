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

#enable systemd-time-wait-sync
ln -v -s /lib/systemd/system/systemd-time-wait-sync.service /tmp/rpi-img/etc/systemd/system/sysinit.target.wants/systemd-time-wait-sync.service

#setup autorun on first boot
cp -v ./services/firstboot.service /tmp/rpi-img/lib/systemd/system/firstboot.service
ln -v -s /lib/systemd/system/firstboot.service /tmp/rpi-img/etc/systemd/system/multi-user.target.wants

# disable built-in audio
sed -i 's/^dtparam=audio=on/#&/' /tmp/rpi-img/boot/config.txt

# disable hdmi audio
sed -i 's/dtoverlay=vc4-kms-v3d/dtoverlay=vc4-kms-v3d,noaudio/' /tmp/rpi-img/boot/config.txt

# setup GUI payload
cp -v ./services/payload.service /tmp/rpi-img/lib/systemd/system/payload.service
ln -v -s /lib/systemd/system/payload.service /tmp/rpi-img/etc/systemd/system/graphical.target.wants

#provision project files
cp -v ./src/setup.sh /tmp/rpi-img/root/setup.sh
cp -v ./patch/init.pd /tmp/rpi-img/root/init.pd

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
