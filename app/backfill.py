"""Pull a longer stretch of history into the database once, alongside the running server.

  docker exec garmin-mcp python -m app.backfill 365
"""

import logging
import sys

from .config import Config
from .db import Database
from .sources import make_source
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
cfg = Config.from_env()
db = Database(cfg.db_path)
result = Syncer(db, make_source(cfg.source, cfg.garmin_api_key), 0, 0, 0).run(days)
print(result)
sys.exit(0 if result.get("ok") else 1)
