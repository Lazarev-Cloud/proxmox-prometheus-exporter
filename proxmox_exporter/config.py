import logging
import os

EXPORTER_PORT = int(os.environ.get('EXPORTER_PORT', 9101))
COLLECTION_INTERVAL = int(os.environ.get('COLLECTION_INTERVAL', 15))
DEBUG_MODE = os.environ.get('DEBUG_MODE', '').lower() in ('true', '1', 'yes')
PARALLEL_COLLECTORS = os.environ.get('PARALLEL_COLLECTORS', 'true').lower() in ('true', '1', 'yes')
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 4))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s:%(funcName)s:%(lineno)d] %(message)s'
)

logger = logging.getLogger(__name__)
if DEBUG_MODE:
    logger.setLevel(logging.DEBUG)
