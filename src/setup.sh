#!/bin/bash

# deploy native patch
mkdir -vp /home/pi/Documents/templates/
cp -v /root/native.vcv /home/pi/Documents/templates/native.vcv
chown -R pi:pi /home/pi/Documents/templates/

# upgrade system
apt-get update
apt-get upgrade -y

# setup apps
apt-get install htop

# setup udev
echo "SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", MODE=\"0666\"" > /etc/udev/rules.d/50-udev-default.rules

# setup firewall
apt-get install ufw -y
ufw allow ssh
ufw enable

# cleanup
mv /root/setup.sh /root/setup.sh.done
chmod -x /root/setup.sh.done
	
echo "done :)"
exit 0