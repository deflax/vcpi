#!/bin/bash

# set opensuse raspbian repo for plugdata
echo 'deb http://download.opensuse.org/repositories/home:/plugdata/Raspbian_12/ /' | tee /etc/apt/sources.list.d/home:plugdata.list
curl -fsSL https://download.opensuse.org/repositories/home:plugdata/Raspbian_12/Release.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/home_plugdata.gpg > /dev/null

# upgrade system
apt-get update
apt-get upgrade -y

# setup apps
apt-get install htop wget curl -y

# setup plugdata
apt-get install plugdata

# setup udev
echo "SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", MODE=\"0666\"" > /etc/udev/rules.d/50-udev-default.rules

# setup firewall
apt-get install ufw -y
ufw allow ssh
ufw enable

# deploy patch
mkdir -vp /home/pi/
mv -v /root/init.pd /home/pi/init.pd
chown pi:pi /home/pi/init.pd

# cleanup
mv /root/setup.sh /root/setup.sh.done
chmod -x /root/setup.sh.done

echo "done :)"

# reboot and exit
reboot
exit 0