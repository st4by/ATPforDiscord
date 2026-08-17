"""
Модуль для импорта видео TikTok

Модуль выполняет:
- Импорт видео из JSON-файла экспорта TikTok
- Импорт лайкнутых и сохранённых видео из TikTok
- Запуск процесса скачивания
"""

import json
import itertools
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from atp import crud
from atp.database import get_db_session, run_migrations
from atp.download import download_new_videos
from atp.models import Video, VideoInfo
from atp.settings import (
    DOWNLOAD_LIKED_VIDEOS,
    DOWNLOAD_SAVED_VIDEOS,
    TIKTOK_DATA_FILE,
    TIKTOK_USER,
)
from atp.tiktok import get_user_liked_videos, get_user_saved_videos
from threading import Thread
from atp.discord import process_download_and_send

logger = logging.getLogger(__name__)

# Runtime baseline: when the importer starts, record current saved/liked IDs
# and ignore them (they were saved while the bot was offline). Only videos
# discovered after this baseline will be imported and processed.
_RUNTIME_BASELINE: dict[str, set[str]] = {"saved": set(), "liked": set()}
_RUNTIME_INITIALIZED: dict[str, bool] = {"saved": False, "liked": False}

def parse_tiktok_json_file(file: str) -> list[VideoInfo] | None:
    """Загружает список видео из JSON-файла экспорта TikTok.

    :param file: Путь к JSON-файлу с данными экспорта

    :return: Список объектов VideoInfo
    """
    with open(file, encoding="utf-8") as f:
        data = json.load(f)

    try:
        activity = (
            data.get("Likes and Favorites") or data.get("Your Activity") or data.get("Activity")
        )
        saved_videos = (
            activity["Favorite Videos"]["FavoriteVideoList"] if DOWNLOAD_SAVED_VIDEOS else []
        )
        liked_videos = (
            activity["Like List"]["ItemFavoriteList"] if DOWNLOAD_LIKED_VIDEOS else []
        )  # fmt: skip
    except (KeyError, TypeError) as e:
        logger.error("JSON error: %s", e)
        return None

    videos: dict[str, VideoInfo] = {}
    for source_videos, liked, saved in (
        (liked_videos, True, False),
        (saved_videos, False, True),
    ):
        for video in source_videos:
            date_str = video.get("date") or video["Date"]
            video_link = video.get("link") or video["Link"]

            date = datetime.fromisoformat(date_str)
            video_id = video_link.split("/")[-2]
            info = videos.get(video_id)
            if info:
                info.liked = info.liked or liked
                info.saved = info.saved or saved
            else:
                videos[video_id] = VideoInfo(id=video_id, date=date, liked=liked, saved=saved)

    return sorted(videos.values(), key=lambda v: v.date)


def import_from_file() -> None:
    db = get_db_session()

    try:
        db_videos: dict[str, Video] = {v.id: v for v in crud.get_videos(db)}

        if not os.path.exists(TIKTOK_DATA_FILE):
            if db_videos:
                logger.info("File %s does not exist, skipping import", Path(TIKTOK_DATA_FILE).name)
            else:
                logger.error(
                    "Cannot import video from file: %s does not exist\n"
                    "Please request your data from TikTok and extract %s to config/ directory\n"
                    "https://github.com/skrepkaq/ATP#экспорт-данных-из-tiktok",
                    Path(TIKTOK_DATA_FILE).name,
                    Path(TIKTOK_DATA_FILE).name,
                )
            return

        videos = parse_tiktok_json_file(TIKTOK_DATA_FILE)
        if not videos:
            logger.warning(
                "No videos were imported from %s\n"
                "Check DOWNLOAD_SAVED_VIDEOS/DOWNLOAD_LIKED_VIDEOS settings "
                "or re-request ALL your data from TikTok\n"
                "https://github.com/skrepkaq/ATP#экспорт-данных-из-tiktok",
                Path(TIKTOK_DATA_FILE).name,
            )
            return

        try:
            videos_to_add: list[VideoInfo] = []
            videos_to_update: list[VideoInfo] = []

            for video in videos:
                db_video = db_videos.get(video.id)
                if not db_video:
                    videos_to_add.append(video)
                elif (video.liked and not db_video.liked) or (video.saved and not db_video.saved):
                    # Никогда не обновляем liked и saved на False
                    videos_to_update.append(video)
            if videos_to_add:
                crud.add_videos_bulk(db, videos_to_add)
                logger.info("Added %s videos", len(videos_to_add))
                for v in videos_to_add:
                    if v.saved:
                        Thread(target=process_download_and_send, args=(v.id,), daemon=True).start()
            if videos_to_update:
                crud.update_video_sources_bulk(db, videos_to_update)
                logger.info("Updated sources for %s videos", len(videos_to_update))
                for v in videos_to_update:
                    if v.saved:
                        Thread(target=process_download_and_send, args=(v.id,), daemon=True).start()
        except Exception as e:
            logger.exception("Error importing videos: %s", e)

    except Exception as e:
        logger.exception("Error importing from file: %s", e)
    finally:
        db.close()


def import_from_tiktok_source(
    importer: Callable[[str], list[dict]], source: Literal["liked", "saved"]
) -> None:
    """Импортирует видео из источника TikTok.
    :param importer: Функция для получения списка видео
    :param source: Источник видео (liked или saved)

    Импортируем до тех пор пока видео не закончатся
    или пока не наткнёмся на 10 видео подряд которые уже есть в БД с тем же источником
      (видео уже были импортированы как лайкнутые/сохранённые)
    или пока не наткнёмся на 100 видео подряд которые уже есть в БД
      (вероятно видео уже были импортированы, но c другим/без источника.
       Теоретически может сломаться если сохранить N видео, потом одновременно
       лайкнуть и сохранить 100 видео, не лайкать больше ничего и запуcтить импорт.
       Тогда те самые N видео не будут импортированы.
       Если бы мы импортировали пока все статусы не будут актуальны
       первый импорт, когда у видео нет статуса, занял бы вечность)
    """
    db = get_db_session()

    try:
        # Build current DB index
        videos = {v.id: v for v in crud.get_videos(db)}
        existing_videos: set[str] = set(videos.keys())
        existing_same_source_videos: set[str] = {
            id for id, video in videos.items() if getattr(video, source) is True
        }

        # If this is the first run for this source, record the current remote
        # saved/liked IDs as baseline and do NOT import them (ignore backlog).
        if not _RUNTIME_INITIALIZED[source]:
            try:
                baseline_ids: list[str] = [v["id"] for v in importer(TIKTOK_USER)]
            except Exception as e:
                logger.exception("Failed to initialize %s baseline: %s", source, e)
                return
            _RUNTIME_BASELINE[source] = set(baseline_ids)
            _RUNTIME_INITIALIZED[source] = True
            logger.info(
                "Initialized runtime baseline for %s: %s items (backlog will be ignored)",
                source,
                len(_RUNTIME_BASELINE[source]),
            )
            return

        new_videos: list[str] = []
        recent_ids_window: list[str] = []
        for video in importer(TIKTOK_USER):
            video = VideoInfo(
                id=video["id"],
                date=datetime.fromtimestamp(video["timestamp"]),
                liked=source == "liked",
                saved=source == "saved",
            )
            new_videos.append(video.id)
            recent_ids_window.append(video.id)

            # If the video existed before runtime baseline and was saved earlier,
            # ignore it.
            if video.id in _RUNTIME_BASELINE[source]:
                continue

            if video.id not in existing_videos:
                logger.info("Importing new runtime %s video %s", source, video.id)
                crud.add_video_to_db(
                    db,
                    video.id,
                    video.date,
                    liked=video.liked,
                    saved=video.saved,
                )
                # After importing, mark as seen to avoid re-importing in this run
                _RUNTIME_BASELINE[source].add(video.id)
                if video.saved:
                    Thread(target=process_download_and_send, args=(video.id,), daemon=True).start()
            elif video.id not in existing_same_source_videos:
                logger.info("Updating video sources for %s", video.id)
                crud.update_video(
                    db,
                    videos[video.id],
                    update_last_checked=False,
                    liked=video.liked,
                    saved=video.saved,
                )

            # Exit heuristics: if recent window contains only already-known items,
            # we assume nothing new below and stop to avoid long scans.
            if len(recent_ids_window) >= 10 and set(recent_ids_window[-10:]).issubset(
                existing_same_source_videos.union(_RUNTIME_BASELINE[source])
            ):
                logger.info("No new %s videos, exiting", source)
                return
            if len(recent_ids_window) >= 100 and set(recent_ids_window[-100:]).issubset(
                existing_videos.union(_RUNTIME_BASELINE[source])
            ):
                logger.info("No new %s videos, exiting (long import)", source)
                return
    except Exception as e:
        logger.exception("Error importing %s videos from TikTok: %s", source, e)
    finally:
        db.close()


def import_from_tiktok() -> None:
    if DOWNLOAD_LIKED_VIDEOS:
        import_from_tiktok_source(get_user_liked_videos, "liked")
    if DOWNLOAD_SAVED_VIDEOS:
        import_from_tiktok_source(get_user_saved_videos, "saved")


def import_from_tiktok_poll_saved(limit: int = 30) -> None:
    """Fast poll for saved videos used by frequent scheduler.

    This limits the number of entries fetched from TikTok to avoid
    scanning the user's entire saved history on every poll.
    """
    logger.info("Polling saved videos (fast mode, limit=%s)", limit)

    # If runtime baseline is not initialized for saved videos, perform a
    # one-time full fetch to establish the baseline. We do not process or
    # download videos during this baseline initialization — we only record
    # the current saved IDs so that older/backlog items are ignored.
    if not _RUNTIME_INITIALIZED["saved"]:
        try:
            logger.info("Initializing full saved baseline (may take a while)...")
            entries = get_user_saved_videos(TIKTOK_USER)
            ids = []
            if entries:
                if isinstance(entries, list):
                    ids = [v["id"] for v in entries]
                else:
                    ids = [v["id"] for v in entries]
            if not ids:
                logger.info(
                    "Full baseline fetch returned 0 items — will retry on next poll"
                )
                return
            _RUNTIME_BASELINE["saved"] = set(ids)
            _RUNTIME_INITIALIZED["saved"] = True
            logger.info(
                "Initialized runtime baseline for saved: %s items (backlog will be ignored)",
                len(_RUNTIME_BASELINE["saved"]),
            )
            return
        except Exception as e:
            logger.exception("Failed to initialize saved baseline: %s", e)
            return

    def limited_importer(username: str) -> list[dict]:
        try:
            entries = get_user_saved_videos(username)
            # If the extractor returns a generator or huge iterable, slice to limit
            if entries is None:
                return []
            # If it's a list, slicing is cheap
            if isinstance(entries, list):
                return entries[:limit]
            # Otherwise treat it as an iterable/generator and use islice
            return list(itertools.islice(entries, limit))
        except Exception as e:
            logger.exception("Error during fast poll importer: %s", e)
            return []

    import_from_tiktok_source(limited_importer, "saved")


def deprecated_run() -> None:
    """
    Обратная совместимость для запуска через
    python -um atp.import_from_file и python -m atp --download-from-file
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(levelname)s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    logger.warning(
        "Deprecated run method!\n"
        "No more need to do `docker compose up atp-from-file`\n"
        "Import from file now works on `docker compose up`\n"
        "\nPlease remove `atp-from-file` service from compose.yaml\n"
        "Or download a new version from https://github.com/skrepkaq/ATP/blob/master/compose.yaml\n"
        "And just run `docker compose up`\n"
        "\nOld run method will still work, but it's deprecated and will be removed in the future"
    )
    time.sleep(5)
    run_migrations()
    import_from_file()
    download_new_videos()


if __name__ == "__main__":
    deprecated_run()
