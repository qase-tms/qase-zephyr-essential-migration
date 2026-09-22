import os
import sys

from src.support.config_manager import ConfigManager
from src.support.logger import Logger
from src.importer import Importer

# Python 3.11 minimum. asyncio.TaskGroup is used by the
# entity importers and does not exist before 3.11; 3.10 reaches end of life in
# October 2026. Fail here rather than partway into a run.
if sys.version_info < (3, 11):
    sys.exit(
        f"This migration requires Python 3.11 or newer "
        f"(found {sys.version_info.major}.{sys.version_info.minor})."
    )


# Usage: python start.py [config.json] [--dry-run]
#   --dry-run  read everything from Zephyr Essential and report exactly what
#              would be created, without writing anything to Qase
_args = [a for a in sys.argv[1:] if a != "--dry-run"]
_dry_run = "--dry-run" in sys.argv[1:] or bool(str(os.environ.get("QASE_DRY_RUN") or "").strip())

# Config file: first positional arg, or QASE_CONFIG_FILE / MIGRATION_CONFIG, else ./config.json
_config_path = (
    (_args[0] if _args else None)
    or os.environ.get("QASE_CONFIG_FILE")
    or os.environ.get("MIGRATION_CONFIG")
    or "./config.json"
)

if not os.path.exists(_config_path):
    print(f"❌ Config file not found: {_config_path}")
    print("   Copy config.example.json to config.json and fill in your tokens,")
    print("   then run `python preflight.py` to validate before migrating.")
    sys.exit(1)

config = ConfigManager(config_file=_config_path)
config.load_config()

# Fail fast on an empty/unparseable config instead of 401ing mid-run
_required = ["qase.api_token", "zephyr.access_token"]
_missing = [key for key in _required if not str(config.get(key) or "").strip()]
if _missing:
    print(f"❌ Config is missing required key(s): {', '.join(_missing)}")
    print("   Run `python preflight.py` for a full validation report.")
    sys.exit(1)

prefix = config.get("prefix") or "zephyr-essential"

logger = Logger(
    level=config.get("logging.level"),
    write_to_file=config.get("logging.write_to_file") is not False,
    log_dir=config.get("logging.dir") or "./logs",
    prefix=prefix,
)

importer = Importer(config, logger, dry_run=_dry_run)
importer.start()
