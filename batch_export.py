import gc
import hashlib
import mimetypes
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cv2
import httpx
import numpy as np
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.responses import FileResponse

from app import HTTP_HEADERS, MEDIA_ROOT, PREPARE_LOCK, app, collect_image_candidates, remove_wall_logo, resize_for_processing

CARS = {
    "2274": "http://avtomirhrb.ru/?car_1/2274.html",
    "2102": "http://avtomirhrb.ru/?car_1/2102.html=",
    "2541": "http://avtomirhrb.ru/?car_1/2541.html=",
    "2223": "http://avtomirhrb.ru/?car_1/2223.html=",
    "2578": "http://avtomirhrb.ru/?car_1/2578.html=",
}
TOKEN = "A6pW5txl2YcQm34H7kV9"
CACHE_ROOT = MEDIA_ROOT / "batch_export_0809"
TTL = 6 * 3600


def normalize(base_url: str, raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip('"\'').replace("\\/", "/")
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return None
    if raw.startswith("//"):
        raw = "http:" + raw
    return urljoin(base_url, raw)


def collect_videos(page_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ordered: list[str] = []
    attrs = ("src", "data-src", "data-url", "data-video", "data-video-url", "data-original", "href")
    for tag in soup.find_all(["video", "source", "a", "iframe"]):
        for attr in attrs:
            candidate = normalize(page_url, tag.get(attr))
            if candidate:
                ordered.append(candidate)
    patterns = [
        r"https?:(?:\\/\\/|//)[^\s\"'<>]+?(?:\.mp4|\.mov|\.m4v|\.webm|\.m3u8)(?:\?[^\s\"'<>]*)?",
        r"[\"']([^\"']+?(?:\.mp4|\.mov|\.m4v|\.webm|\.m3u8)(?:\?[^\"']*)?)[\"']",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            raw = match.group(1) if match.lastindex else match.group(0)
            candidate = normalize(page_url, raw)
            if candidate:
                ordered.append(candidate)
    out, seen = [], set()
    for url in ordered:
        low = url.lower()
        if not any(ext in low for ext in (".mp4", ".mov", ".m4v", ".webm", ".m3u8")):
            continue
        normalized = urlparse(url)._replace(fragment="").geturl()
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def video_ext(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    return guessed if guessed in {".mp4", ".mov", ".m4v", ".webm"} else ".mp4"


async def download_hls(client: httpx.AsyncClient, playlist_url: str, page_url: str, destination: Path) -> bool:
    r = await client.get(playlist_url, headers={**HTTP_HEADERS, "Referer": page_url})
    r.raise_for_status()
    text = r.text
    if "#EXT-X-KEY" in text and "METHOD=NONE" not in text:
        return False
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
            variants.append(urljoin(str(r.url), lines[i + 1]))
    if variants:
        return await download_hls(client, variants[-1], page_url, destination)
    segments = [urljoin(str(r.url), x) for x in lines if not x.startswith("#")]
    if not segments:
        return False
    total = 0
    with destination.open("wb") as fh:
        for seg in segments:
            sr = await client.get(seg, headers={**HTTP_HEADERS, "Referer": page_url})
            sr.raise_for_status()
            fh.write(sr.content)
            total += len(sr.content)
    return total > 100_000


async def build_one(car_id: str) -> Path:
    page_url = CARS[car_id]
    car_root = CACHE_ROOT / car_id
    zip_path = car_root / f"car_{car_id}_photos_videos.zip"
    car_root.mkdir(parents=True, exist_ok=True)
    if zip_path.is_file() and time.time() - zip_path.stat().st_mtime < TTL:
        return zip_path

    work = car_root / f"work_{uuid.uuid4().hex}"
    photos = work / "photos"
    videos = work / "videos"
    photos.mkdir(parents=True)
    videos.mkdir(parents=True)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=180.0, write=60.0, pool=30.0), follow_redirects=True, headers=HTTP_HEADERS) as client:
            page = await client.get(page_url)
            page.raise_for_status()
            html = page.text
            effective = str(page.url)

            image_candidates = collect_image_candidates(effective, html)
            video_candidates = collect_videos(effective, html)

            image_hashes: set[str] = set()
            photo_count = 0
            for candidate in image_candidates[:220]:
                try:
                    r = await client.get(candidate, headers={**HTTP_HEADERS, "Referer": page_url})
                    r.raise_for_status()
                except httpx.HTTPError:
                    continue
                content = r.content
                if len(content) < 20_000:
                    continue
                digest = hashlib.sha256(content).hexdigest()
                if digest in image_hashes:
                    continue
                arr = np.frombuffer(content, dtype=np.uint8)
                image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if image is None:
                    continue
                h, w = image.shape[:2]
                if w < 500 or h < 350:
                    continue
                image_hashes.add(digest)
                image = resize_for_processing(image)
                cleaned, _ = remove_wall_logo(image)
                photo_count += 1
                dest = photos / f"photo_{photo_count:02d}.jpg"
                if not cv2.imwrite(str(dest), cleaned, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
                    photo_count -= 1
                del image, cleaned, arr, content
                gc.collect()

            video_hashes: set[str] = set()
            video_count = 0
            for candidate in video_candidates[:60]:
                low = candidate.lower()
                try:
                    if ".m3u8" in low:
                        temp = videos / f"video_{video_count + 1:02d}.ts"
                        if await download_hls(client, candidate, page_url, temp):
                            digest = hashlib.sha256(temp.read_bytes()).hexdigest()
                            if digest in video_hashes:
                                temp.unlink(missing_ok=True)
                            else:
                                video_hashes.add(digest)
                                video_count += 1
                        else:
                            temp.unlink(missing_ok=True)
                        continue
                    r = await client.get(candidate, headers={**HTTP_HEADERS, "Referer": page_url})
                    r.raise_for_status()
                except httpx.HTTPError:
                    continue
                content = r.content
                ctype = r.headers.get("content-type", "").lower()
                if len(content) < 100_000:
                    continue
                if not (ctype.startswith("video/") or any(ext in low for ext in (".mp4", ".mov", ".m4v", ".webm"))):
                    continue
                digest = hashlib.sha256(content).hexdigest()
                if digest in video_hashes:
                    continue
                video_hashes.add(digest)
                video_count += 1
                (videos / f"video_{video_count:02d}{video_ext(candidate, ctype)}").write_bytes(content)

        if photo_count == 0:
            raise HTTPException(status_code=502, detail=f"Не удалось скачать фотографии для {car_id}")

        readme = work / "README.txt"
        readme.write_text(
            f"Источник: {page_url}\nФотографий: {photo_count}\nВидео: {video_count}\nНазвание на стене удалено/замазано на фотографиях алгоритмом tgautopost.\n",
            encoding="utf-8",
        )
        tmp = car_root / f"{zip_path.name}.tmp"
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(photos.glob("*")):
                z.write(p, arcname=f"photos/{p.name}")
            for p in sorted(videos.glob("*")):
                z.write(p, arcname=f"videos/{p.name}")
            z.write(readme, arcname="README.txt")
        tmp.replace(zip_path)
        return zip_path
    finally:
        shutil.rmtree(work, ignore_errors=True)
        gc.collect()


@app.get(f"/batch-export-{TOKEN}/{{car_id}}")
async def export_one(car_id: str):
    if car_id not in CARS:
        raise HTTPException(status_code=404, detail="Неизвестный автомобиль")
    async with PREPARE_LOCK:
        try:
            path = await build_one(car_id)
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"Источник {car_id} вернул HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка подключения для {car_id}: {exc.__class__.__name__}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Ошибка {car_id}: {exc.__class__.__name__}: {str(exc)[:240]}") from exc
    return FileResponse(path, media_type="application/zip", filename=f"car_{car_id}_photos_videos.zip")
