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

#setup autorun on first boot
cp -v ./src/firstboot.service /tmp/rpi-img/lib/systemd/system/firstboot.service
ln -v -s /lib/systemd/system/firstboot.service /tmp/rpi-img/etc/systemd/system/multi-user.target.wants

#enable systemd-time-wait-sync
ln -v -s /lib/systemd/system/systemd-time-wait-sync.service /tmp/rpi-img/etc/systemd/system/sysinit.target.wants/systemd-time-wait-sync.service

# disable built-in audio
sed -i 's/^dtparam=audio=on/#&/' /tmp/rpi-img/boot/firmware/config.txt

echo "] press enter to write the image
read

sync

#cleanup
losetup -d /dev/loop8
umount /tmp/rpi-img/boot
umount /tmp/rpi-img
rmdir -v /tmp/rpi-img

#write image
xz -v -T 0 -z src.img
mv -v src.img.xz vcpi.img.xz
