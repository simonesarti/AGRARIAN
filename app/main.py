import logging
import os
import sys
from pathlib import Path

from app.shared.processes.constants import SUPPORTED_APP_MODES

# ================================================================

Path('./logs').mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        #logging.StreamHandler(),
        logging.FileHandler('./logs/main.log', mode='w'),
    ],
)
logger = logging.getLogger("main")

# ================================================================


def main() -> None:
    mode = os.environ.get("APP_MODE", "").strip().lower()

    if not mode:
        logger.critical(
            "APP_MODE environment variable is not set. "
            f"Supported values: {SUPPORTED_APP_MODES}."
        )
        sys.exit(1)

    if mode == "danger_detection":
        logger.info("Starting danger detection pipeline.")
        from app.danger_detection_stream import main as run
        run()

    elif mode == "health_monitoring":
        logger.info("Starting health monitoring pipeline.")
        from app.health_monitoring_stream import main as run
        run()

    else:
        logger.critical(
            f"Unknown APP_MODE '{mode}'. "
            f"Supported values: {SUPPORTED_APP_MODES}."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
