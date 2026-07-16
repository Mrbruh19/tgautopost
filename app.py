import hashlib
import logging
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import cv2
import httpx
import numpy as np
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="Telegram Car Publisher", version="2.2.0")
logger = logging.getLogger("tgautopost")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_KEY = os.getenv("PUBLISH_API_KEY", "").strip()
MEDIA_TTL_HOURS = int(os.getenv("MEDIA_TTL_HOURS", "24"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/tmp/tgautopost_media"))
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


class PrepareRequest(BaseModel):
    page_url: HttpUrl


class PublishRequest(BaseModel):
    job_id: str
    caption: str


def verify_api_key(x_api_key: str | None) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="PUBLISH_API_KEY не настроен.")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Неверный API-ключ.")


def cleanup_expired_jobs() -> None:
    cutoff = time.time() - MEDIA_TTL_HOURS * 3600
    for path in MEDIA_ROOT.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def normalize_candidate(base_url: str, raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip('"\'')
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    return urljoin(base_url, raw)


def collect_image_candidates(page_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ordered: list[str] = []

    attrs = (
        "src",
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-url",
        "data-image",
        "data-large",
        "data-zoom-image",
    )

    for tag in soup.find_all(["img", "source", "a"]):
        for attr in attrs + (("href",) if tag.name == "a" else tuple()):
            value = tag.get(attr)
            if value:
                candidate = normalize_candidate(page_url, value)
                if candidate:
                    ordered.append(candidate)

        for srcset_attr in ("srcset", "data-srcset"):
            srcset = tag.get(srcset_attr)
            if srcset:
                for part in srcset.split(","):
                    candidate = normalize_candidate(page_url, part.strip().split(" ")[0])
                    if candidate:
                        ordered.append(candidate)

    # URLs that are embedded in scripts, JSON, or inline styles.
    patterns = [
        r"https?://[^\s\"'<>]+?(?:\.jpe?g|\.png|\.webp)(?:\?[^\s\"'<>]*)?",
        r"[\"']([^\"']+?(?:\.jpe?g|\.png|\.webp)(?:\?[^\"']*)?)[\"']",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            raw = match.group(1) if match.lastindex else match.group(0)
            candidate = normalize_candidate(page_url, raw)
            if candidate:
                ordered.append(candidate)

    blocked = (
        "favicon",
        "logo.",
        "/logo",
        "icon",
        "sprite",
        "loading",
        "placeholder",
        "avatar",
        "qrcode",
        "qr-code",
        "wechat",
    )

    result: list[str] = []
    seen: set[str] = set()
    for url in ordered:
        lowered = url.lower()
        if any(token in lowered for token in blocked):
            continue
        # Strip URL fragments only; query strings can be required by image CDNs.
        parsed = urlparse(url)
        normalized = parsed._replace(fragment="").geturl()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def detect_logo_bbox(image: np.ndarray) -> tuple[int, int, int, int] | None:
    """Detect the dark-red wall sign in the upper area of the image."""
    height, width = image.shape[:2]
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    local_brightness = cv2.blur(gray, (25, 25))

    mask = (
        (red > 60)
        & ((red - green) > 15)
        & ((red - blue) > 15)
        & (red > green * 1.10)
        & (red > blue * 1.10)
        & (local_brightness > 135)
    ).astype(np.uint8) * 255

    # The sign is consistently on the wall above the vehicle.
    mask[int(height * 0.32) :, :] = 0
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2)),
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)),
    )

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    components: list[tuple[int, int, int, int, int]] = []
    for index in range(1, count):
        x, y, w, h, area = [int(v) for v in stats[index]]
        center_y = y + h / 2
        if area < 8:
            continue
        if center_y > height * 0.27:
            continue
        if w > width * 0.35 or h > height * 0.18:
            continue
        components.append((x, y, w, h, area))

    if not components:
        return None

    components.sort(key=lambda item: item[1] + item[3] / 2)
    bands: list[dict] = []
    for component in components:
        center_y = component[1] + component[3] / 2
        for band in bands:
            if abs(center_y - band["center_y"]) < height * 0.06:
                band["items"].append(component)
                band["center_y"] = sum(
                    item[1] + item[3] / 2 for item in band["items"]
                ) / len(band["items"])
                break
        else:
            bands.append({"center_y": center_y, "items": [component]})

    valid_components: list[tuple[int, int, int, int, int]] = []
    for band in bands:
        items = band["items"]
        left = min(item[0] for item in items)
        right = max(item[0] + item[2] for item in items)
        total_area = sum(item[4] for item in items)
        if right - left > width * 0.08 and total_area > 30:
            valid_components.extend(items)

    if not valid_components:
        return None

    x0 = min(item[0] for item in valid_components)
    y0 = min(item[1] for item in valid_components)
    x1 = max(item[0] + item[2] for item in valid_components)
    y1 = max(item[1] + item[3] for item in valid_components)

    if x1 - x0 < width * 0.12 or y1 - y0 < height * 0.04:
        return None

    pad_x = max(10, int((x1 - x0) * 0.08))
    pad_y = max(8, int((y1 - y0) * 0.12))
    return (
        max(0, x0 - pad_x),
        max(0, y0 - pad_y),
        min(width, x1 + pad_x),
        min(height, y1 + pad_y),
    )


def remove_wall_logo(image: np.ndarray) -> tuple[np.ndarray, bool]:
    bbox = detect_logo_bbox(image)
    if bbox is None:
        return image, False

    x0, y0, x1, y1 = bbox
    height, width = image.shape[:2]
    region_width = x1 - x0
    region_height = y1 - y0
    gap = max(8, int(region_width * 0.04))

    candidates: list[tuple[int, int]] = []
    if x0 - region_width - gap >= 0:
        candidates.append((x0 - region_width - gap, x0 - gap))
    if x1 + gap + region_width <= width:
        candidates.append((x1 + gap, x1 + gap + region_width))

    if not candidates:
        if x0 >= width - x1:
            source_x0 = max(0, x0 - region_width - gap)
            source_x1 = source_x0 + region_width
        else:
            source_x1 = min(width, x1 + region_width + gap)
            source_x0 = source_x1 - region_width
        candidates.append((source_x0, source_x1))

    best_patch: np.ndarray | None = None
    best_score: float | None = None
    for source_x0, source_x1 in candidates:
        patch = image[y0:y1, source_x0:source_x1]
        if patch.shape[:2] != (region_height, region_width):
            continue
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        brightness = float(hsv[:, :, 2].mean())
        saturation = float(hsv[:, :, 1].mean())
        edges = float(
            cv2.Canny(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), 50, 120).mean()
        )
        score = brightness - 1.1 * saturation - edges
        if best_score is None or score > best_score:
            best_score = score
            best_patch = patch.copy()

    if best_patch is None:
        return image, False

    clone_mask = np.full((region_height, region_width), 255, dtype=np.uint8)
    border = max(3, int(min(region_height, region_width) * 0.03))
    clone_mask[:border, :] = 0
    clone_mask[-border:, :] = 0
    clone_mask[:, :border] = 0
    clone_mask[:, -border:] = 0
    center = ((x0 + x1) // 2, (y0 + y1) // 2)

    try:
        cleaned = cv2.seamlessClone(
            best_patch,
            image,
            clone_mask,
            center,
            cv2.NORMAL_CLONE,
        )
    except cv2.error:
        cleaned = image.copy()
        cleaned[y0:y1, x0:x1] = best_patch

    return cleaned, True


def optimize_for_telegram(image: np.ndarray) -> np.ndarray:
    """Resize large photos to reduce Telegram upload size and avoid timeouts."""
    height, width = image.shape[:2]
    max_side = 1920
    longest = max(width, height)
    if longest <= max_side:
        return image
    scale = max_side / longest
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)


async def download_first_four_images(page_url: str, output_dir: Path) -> list[dict]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(45.0),
        follow_redirects=True,
        headers=HTTP_HEADERS,
    ) as client:
        page_response = await client.get(page_url)
        page_response.raise_for_status()
        html = page_response.text
        candidates = collect_image_candidates(str(page_response.url), html)

        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="На странице не найдены ссылки на фотографии.",
            )

        accepted: list[dict] = []
        hashes: set[str] = set()
        for candidate in candidates[:60]:
            if len(accepted) >= 4:
                break
            try:
                response = await client.get(candidate, headers={**HTTP_HEADERS, "Referer": page_url})
                response.raise_for_status()
            except httpx.HTTPError:
                continue

            content = response.content
            if len(content) < 20_000:
                continue
            array = np.frombuffer(content, dtype=np.uint8)
            image = cv2.imdecode(array, cv2.IMREAD_COLOR)
            if image is None:
                continue
            height, width = image.shape[:2]
            if width < 500 or height < 350:
                continue

            digest = hashlib.sha256(content).hexdigest()
            if digest in hashes:
                continue
            hashes.add(digest)

            cleaned, logo_removed = remove_wall_logo(image)
            cleaned = optimize_for_telegram(cleaned)
            out_height, out_width = cleaned.shape[:2]
            destination = output_dir / f"photo_{len(accepted) + 1}.jpg"
            if not cv2.imwrite(
                str(destination),
                cleaned,
                [int(cv2.IMWRITE_JPEG_QUALITY), 88],
            ):
                continue
            # Keep every file comfortably below Telegram's per-photo limit.
            if destination.stat().st_size > 9_000_000:
                if not cv2.imwrite(
                    str(destination),
                    cleaned,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 78],
                ):
                    continue
            accepted.append(
                {
                    "file": destination.name,
                    "source_url": candidate,
                    "logo_removed": logo_removed,
                    "width": int(out_width),
                    "height": int(out_height),
                    "size_bytes": int(destination.stat().st_size),
                }
            )

    if len(accepted) < 4:
        raise HTTPException(
            status_code=400,
            detail=f"Удалось получить только {len(accepted)} подходящих фотографий из 4.",
        )
    return accepted


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "2.2.0"}


@app.post("/prepare")
async def prepare_car_photos(
    payload: PrepareRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> dict:
    verify_api_key(x_api_key)
    cleanup_expired_jobs()

    job_id = uuid.uuid4().hex
    job_dir = MEDIA_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    try:
        photos = await download_first_four_images(str(payload.page_url), job_dir)
        metadata = {
            "job_id": job_id,
            "page_url": str(payload.page_url),
            "created_at": int(time.time()),
            "photos": photos,
        }
        (job_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    if PUBLIC_BASE_URL:
        base_url = PUBLIC_BASE_URL
    else:
        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        base_url = f"{forwarded_proto}://{forwarded_host}".rstrip("/")

    public_photos = [
        f"{base_url}/media/{job_id}/photo_{index}.jpg" for index in range(1, 5)
    ]
    return {
        "ok": True,
        "job_id": job_id,
        "photo_urls": public_photos,
        "logo_removed": [photo["logo_removed"] for photo in photos],
        "message": "Фотографии подготовлены. Покажите их пользователю до публикации.",
    }


@app.get("/media/{job_id}/{filename}")
async def get_prepared_photo(job_id: str, filename: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(status_code=404, detail="Файл не найден.")
    if not re.fullmatch(r"photo_[1-4]\.jpg", filename):
        raise HTTPException(status_code=404, detail="Файл не найден.")
    file_path = MEDIA_ROOT / job_id / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден или срок хранения истёк.")
    return FileResponse(file_path, media_type="image/jpeg")


@app.post("/publish")
async def publish_prepared_car(
    payload: PublishRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    verify_api_key(x_api_key)
    if not BOT_TOKEN or not CHAT_ID:
        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не настроены.",
        )
    if not re.fullmatch(r"[0-9a-f]{32}", payload.job_id):
        raise HTTPException(status_code=400, detail="Некорректный job_id.")

    job_dir = MEDIA_ROOT / payload.job_id
    photo_paths = [job_dir / f"photo_{index}.jpg" for index in range(1, 5)]
    if not all(path.is_file() for path in photo_paths):
        raise HTTPException(
            status_code=404,
            detail="Подготовленные фотографии не найдены или срок хранения истёк.",
        )
    if len(payload.caption) > 1024:
        raise HTTPException(
            status_code=400,
            detail=f"Подпись содержит {len(payload.caption)} символов. Telegram допускает не более 1024 символов для подписи к альбому.",
        )

    media = []
    for index, path in enumerate(photo_paths, start=1):
        item = {"type": "photo", "media": f"attach://photo{index}"}
        if index == 1:
            item["caption"] = payload.caption
            item["parse_mode"] = "HTML"
        media.append(item)

    upload_files = {
        f"photo{index}": (path.name, path.read_bytes(), "image/jpeg")
        for index, path in enumerate(photo_paths, start=1)
    }

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=180.0, write=180.0, pool=30.0)
        ) as client:
            response = await client.post(
                telegram_url,
                data={
                    "chat_id": CHAT_ID,
                    "media": json.dumps(media, ensure_ascii=False),
                },
                files=upload_files,
            )
    except httpx.TimeoutException as exc:
        logger.exception("Telegram upload timed out")
        raise HTTPException(
            status_code=504,
            detail="Истекло время отправки фотографий в Telegram. Фотографии не опубликованы.",
        ) from exc
    except httpx.RequestError as exc:
        logger.exception("Telegram request failed")
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось подключиться к Telegram: {exc.__class__.__name__}.",
        ) from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram вернул неожиданный ответ: HTTP {response.status_code}",
        ) from exc

    if not result.get("ok"):
        description = result.get("description", "неизвестная ошибка")
        error_code = result.get("error_code", response.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"Telegram API {error_code}: {description}",
        )

    return {
        "ok": True,
        "message": "Пост опубликован в Telegram.",
        "job_id": payload.job_id,
    }
