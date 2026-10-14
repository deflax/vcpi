# Raspberry Pi Image Build Tools

This folder contains tooling only for building and provisioning a Raspberry Pi
OS image that auto-starts vcpi.

## Files

- `prepare-image.sh` - image download, mount, provisioning, and packing
- `setup.sh` - first boot provisioning script executed on the Pi
- `services/firstboot.service` - runs `setup.sh` once
- `services/wifi-unblock.service` - unblocks rfkill and enables Wi-Fi before network-online
- `services/payload.service` - starts vcpi daemon on boot
- `services/journald-console.conf` - mirrors journal logs to root console (tty1)
- `userconf.txt.dist` - template for Raspberry Pi credentials
- `wpa_supplicant.conf.dist` - template Wi-Fi config

## Quick Build

From repository root:

```bash
cp rpi-build/wpa_supplicant.conf.dist rpi-build/wpa_supplicant.conf
cp rpi-build/userconf.txt.dist rpi-build/userconf.txt

# Replace password hash with your own SHA-512 hash
HASH=$(openssl passwd -6 'your-strong-password')
printf 'pi:%s\n' "$HASH" > rpi-build/userconf.txt

sudo ./rpi-build/prepare-image.sh <raspios-image-url>
```

From inside this folder:

```bash
sudo ./prepare-image.sh <raspios-image-url>
```

Output image path:

```text
rpi-build/vcpi.img
```

The local secrets/config files (`userconf.txt`, `wpa_supplicant.conf`) are kept
inside `rpi-build/` and are gitignored.

## Script Options

```bash
# Force re-download of cached source archive
sudo ./rpi-build/prepare-image.sh --refresh <raspios-image-url>

# Custom cache directory
sudo IMAGE_CACHE_DIR=/var/cache/vcpi ./rpi-build/prepare-image.sh <raspios-image-url>

# Custom credentials or Wi-Fi file paths
sudo USERCONF_PATH=/secure/userconf.txt WPA_CONF_PATH=/secure/wpa_supplicant.conf \
  ./rpi-build/prepare-image.sh <raspios-image-url>
```

## On First Boot

- `firstboot.service` runs `/root/setup.sh`
- `wifi-unblock.service` runs before `network-online.target` (`rfkill unblock all`, `nmcli radio wifi on`)
- `NetworkManager-wait-online.service` is enabled so first boot waits for connectivity
- The vcpi repo is cloned from GitHub into `/home/pi/vcpi`
- Python venv is created at `/home/pi/vcpi/.venv`
- The native C++ audio extension is built with the Pi's Python interpreter
- Cardinal VST3 is installed at `/usr/local/lib/vst3/Cardinal.vst3`
- `payload.service` sets `LOG_LEVEL=DEBUG`
- journald forwards logs to `/dev/tty1` at `debug` level
- `payload.service` starts:

```text
/home/pi/vcpi/.venv/bin/python /home/pi/vcpi/main.py serve --sock /run/vcpi/vcpi.sock
```

## Selective Realtime Scheduling

On Linux, vcpi attempts `SCHED_FIFO` priority **20** on the native output
callback thread and priority **10** on each native slot DSP thread. Python and
process-control threads keep normal `SCHED_OTHER` scheduling. The Pi unit runs
as `pi` and sets only `LimitRTPRIO=20`: this finite permission allowance is
inherited by its child processes; it does not promote the whole daemon or its
children. It does not set `CPUSchedulingPolicy`, run vcpi as root, or add
`CAP_SYS_NICE`.

Use `status` or `audio diagnose` to inspect output and slot realtime state,
actual policy/priority, requested priority, native thread ID, and any errno.
`active` means the live thread actually has the requested FIFO priority.
`denied`, for example `SCHED_OTHER priority 0`, `errno=1 (Operation not permitted)`,
means promotion failed; audio remains usable without realtime scheduling.
See [USAGE.md](../USAGE.md#selective-realtime-scheduling) for all diagnostic states.

The systemd allowance applies to the service, not to an SSH/login shell:
starting `./vcpid` manually may still report `denied`. `ulimit -r` shows the
shell's realtime-priority limit. Any manual-launch permission setup must allow
the native child threads to request priority 20 without globally promoting
Python/control threads. Do not use whole-process `chrt` as a substitute.
This Linux-only feature reports `unsupported` on macOS and makes no macOS
scheduling-policy changes.

Scheduling does not change the buffer size or the existing one-block slot
handoff latency, and does not guarantee dropout-free audio under overload.
Measure callback deadlines, underruns, and missing slot blocks before further
tuning; global realtime promotion, wakeup changes, memory locking, affinity,
governor changes, and kernel tuning are not part of this setup.

Verification scope: the native extension and a daemon/CLI smoke run were
exercised on Linux aarch64 with CPython 3.13, an ALSA null sink, and a spawned WAV
slot at 44100 Hz / 512 samples. With `RTPRIO=(0,0)`, output/slot diagnostics
matched actual kernel `SCHED_OTHER`/priority 0 and reported permission denial
without stopping the owner. Python/control threads remained `SCHED_OTHER`;
output stop/restart reported inactive state and a new callback thread ID.
Successful FIFO promotion and physical Pi audio hardware were **not** exercised;
the unpaced null sink is not evidence of latency or audio quality.

## Updating

SSH into the Pi and pull the latest changes:

```bash
cd /home/pi/vcpi
git pull
.venv/bin/pip install -r requirements.txt   # if dependencies changed
.venv/bin/python build_native.py            # rebuild native code for this Pi
sudo install -m 0644 rpi-build/services/payload.service /lib/systemd/system/payload.service
sudo systemctl daemon-reload
sudo systemctl restart payload
```

Pulling the repo alone does not update the installed unit. The install and
`daemon-reload` steps above apply the finite realtime allowance; restarting
`payload` gives the daemon and newly spawned children the updated limit.

## Debugging

```bash
journalctl -u firstboot
journalctl -u payload

# follow vcpi service logs live
journalctl -u payload -f
```

On a connected display, service logs are also mirrored to the root console
on `tty1` during startup.
