#!/bin/bash
# Build-time provisioning inside the mounted ARM64 image, never on first boot.
# prepare-image.sh supplies the checkout, DNS and a service-start policy.
set -euo pipefail

# Both are supplied only by prepare-image.sh inside the mounted target root.
# This is an accidental-host-execution guard, not a security boundary.
if [ "${VCPI_IMAGE_BUILD:-}" != 1 ] || [ ! -f /root/.vcpi-image-build ]; then
  echo "ERROR: internal image provisioner; run rpi-build/prepare-image.sh instead" >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-2}"

PROJECT_DIR=/home/pi/vcpi
CARDINAL_VERSION="${CARDINAL_VERSION:-26.01}"
CARDINAL_ARCHIVE="Cardinal-linux-aarch64-${CARDINAL_VERSION}.tar.gz"
CARDINAL_URL="${CARDINAL_URL:-https://github.com/DISTRHO/Cardinal/releases/download/${CARDINAL_VERSION}/${CARDINAL_ARCHIVE}}"

if [ "$EUID" -ne 0 ] || [ "$(dpkg --print-architecture)" != arm64 ]; then
  echo "ERROR: target provisioning requires root inside an ARM64 Raspberry Pi OS image" >&2
  exit 1
fi
if [ ! -f "$PROJECT_DIR/requirements.txt" ] || [ ! -f "$PROJECT_DIR/build_native.py" ]; then
  echo "ERROR: prepare-image.sh must copy the current checkout to $PROJECT_DIR" >&2
  exit 1
fi
if [ -z "${VCPI_USERCONF:-}" ] || [ "${VCPI_USERCONF%%:*}" != pi ] || [ "${VCPI_USERCONF#*:}" = "$VCPI_USERCONF" ] || [ -z "${VCPI_USERCONF#*:}" ]; then
  echo 'ERROR: VCPI_USERCONF must contain pi:<encrypted-password-hash>' >&2
  exit 1
fi

install_cardinal_vst3() {
  local tmp_dir archive_path extract_dir cardinal_dir binary dependencies
  tmp_dir="$(mktemp -d /var/tmp/cardinal.XXXXXX)"
  trap "rm -rf '$tmp_dir'" EXIT
  archive_path="$tmp_dir/$CARDINAL_ARCHIVE"
  extract_dir="$tmp_dir/extract"
  mkdir -p "$extract_dir"

  echo "Downloading Cardinal VST3: $CARDINAL_URL"
  curl -fL --retry 6 --retry-delay 2 --retry-all-errors "$CARDINAL_URL" -o "$archive_path"
  tar -xzf "$archive_path" -C "$extract_dir"
  cardinal_dir="$(find "$extract_dir" -type d -name Cardinal.vst3 -print -quit)"
  if [ -z "$cardinal_dir" ]; then
    echo "ERROR: full Cardinal.vst3 not found in $CARDINAL_ARCHIVE" >&2
    exit 1
  fi

  mkdir -p /usr/local/lib/vst3
  rm -rf /usr/local/lib/vst3/Cardinal.vst3
  # The official release includes the full module resources within the bundle.
  cp -aL "$cardinal_dir" /usr/local/lib/vst3/Cardinal.vst3
  chmod -R a+rX /usr/local/lib/vst3/Cardinal.vst3
  binary="$(find /usr/local/lib/vst3/Cardinal.vst3 -type f -path '*/aarch64-linux/*.so' -print -quit)"
  if [ -z "$binary" ]; then
    echo "ERROR: Cardinal bundle has no ARM64 VST3 binary" >&2
    exit 1
  fi
  dependencies="$(ldd "$binary")"
  if [[ "$dependencies" == *"not found"* ]]; then
    printf 'ERROR: Cardinal runtime libraries are missing:\n%s\n' "$dependencies" >&2
    exit 1
  fi
  rm -rf "$tmp_dir"
  trap - EXIT
}

# Install only what provisioning/runtime need; do not upgrade the entire image.
# policy-rc.d installed by prepare-image.sh prevents package service starts here.
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git openssh-server sudo \
  build-essential cmake pkg-config \
  python3-dev python3-pip python3-venv \
  libx11-dev libxext-dev libxinerama-dev libxrandr-dev libxcursor-dev \
  libfreetype6-dev libfontconfig1-dev \
  libjack-jackd2-dev libsndfile1-dev libavahi-client-dev libudev-dev \
  libasound2-dev libreadline-dev libportaudio2 portaudio19-dev jackd2 \
  libgl1 libegl1 libgles2 libglu1-mesa libsdl2-2.0-0 liblo7 libdbus-1-3

for group in audio plugdev sudo; do
  groupadd --force "$group"
done
if ! id pi >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash pi
fi
usermod -aG audio,plugdev,sudo pi
printf '%s\n' "$VCPI_USERCONF" | chpasswd --encrypted
unset VCPI_USERCONF

# Setting the password alone leaves Raspberry Pi OS's first-boot wizard enabled.
# Finish account setup now, including console/desktop and SSH wizard cleanup.
/usr/bin/cancel-rename pi

install -d -o pi -g pi /home/pi
printf '%s\n' '/usr/bin/jackd -P75 -p16 -dalsa -dhw:0 -p1024 -n3' > /home/pi/.jackdrc
chown pi:pi /home/pi/.jackdrc

# No ufw enable: chroot shares the build host's networking/netfilter namespace.
# Realtime scheduling is granted by the installed service's LimitRTPRIO setting.
install_cardinal_vst3

cd "$PROJECT_DIR"
python3 -m venv "$PROJECT_DIR/.venv"
PYTHON="$PROJECT_DIR/.venv/bin/python"
"$PYTHON" -m pip install --no-cache-dir --upgrade pip
"$PYTHON" -m pip install --no-cache-dir -r "$PROJECT_DIR/requirements.txt"
cp "$PROJECT_DIR/requirements.txt" "$PROJECT_DIR/.venv/.vcpi-requirements.txt"
"$PYTHON" "$PROJECT_DIR/build_native.py"

# Keep the build cache and fetched sources for offline incremental ./vcpid runs.
# build_native.py reconfigures each time; suppress FetchContent remote updates.
"$PYTHON" - <<'PY'
from pathlib import Path
import platform
import subprocess
import sys

root = Path.cwd()
target = f"{sys.platform}-{platform.machine()}-{sys.implementation.cache_tag}"
subprocess.run([
    "cmake", "-S", str(root / "native"), "-B", str(root / "build" / target),
    "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
], check=True)
PY

chown -R pi:pi "$PROJECT_DIR"

# Run as the eventual service user, without needing a device, display or network.
# Exercise the installed native extension, MIDI, mmap handoff and engine mixer.
runuser -u pi -- "$PYTHON" - <<'PY'
from pathlib import Path
import struct
import tempfile
import wave

import aalink
import mido
import numpy as np
import prompt_toolkit
import rich
import rtmidi
from core.audio_deps import require_native

native = require_native()
with tempfile.TemporaryDirectory(prefix="vcpi-image-smoke-") as directory:
    root = Path(directory)
    sample_path = root / "sample.wav"
    with wave.open(str(sample_path), "wb") as sample:
        sample.setnchannels(1)
        sample.setsampwidth(2)
        sample.setframerate(44100)
        sample.writeframes(struct.pack("<h", 4096) * 4096)

    transport = native.Transport(str(root / "audio.mmap"), 64, True)
    plugin = native.Plugin(str(sample_path), 44100, 64, "wav")
    engine = native.Engine(44100, 64)
    runner = None
    try:
        runner = native.SlotRunner(transport, plugin, 44100, 64)
        engine.attach(0, transport, 0.5, False, False)
        transport.send_midi(bytes(mido.Message("note_on", note=60, velocity=127).bytes()))
        # Fixed one-block pipeline: the first call requests, the next consumes.
        np.testing.assert_array_equal(engine.render_block(), np.zeros((64, 2), dtype=np.float32))
        runner.barrier()
        rendered = engine.render_block()
        np.testing.assert_array_equal(rendered, np.full((64, 2), 0.0625, dtype=np.float32))
        if engine.stats()["render_errors"] or transport.stats()["failure"]:
            raise RuntimeError("Native image smoke reported a render failure")
        print("Target dependency imports and native offline WAV rendering passed")
    finally:
        engine.shutdown()
        if runner is not None:
            runner.shutdown()
    del runner, engine, plugin, transport
PY

apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache/pip /home/pi/.cache/pip

echo "Build-time target provisioning complete"
