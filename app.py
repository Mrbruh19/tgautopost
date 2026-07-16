import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import List

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl, field_validator

app = FastAPI(title="Telegram Car Publisher", version="1.0.0")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_KEY = os.getenv("PUBLISH_API_KEY", "")


class PublishRequest(BaseModel):
    caption: str
    photo_urls: List[HttpUrl]

    @field_validator("photo_urls")
    @classmethod
    def validate_photo_count(cls, value):
        if len(value) != 4:
            raise ValueError("Нужно передать ровно 4 фотографии.")
        return value


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/publish")
async def publish_car(
    payload: PublishRequest,
    x_api_key: str | None = Header(default=None),
):
    if not BOT_TOKEN or not CHAT_ID or not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Не настроены TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID или PUBLISH_API_KEY.",
        )

    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Неверный API-ключ.")

    temp_dir = Path(tempfile.mkdtemp(prefix="carpost_"))
    downloaded_files: list[Path] = []

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            for index, photo_url in enumerate(payload.photo_urls, start=1):
                response = await client.get(str(photo_url))
                response.raise_for_status()

                content_type = response.headers.get("content-type", "").lower()
                if "image" not in content_type:
                    raise HTTPException(
                        status_code=400,
                        detail=f"photo{index} не является изображением.",
                    )

                suffix = ".jpg"
                if "png" in content_type:
                    suffix = ".png"
                elif "webp" in content_type:
                    suffix = ".webp"

                file_path = temp_dir / f"photo_{index}{suffix}"
                file_path.write_bytes(response.content)
                downloaded_files.append(file_path)

        media = []
        for i, file_path in enumerate(downloaded_files):
            item = {"type": "photo", "media": f"attach://photo{i+1}"}
            if i == 0:
                item["caption"] = payload.caption
                item["parse_mode"] = "HTML"
            media.append(item)

        upload_files = {
            f"photo{i+1}": (
                file_path.name,
                file_path.read_bytes(),
                "application/octet-stream",
            )
            for i, file_path in enumerate(downloaded_files)
        }

        telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                telegram_url,
                data={
                    "chat_id": CHAT_ID,
                    "media": json.dumps(media, ensure_ascii=False),
                },
                files=upload_files,
            )

        result = response.json()
        if not result.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=f"Telegram вернул ошибку: {result}",
            )

        return {"ok": True, "message": "Пост опубликован"}

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось скачать фотографию: HTTP {exc.response.status_code}",
        ) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
