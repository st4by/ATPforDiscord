import argparse
import logging
import sys
import time

import schedule
from atp.stop import setup_signal_handlers, stop_event
import threading

from atp import crud
from atp.check_availability import check_video_batch
from atp.database import get_db_session, run_migrations
from atp.download import download_new_videos
from atp.settings import (
    COOKIES_FILE,
    DOWNLOAD_LIKED_VIDEOS,
    DOWNLOAD_SAVED_VIDEOS,
    TIKTOK_USER,
    PROCESS_BACKLOG_ON_STARTUP,
)
from atp.telegram import discover_chat_id
from atp.video_import import import_from_file, import_from_tiktok
from atp.tiktok import get_user_saved_videos

logger = logging.getLogger(__name__)


def run_download_from_file() -> None:
    """Импортирует видео из json файла и скачивает их"""
    import_from_file()
    download_new_videos()


def run_download_from_tiktok() -> None:
    """Импортирует видео из TikTok и скачивает их"""
    import_from_tiktok()
    download_new_videos()


def run_scheduler() -> None:
    """Основной цикл работы приложения"""
    run_migrations()
    discover_chat_id()
    # By default we skip importing/downloading the backlog on startup to avoid
    # mass-downloads and rate-limiting issues. Enable via settings.conf if needed.
    if PROCESS_BACKLOG_ON_STARTUP:
        run_download_from_file()
    else:
        logger.info(
            "Skipping initial import/download from file (PROCESS_BACKLOG_ON_STARTUP=false)"
        )

    db = get_db_session()
    videos = crud.get_videos(db)
    db.close()
    # If we skipped processing the backlog on startup, don't abort the whole
    # application when the DB is empty — continue running and wait for scheduled imports.
    if not videos and PROCESS_BACKLOG_ON_STARTUP:
        logger.error(
            "No videos were imported! Cannot start the archiver!\n"
            "Please fix the errors above and restart the application"
        )
        sys.exit(1)

    schedule.every().hour.at("00:00").do(check_video_batch)

    if not TIKTOK_USER:
        logger.warning("TIKTOK_USER is missing! Importing videos from TikTok is disabled")
    elif not DOWNLOAD_LIKED_VIDEOS and not DOWNLOAD_SAVED_VIDEOS:
        logger.warning(
            "DOWNLOAD_LIKED_VIDEOS and DOWNLOAD_SAVED_VIDEOS are disabled! "
            "Skipping import from TikTok"
        )
    else:
        if DOWNLOAD_SAVED_VIDEOS and not COOKIES_FILE:
            logger.warning(
                "DOWNLOAD_SAVED_VIDEOS is enabled, but COOKIES_FILE is missing!\n"
                "For more information please visit https://github.com/skrepkaq/ATP#cookies"
            )

        schedule.every().hour.at("30:00").do(run_download_from_tiktok)
        # Poll saved videos frequently for new runtime saves and process them
        # immediately. Use a lock to avoid overlapping runs if a previous
        # poll is still executing.
        saved_poll_lock = threading.Lock()

        def _poll_saved_once() -> None:
            if not DOWNLOAD_SAVED_VIDEOS or not TIKTOK_USER:
                return
            if not saved_poll_lock.acquire(blocking=False):
                return
            try:
                # Import only saved videos; the importer will initialize runtime
                # baseline on first run and spawn download/send threads for new items.
                from atp.video_import import import_from_tiktok_poll_saved

                import_from_tiktok_poll_saved(limit=30)
            finally:
                saved_poll_lock.release()

        schedule.every(15).seconds.do(_poll_saved_once)

    logger.info("ATP archiver has been started!")
    # Setup graceful shutdown handlers and run main loop
    setup_signal_handlers()
    try:
        while not stop_event.is_set():
            schedule.run_pending()
            time.sleep(1)
    finally:
        logger.info("ATP archiver is shutting down")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download-from-file",
        action="store_true",
        help="Import videos from json file and download them",
    )
    args = parser.parse_args()

    if args.download_from_file:
        from atp.video_import import deprecated_run

        return deprecated_run()

    run_scheduler()


if __name__ == "__main__":
    main()
