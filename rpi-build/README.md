# Raspberry Pi Image Build Tools

Builds a fully provisioned **64-bit Raspberry Pi OS (Bookworm or newer)** image
that auto-starts vcpi. Internet is required on the build machine, not on the Pi
at first boot or during normal use. SSH and Ableton Link use the local network.

## Files

- `prepare-image.sh` - image download, mount, provisioning, and packing
- `internal/provision-target.sh` - internal, non-executable target provisioner;
  called only by `prepare-image.sh` inside the mounted image
- `services/wifi-unblock.service` - unblocks rfkill and enables Wi-Fi at startup
- `services/payload.service` - starts vcpi daemon on boot
- `services/journald-console.conf` - mirrors journal logs to root console (tty1)
- `userconf.txt.dist` - template for Raspberry Pi credentials
- `wpa_supplicant.conf.dist` - template Wi-Fi config

Run only `prepare-image.sh`. The internal provisioner has no executable bit and
rejects explicit `bash` invocation unless the builder supplies both its build
flag and a marker inside the target root. This prevents accidental host execution;
it is not a security sandbox.


## Quick Build

Use a Linux build host with root access, loop devices, mount permission and enough
disk space for the source image, expanded working image and output image.
Containers without privileged mounts cannot build this image.

On a Debian/Ubuntu build host:

```bash
sudo apt-get install curl xz-utils util-linux fdisk e2fsprogs tar
# Additionally on x86_64 hosts:
sudo apt-get install qemu-user-static binfmt-support
sudo update-binfmts --enable qemu-aarch64
```

Native ARM64 hosts do not need QEMU. Cross-architecture builds execute the target
Python and compiler under QEMU; this is slower than building on ARM64. The builder
checks target execution before provisioning. Use an official ARM64 Raspberry Pi
OS `.img.xz` with a FAT first partition and ext4 root second partition.


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

# Add filesystem space for provisioning/native compilation (default: 8 GiB)
sudo IMAGE_EXTRA_GIB=12 ./rpi-build/prepare-image.sh <raspios-image-url>

# Override the pinned Cardinal release or download URL
sudo CARDINAL_VERSION=26.01 ./rpi-build/prepare-image.sh <raspios-image-url>
```

## What Is Installed During Build

- OS packages and Python dependencies are installed inside the image.
- The current local application checkout, including uncommitted source changes,
  is copied to `/home/pi/vcpi`; no remote clone is used.
- Host `.venv`, native extension, build cache, Git metadata and image credential
  files are not copied. Target-native binaries are rebuilt using the image's Python.
- A `pi` account is created using `userconf.txt`, with audio/plugdev/sudo access.
- Python venv is created at `/home/pi/vcpi/.venv`, including the launcher dependency
  stamp so `./vcpi` does not run pip on first use.
- Cardinal 26.01 ARM64 VST3 is installed at `/usr/local/lib/vst3/Cardinal.vst3`.
- The native C++ extension is built and a WAV/MIDI/mixer smoke runs without an audio
  device. Native build dependencies are retained for offline incremental builds.
- Temporary build-host DNS, service-start suppression and the copied QEMU
  interpreter are removed/restored before the output image is published.

The image contains your Wi-Fi credentials, password hash, application patches,
samples and sessions. Treat it as private. Any additional plugins already present
in the local `vst3/` tree must be compatible with Linux ARM64.

## On First Boot

No apt, pip, Git downloads, compilation or provisioning service runs on the Pi.
There is no wait for Internet connectivity or time synchronization. Wi-Fi and SSH
are enabled for local access. The builder does not configure a firewall.

`payload.service` runs as `pi`, sets `LOG_LEVEL=DEBUG`, and starts:

```text
/home/pi/vcpi/.venv/bin/python /home/pi/vcpi/main.py serve --sock /run/vcpi/vcpi.sock
```

Connect with `/home/pi/vcpi/vcpi`. Audio is still started explicitly from the CLI:

```text
slot 1 vcv default
midi link 1 1
audio start
```

Check the VCV command's `result` output: loading Cardinal does not alone prove
the patch was applied. Physical audio and Cardinal loading must be checked on
the Pi; the build's WAV smoke does not verify either.

Offline-image implementation verification: source staging, builder preflight and
temporary DNS/service-policy restoration were exercised in the development
workspace. A complete privileged image build, first boot without Internet, and
physical Pi audio were not exercised there. Run the builder on a capable Linux
host, then boot the flashed Pi with WAN access disconnected to validate the
deployment end to end. This is separate from the historical realtime checks below.

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

Rebuild and flash a new image from the updated local checkout. The deployed
application is a source snapshot, not a Git clone, so `git pull` is not available
inside `/home/pi/vcpi`. Back up your sessions and custom assets before reflashing.

If manually transferring source changes to a running Pi, dependency changes
require internet (or a separately prepared package cache). Native-only changes
can use the retained build tree:

```bash
cd /home/pi/vcpi
sudo systemctl stop payload
.venv/bin/python build_native.py
sudo systemctl start payload
```

## Debugging

```bash
journalctl -u payload

# follow vcpi service logs live
journalctl -u payload -f
```

On a connected display, service logs are also mirrored to the root console
on `tty1` during startup.
