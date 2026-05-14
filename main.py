import logging
import os
import sys

from src.shared.processes.constants import SUPPORTED_APP_MODES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("main")


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
        from danger_detection_stream import main as run
        run()

    elif mode == "health_monitoring":
        logger.critical("Health monitoring pipeline is not yet implemented.")
        sys.exit(1)

    else:
        logger.critical(
            f"Unknown APP_MODE '{mode}'. "
            f"Supported values: {SUPPORTED_APP_MODES}."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
