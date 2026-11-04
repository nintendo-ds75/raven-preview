"""Raven: a local judgment inbox for coding agents."""
import os as _os

__version__ = "0.1.0"

# Raven was called Bridge, and its settings were BRIDGE_*. An environment or
# .env written before the rename keeps working: each BRIDGE_X fills RAVEN_X
# when RAVEN_X is unset. Child processes (hosts, hooks) inherit the result.
for _key, _value in list(_os.environ.items()):
    if _key.startswith("BRIDGE_"):
        _os.environ.setdefault("RAVEN_" + _key[len("BRIDGE_"):], _value)


def data_path(*parts: str) -> str:
    """A path under the local data folder, `.raven/`. A checkout that ran
    before the rename has `.bridge/` and no `.raven/`: keep using it, so
    its database, voice models and settings are not left behind."""
    base = ".raven"
    if not _os.path.exists(base) and _os.path.isdir(".bridge"):
        base = ".bridge"
    return _os.path.join(base, *parts)
