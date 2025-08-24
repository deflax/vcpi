#!/bin/bash

# upgrade system
apt update
apt dist-upgrade -y --autoremove

# setup apps
apt install htop wget -y

# setup sonic-pi
wget https://sonic-pi.net/files/releases/v4.6.0/sonic-pi_4.6.0_1_bookworm.arm64.deb -o sonicpi.deb
dpkg -i sonicpi.deb

# setup udev
echo "SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", MODE=\"0666\"" > /etc/udev/rules.d/50-udev-default.rules

# setup firewall
apt install ufw -y
ufw allow ssh
ufw enable

# deploy patch
mkdir -vp /home/pi/.sonic-pi/config/
mv -v /root/init.rb /home/pi/.sonic-pi/config/init.rb
chown -R pi:pi /home/pi/.sonic-pi/config

# cleanup
mv /root/setup.sh /root/setup.sh.done
chmod -x /root/setup.sh.done
	
echo "done :)"
exit 0