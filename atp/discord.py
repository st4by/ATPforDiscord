import logging
from pathlib import Path
import requests

from atp.database import get_db_session
from atp import crud
from atp.models import Video
from atp import tiktok
from atp.settings import (
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    DISCORD_MAX_VIDEO_SIZE,
    DOWNLOADS_DIR,
)

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)


def _post_message(content: str) -> dict:
    """Post a plain message to the configured Discord channel."""
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    resp = requests.post(url, headers=headers, data={"content": content}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_media(caption: str, file_path: Path) -> dict:
    """Send a file to the Discord channel. If file is too large, post a warning message instead.

    Raises: Exception on misconfiguration or HTTP errors.
    Returns: Response JSON or a dict with warning info when file is too large.
    """
    if not is_configured():
        raise Exception("Discord parameters not configured (token or channel id)")

    if not file_path.exists():
        raise Exception(f"File does not exist: {file_path}")

    size = file_path.stat().st_size
    if size > DISCORD_MAX_VIDEO_SIZE:
        # Post a warning message instead of uploading
        content = f"Не смог отправить видео {file_path.stem}: размер {size} байт > {DISCORD_MAX_VIDEO_SIZE} байт"
        return _post_message(content)

    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

    with open(file_path, "rb") as fh:
        files = {"file": (file_path.name, fh)}
        data = {"content": caption or ""}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=180)

    if resp.status_code not in (200, 201):
        raise Exception(f"Failed to send Discord media: {resp.status_code} {resp.text}")
    return resp.json()


def process_download_and_send(video_id: str) -> None:
    """Download the video and send to Discord if not already sent.

    This function opens a DB session, queries the `Video` record, attempts download via
    `tiktok.download_video`, updates DB fields, and sends the resulting file to Discord.
    """
    db = get_db_session()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if video is None:
            logger.error("Video %s not found in DB", video_id)
            return

        if video.sent_to_discord:
            logger.info("Video %s already sent to Discord, skipping", video_id)
            return

        # Attempt to download
        info = None
        try:
            info = tiktok.download_video(video)
        except Exception as e:
            logger.exception("Error downloading video %s: %s", video_id, e)

        if info:
            status = "success" if not info.deleted_reason else "failed"
            try:
                crud.update_video(
                    db,
                    video,
                    status=status,
                    name=info.name,
                    author=info.author,
                    type=info.type,
                    deleted_reason=info.deleted_reason,
                )
            except Exception:
                logger.exception("Failed to update DB for video %s", video_id)

        target = Path(DOWNLOADS_DIR) / f"{video_id}.mp4"
        if not target.exists():
            logger.warning("Downloaded file %s not found for video %s", target, video_id)
            return

        try:
            caption = f"{video.author or ''} — {video.name or video_id}"
            send_media(caption, target)
            crud.update_video(db, video, sent_to_discord=True)
            logger.info("Sent video %s to Discord", video_id)
        except Exception as e:
            logger.exception("Failed to send video %s to Discord: %s", video_id, e)

    finally:
        db.close()
