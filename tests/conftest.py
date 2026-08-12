"""Test helpers shared across the suite.

`api.py` deliberately depends on nothing but `requests`, so its tests must not
need a Home Assistant install to run. Importing it the normal way would execute
`custom_components/huawei_ont/__init__.py`, which does pull Home Assistant in,
so load the module straight off disk instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "huawei_ont"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Home Assistant does not import on Windows (homeassistant.runner needs fcntl),
# so the platform tests cannot be collected there. CI runs on Linux and does
# collect them; say so out loud rather than quietly running half a suite.
try:
    import homeassistant.runner  # noqa: F401

    HA_AVAILABLE = True
except ImportError:
    HA_AVAILABLE = False
    collect_ignore = ["test_device_tracker.py"]


def pytest_report_header(config):
    if not HA_AVAILABLE:
        return (
            "Home Assistant unavailable — tests/test_device_tracker.py was NOT "
            "collected. Run the full suite on Linux (or WSL)."
        )
    return None


def load_standalone(name: str):
    """Import a module of the integration without running its package init.

    Only works for modules that use no relative imports — `api` and `oui`.
    """
    module_name = f"_huawei_ont_standalone_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, COMPONENT_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
