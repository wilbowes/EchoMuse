"""
em_start.py — Home Assistant add-on entrypoint

When running as a Home Assistant Supervisor add-on, options set in the add-on
UI land in /data/options.json rather than the environment. This translates
each option into the env var em_controller.py already reads (see
.env.example) and then execs into it directly, so em_controller.py itself
stays completely unaware of Home Assistant.

Outside the add-on (docker-compose, bare `python em_controller.py`),
/data/options.json doesn't exist and this is a no-op passthrough. Any env var
already set explicitly is left alone — options.json only fills in what's
missing.
"""

import json
import os
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")

# config.yaml's `options` keys map to env vars by uppercasing — kept as an
# explicit table rather than a blanket `.upper()`, so a renamed or removed
# add-on option is caught here rather than silently reaching em_controller.py
# under a name it does not read. options.json is generated from config.yaml's
# own schema, so a key missing from this table means those two files have
# drifted — a packaging bug, and one whose only other symptom is a setting
# that quietly does nothing.
OPTION_ENV_VARS = {
    "server_host": "SERVER_HOST",
    "server_ip": "SERVER_IP",
    "mdns_name": "MDNS_NAME",
    "esphome_project_version": "ESPHOME_PROJECT_VERSION",
    "esphome_port_base": "EM_ESPHOME_PORT_BASE",
    "oww_model": "OWW_MODEL",
    "oww_threshold": "OWW_THRESHOLD",
    "require_device_tls": "REQUIRE_DEVICE_TLS",
    "device_approval": "DEVICE_APPROVAL",
    "debug": "DEBUG",
}

if OPTIONS_PATH.is_file():
    options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))

    for key, value in options.items():
        env_key = OPTION_ENV_VARS.get(key)
        if env_key is None:
            # Warn rather than exit: the add-on failing to boot over one
            # stray key would be a worse outcome than the setting being
            # ignored, and this line names it in the add-on log either way.
            print(f"em_start: no env var mapped for add-on option {key!r} —"
                  " config.yaml and OPTION_ENV_VARS have drifted", flush=True)
            continue
        if value == "":
            # An unfilled required field (server_ip has no sane default) —
            # leave it unset so em_controller.py's own fallback applies
            # rather than handing it a literal empty string.
            continue
        if isinstance(value, bool):
            # em_controller.py checks `== "1"` (see REQUIRE_DEVICE_TLS in
            # .env.example) — Python's str(bool) would produce "True"/"False"
            # and silently leave every bool option permanently off.
            env_value = "1" if value else "0"
        else:
            env_value = str(value)
        os.environ.setdefault(env_key, env_value)

os.execvp("python3", ["python3", "-u", "em_controller.py"])
