"""Central configuration — loads afs-config.json > .env > defaults.

Single source of truth for all tunable parameters.
"""

import copy
import json
import os
import pathlib


VERSION = "1.1.0"

# Project root is one level above afs/ package
PROJECT_ROOT = pathlib.Path(__file__).parent.parent

DEFAULTS = {
    "models": {
        "ollama_url": "http://localhost:11434",
        "vision_model": "llava:latest",
        "text_model": "qwen3:8b",
        "vision_timeout": 180,
        "text_timeout": 120,
        "vision_ctx": 4096,
        "text_ctx": 8192,
        "keep_alive": "30m",
    },
    "processing": {
        "sanitize_images": True,
        "convert_webp": True,
        "chunk_size": 30,
        "confidence_threshold": 0.5,
    },
    "sorting": {
        "max_topics": 25,
        "max_topic_words": 2,
        "cleanup_empty_folders": True,
        "group_by_topic": [
            ".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".webp",
            ".tiff", ".tif", ".gif",
        ],
        "group_by_type": [
            ".webm", ".mp4", ".mov", ".avi", ".mkv",
            ".mp3", ".wav", ".flac", ".ogg", ".aac",
            ".psd", ".ai", ".svg", ".indd",
            ".xlsx", ".xls", ".csv", ".doc", ".docx", ".pptx", ".ppt",
            ".db", ".sqlite", ".mdb",
        ],
        "custom_folders": {},
        "folder_aliases": {},
    },
}

# Maps .env keys to config paths
_ENV_MAP = {
    "OLLAMA_URL": ("models", "ollama_url"),
    "VISION_MODEL": ("models", "vision_model"),
    "TEXT_MODEL": ("models", "text_model"),
    "VISION_TIMEOUT": ("models", "vision_timeout"),
    "TEXT_TIMEOUT": ("models", "text_timeout"),
    "VISION_CTX": ("models", "vision_ctx"),
    "TEXT_CTX": ("models", "text_ctx"),
    "KEEP_ALIVE": ("models", "keep_alive"),
    "SANITIZE_IMAGES": ("processing", "sanitize_images"),
    "CONVERT_WEBP": ("processing", "convert_webp"),
    "CHUNK_SIZE": ("processing", "chunk_size"),
    "CONFIDENCE_THRESHOLD": ("processing", "confidence_threshold"),
    "MAX_TOPICS": ("sorting", "max_topics"),
    "MAX_TOPIC_WORDS": ("sorting", "max_topic_words"),
    "CLEANUP_EMPTY_FOLDERS": ("sorting", "cleanup_empty_folders"),
}


def load_config(config_path: pathlib.Path | None = None) -> dict:
    """Load configuration with merge order: defaults < afs-config.json < .env < environ.

    Args:
        config_path: Path to JSON config file. Defaults to PROJECT_ROOT/afs-config.json.
    """
    cfg = copy.deepcopy(DEFAULTS)

    # Layer 2: afs-config.json
    json_path = config_path or (PROJECT_ROOT / "afs-config.json")
    if json_path.exists():
        try:
            overlay = json.loads(json_path.read_text(encoding="utf-8"))
            _deep_merge(cfg, overlay)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt config — use defaults

    # Layer 3: .env file (load into environ if not already set)
    _load_dotenv(PROJECT_ROOT / ".env")

    # Layer 4: environment variables override everything
    for env_key, (section, key) in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            cfg[section][key] = _coerce(val, type(DEFAULTS[section][key]))

    return cfg


def save_config(cfg: dict, config_path: pathlib.Path | None = None):
    """Write config to afs-config.json (only non-default values)."""
    json_path = config_path or (PROJECT_ROOT / "afs-config.json")
    # Only persist values that differ from defaults
    diff = _diff_from_defaults(cfg)
    json_path.write_text(json.dumps(diff, indent=2) + "\n", encoding="utf-8")


def _deep_merge(base: dict, overlay: dict):
    """Recursively merge overlay into base (mutates base)."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _diff_from_defaults(cfg: dict, defaults: dict | None = None) -> dict:
    """Return only the keys/values that differ from defaults."""
    if defaults is None:
        defaults = DEFAULTS
    diff = {}
    for key, value in cfg.items():
        if key not in defaults:
            diff[key] = value
        elif isinstance(value, dict) and isinstance(defaults.get(key), dict):
            sub = _diff_from_defaults(value, defaults[key])
            if sub:
                diff[key] = sub
        elif value != defaults.get(key):
            diff[key] = value
    return diff


def _coerce(val: str, target_type: type):
    """Coerce string env var to the target type."""
    if target_type is bool:
        return val.lower() in ("true", "1", "yes")
    if target_type is int:
        try:
            return int(val)
        except ValueError:
            return val
    if target_type is float:
        try:
            return float(val)
        except ValueError:
            return val
    return val


def _load_dotenv(env_path: pathlib.Path):
    """Load .env file into os.environ (does not override existing vars)."""
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if "#" in value:
                value = value[:value.index("#")].strip()
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass
