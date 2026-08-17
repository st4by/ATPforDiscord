import signal
import threading
import logging

logger = logging.getLogger(__name__)

# Event used across the app to request graceful shutdown
stop_event = threading.Event()


def _handle_signal(signum, frame):
    logger.info("Shutdown signal received: %s", signum)
    stop_event.set()


def setup_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGINT, _handle_signal)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass
