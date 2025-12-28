import os
import signal
import sys

from .config import logger
from .exporter import EnhancedProxmoxExporter


def _signal_handler(signum, frame):
    logger.info(f"Received signal {signum}")
    sys.exit(0)


def main():
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        if os.geteuid() != 0:
            logger.warning("Not running as root. Some metrics may be unavailable.")
            logger.warning("For full functionality, run with: sudo python3 %s" % sys.argv[0])

        exporter = EnhancedProxmoxExporter()
        exporter.run()
    except KeyboardInterrupt:
        logger.info("Exporter stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == '__main__':
    main()
