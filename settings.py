"""Settings shared by the receiver, the CLI and the MCP server.

Two layers live apart on purpose. The engine (this checkout) ships defaults in
config.yaml and prompts/. The FLOW SPEC is a separate directory, named by
SPEC_DIR in .env, that holds one team's process: its rules.toml, prompts and a
config.yaml with board columns and sticker states. Nothing about a particular
board is hardcoded in the engine; swapping SPEC_DIR swaps the process.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("board-agent")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

# The flow spec. Empty = engine defaults only (rules.toml and prompts/ here).
SPEC_DIR = (Path(os.environ["SPEC_DIR"]).expanduser()
            if os.environ.get("SPEC_DIR", "").strip() else None)
STATE_DIR = ROOT / os.environ.get("STATE_DIR", "state")


# --- Settings ----------------------------------------------------------------
# Seventy flat environment variables is a lot of surface for something whose
# knobs group naturally (rotation, handoff, history, turns). config.yaml gives
# them that shape; config.local.yaml overrides it per machine and is not
# committed. Environment variables still win over both, so a one-off override
# needs no file edit and every existing .env keeps working unchanged.
#
# Secrets stay in .env deliberately: config.yaml is meant to be committable.

CONFIG_FILE = ROOT / os.environ.get("CONFIG_FILE", "config.yaml")
CONFIG_LOCAL_FILE = ROOT / os.environ.get("CONFIG_LOCAL_FILE", "config.local.yaml")
CONFIG_SPEC_FILE = SPEC_DIR / "config.yaml" if SPEC_DIR else None


class _StrictBoolLoader(yaml.SafeLoader):
    """YAML 1.1 reads `no`, `off` and `yes` as booleans.

    That is a live hazard here: `language: no` is Norwegian, not False, and the
    same goes for locale lists and two-letter codes generally. Only `true` and
    `false` stay boolean; everything else that looks like a word stays a string.
    """


_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
for _ch in list("yYnNoO"):
    _StrictBoolLoader.yaml_implicit_resolvers.setdefault(_ch, [])
    _StrictBoolLoader.yaml_implicit_resolvers[_ch] = [
        (tag, rx) for tag, rx in _StrictBoolLoader.yaml_implicit_resolvers[_ch]
        if tag != "tag:yaml.org,2002:bool"
    ]


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_config() -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for path in (CONFIG_FILE, CONFIG_SPEC_FILE, CONFIG_LOCAL_FILE):
        if path is None or not path.exists():
            continue
        try:
            with path.open("rb") as f:
                data = yaml.load(f, Loader=_StrictBoolLoader) or {}
        except (OSError, yaml.YAMLError):
            log.exception("config %s is unreadable — ignoring it", path)
            continue
        if not isinstance(data, dict):
            log.warning("config %s is not a mapping — ignoring it", path)
            continue
        merged = _deep_merge(merged, data)
        log.info("config: loaded %s", path)
    return merged


CONFIG: Dict[str, Any] = _load_config()


def conf(path: str, env: str, default: Any, cast: Any = str) -> Any:
    """One setting, resolved: environment > config.local.yaml > spec config.yaml
    > engine config.yaml > default.

    `path` is dotted ("rotation.turn_cost_usd"); `env` is the variable that has
    always set it. Keeping both means no existing deployment has to change and
    nothing has to be looked up in two places to know what is in effect.
    """
    raw = os.environ.get(env)
    if raw is None:
        node: Any = CONFIG
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        raw = node
    if raw is None:
        return default
    try:
        if cast is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
        if cast is list:
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            return [x.strip() for x in str(raw).split(",") if x.strip()]
        return cast(raw)
    except (TypeError, ValueError):
        log.warning("config %s: %r is not a valid %s — using %r",
                    path, raw, getattr(cast, "__name__", cast), default)
        return default
