"""EchoMuse Controller startup launcher.

When running as a Home Assistant add-on, reads user options from
``/data/options.json`` and exposes them as environment variables compatible
with EchoMuse's standard ``.env`` configuration. Explicit environment
variables take precedence, allowing the same container image to run under
Docker Compose or other non-Home-Assistant deployments.

Executes the EchoMuse controller after configuration is prepared.
"""
import json
import os
from pathlib import Path

options_path = Path("/data/options.json")

if options_path.is_file():
    with options_path.open(encoding="utf-8") as f:
        options = json.load(f)

    for key, value in options.items():
        # These names match your existing .env.example:
        # server_host -> SERVER_HOST
        env_key = key.upper()

        if isinstance(value, bool):
            env_value = str(value).lower()
        else:
            env_value = str(value)

        # Do not overwrite an explicitly supplied environment variable.
        os.environ.setdefault(env_key, env_value)

os.execvp("python3", ["python3", "/app/em_controller.py"])