"""Shared asset discovery and path resolution for execution and completion."""

import os
from pathlib import Path

from vcpi.resources import repo_root, samples_root


def patches_root(host) -> Path:
    patches_dir = host.patches_dir
    if not patches_dir.is_absolute():
        patches_dir = Path.cwd() / patches_dir
    return patches_dir


def vst_search_dirs() -> list[Path]:
    env_tokens: list[str] = []
    for key in ("VST3_PATH", "VST_PATH"):
        raw = os.environ.get(key)
        if raw:
            env_tokens.extend(part for part in raw.split(os.pathsep) if part)

    candidates = [
        *env_tokens,
        str(Path.cwd()),
        str(repo_root()),
        "~/.vst3",
        "/usr/lib/vst3",
        "/usr/local/lib/vst3",
        "~/Library/Audio/Plug-Ins/VST3",
        "/Library/Audio/Plug-Ins/VST3",
    ]

    found: list[Path] = []
    seen: set[Path] = set()
    for token in candidates:
        path = Path(token).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists() and resolved.is_dir():
            found.append(resolved)
    return found


def iter_vst_plugin_paths() -> list[Path]:
    plugins: dict[str, Path] = {}
    for base in vst_search_dirs():
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() != ".vst3":
                continue
            plugins.setdefault(str(entry), entry)
    return sorted(plugins.values(), key=lambda p: p.name.lower())


def resolve_vst_token(token: str) -> str:
    token = token.strip()
    if not token:
        raise ValueError("VST path/name is required")

    path = Path(token).expanduser()
    if path.exists():
        return str(path)

    if token.startswith(("/", "./", "../", "~")):
        return str(path)

    key = token.lower()
    exact: list[Path] = []
    prefix: list[Path] = []

    for candidate in iter_vst_plugin_paths():
        stem = candidate.stem.lower()
        name = candidate.name.lower()

        if key in {stem, name} or (not key.endswith(".vst3") and f"{key}.vst3" == name):
            exact.append(candidate)
            continue

        if stem.startswith(key) or name.startswith(key):
            prefix.append(candidate)

    if len(exact) == 1:
        return str(exact[0])
    if len(exact) > 1:
        preview = ", ".join(sorted({p.stem for p in exact})[:5])
        raise ValueError(f"ambiguous VST name '{token}' (matches: {preview})")

    if len(prefix) == 1:
        return str(prefix[0])
    if len(prefix) > 1:
        preview = ", ".join(sorted({p.stem for p in prefix})[:5])
        raise ValueError(f"ambiguous VST name '{token}' (matches: {preview})")

    return token


def sample_pack_names() -> list[str]:
    root = samples_root()
    if not root.exists() or not root.is_dir():
        return []
    packs: list[str] = []
    for entry in root.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            packs.append(entry.name)
    return sorted(packs)


def sample_names(pack_name: str) -> list[str]:
    pack = Path(pack_name.strip().strip("/"))
    if pack.is_absolute() or ".." in pack.parts:
        return []

    pack_dir = samples_root() / pack
    if not pack_dir.exists() or not pack_dir.is_dir():
        return []

    samples: list[str] = []
    for wav in pack_dir.glob("*.wav"):
        samples.append(wav.stem)
    return sorted(samples)


def vcv_patch_names(host) -> list[str]:
    root = patches_root(host)
    if not root.exists() or not root.is_dir():
        return []

    patches: list[str] = []
    for patch_file in root.rglob("*.vcv"):
        try:
            rel = patch_file.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.lower().endswith(".vcv"):
            rel = rel[:-4]
        patches.append(rel)
    return sorted(patches)


def pd_patch_names(host) -> list[str]:
    root = patches_root(host)
    if not root.is_dir():
        return []
    return sorted(
        patch.relative_to(root).with_suffix("").as_posix()
        for patch in root.rglob("*.pd") if patch.is_file()
    )


def vst_names() -> list[str]:
    return sorted({path.stem for path in iter_vst_plugin_paths()})


def sessions_root() -> Path:
    """Return the sessions directory (CWD or repo root)."""
    cwd_sessions = Path.cwd() / "sessions"
    if cwd_sessions.exists() and cwd_sessions.is_dir():
        return cwd_sessions
    return repo_root() / "sessions"


def resolve_session_path(name: str) -> Path:
    """Resolve a session name to a full path in the sessions directory."""
    if name.lower().endswith(".json"):
        name = name[:-5]
    return sessions_root() / f"{name}.json"


def session_names() -> list[str]:
    """List available session names for tab completion."""
    root = sessions_root()
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        f.stem for f in root.glob("*.json")
        if f.is_file() and not f.name.startswith(".")
    )
