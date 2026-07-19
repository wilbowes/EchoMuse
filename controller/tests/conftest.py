"""
Controller unit tests cover the pure-logic modules only (em_eq,
em_scenes, em_oww_models, version) — nothing that needs openwakeword,
aiohttp, a database, or a device. Run from anywhere:

    cd controller && python -m pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
