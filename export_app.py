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

from app import (
    HTTP_HEADERS,
    MEDIA_ROOT,
    PREPARE_LOCK,
    app,
    collect_image_candidates,
    remove_wall_logo,
    resize_for_processing,
)


EXPORT_PAGE_URL = "http://avtomirhrb.ru/?car_1/2195.html="
EXPORT_TOKEN = "_YE_JdWcdJKhfikv7tcW3iAO"
EXPORT_CACHE_DIR = MEDIA_ROOT / "export_2195"
EXPORT_ZIP = EXPORT_CACHE_DIR / "Honda_Civic_2195_media.zip"
EXPORT_TTL_SECONDS = 6 * 3600


def _normalize_candidate(base_url: str, raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip('"\'').replace("\\/", "/")
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return None
    if raw.startswith("//"):
        raw = "http:" + raw
    return urljoin(base_url, raw)


def collect_video_candidates(page_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ordered: list[str] = []
    attrs = (
        "src",
        "data-src",
        "data-url",
        "data-video",
        "data-video-url",
        "data-original",
        "href",
    )

    for tag in soup.find_all(["video", "source", "a", "iframe"]):
        for attr in attrs:
            value = tag.get(attr)
            candidate = _normalize_candidate(page_url, value)
            if candidate:
                ordered.append(candidate)

    patterns = [
        r"https?:(?:\\/\\/|//)[^\s\"'<>]+?(?:\.mp4|\.mov|\.m4v|\.webm|\.m3u8)(?:\?[^\s\"'<>]*)?",
        r"[\"']([^\"']+?(?:\.mp4|\.mov|\.m4v|\.webm|\.m3u8)(?:\?[^\"']*)?)[\"']",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            raw = match.group(1) if match.lastindex else match.group(0)
            candidate = _normalize_candidate(page_url, raw)
            if candidate:
                ordered.append(candidate)

    result: list[str] = []
    seen: set[str] = set()
    for url in ordered:
        lowered = url.lower()
        if not any(ext in lowered for ext in (".mp4", ".mov", ".m4v", ".webm", ".m3u8")):
            continue
        parsed = urlparse(url)
        normalized = parsed._replace(fragment="").geturl()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _video_extension(url: str, content_type: str) -> str:
    path_suffix = Path(urlparse(url).path).suffix.lower()
    if path_suffix in {".mp4", ".mov", ".m4v", ".webm", ".m3u8"}:
        return path_suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if guessed in {".mp4", ".mov", ".m4v", ".webm"}:
        return guessed
    return ".mp4"


async def _download_hls_playlist(
    client: httpx.AsyncClient,
    playlist_url: str,
    page_url: str,
    destination: Path,
) -> bool:
    """Download a simple HLS playlist to one .ts file.

    This handles ordinary media playlists and one-level master playlists. It is
    intentionally conservative: encrypted playlists are skipped rather than
    creating a broken file.
    """
    response = await client.get(
        playlist_url,
        headers={**HTTP_HEADERS, "Referer": page_url},
    )
    response.raise_for_status()
    text = response.text
    if "#EXT-X-KEY" in text and "METHOD=NONE" not in text:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variant_urls: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            next_line = lines[index + 1]
            if not next_line.startswith("#"):
                variant_urls.append(urljoin(str(response.url), next_line))
    if variant_urls:
        return await _download_hls_playlist(
            client,
            variant_urls[-1],
            page_url,
            destination,
        )

    segment_urls = [
        urljoin(str(response.url), line)
        for line in lines
        if not line.startswith("#")
    ]
    if not segment_urls:
        return False

    total = 0
    with destination.open("wb") as handle:
        for segment_url in segment_urls:
            segment = await client.get(
                segment_url,
                headers={**HTTP_HEADERS, "Referer": page_url},
            )
            segment.raise_for_status()
            if not segment.content:
                continue
            handle.write(segment.content)
            total += len(segment.content)
    return total > 100_000


async def build_export_zip() -> Path:
    EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if EXPORT_ZIP.is_file() and time.time() - EXPORT_ZIP.stat().st_mtime < EXPORT_TTL_SECONDS:
        return EXPORT_ZIP

    work_dir = EXPORT_CACHE_DIR / f"work_{uuid.uuid4().hex}"
    photo_dir = work_dir / "photos"
    video_dir = work_dir / "videos"
    photo_dir.mkdir(parents=True, exist_ok=False)
    video_dir.mkdir(parents=True, exist_ok=False)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=180.0, write=60.0, pool=30.0),
            follow_redirects=True,
            headers=HTTP_HEADERS,
        ) as client:
            page_response = await client.get(EXPORT_PAGE_URL)
            page_response.raise_for_status()
            source_html = page_response.text
            effective_url = str(page_response.url)

            image_candidates = collect_image_candidates(effective_url, source_html)
            video_candidates = collect_video_candidates(effective_url, source_html)

            image_hashes: set[str] = set()
            photo_count = 0
            for candidate in image_candidates[:180]:
                try:
                    response = await client.get(
                        candidate,
                        headers={**HTTP_HEADERS, "Referer": EXPORT_PAGE_URL},
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue

                content = response.content
                if len(content) < 20_000:
                    continue
                digest = hashlib.sha256(content).hexdigest()
                if digest in image_hashes:
                    continue

                array = np.frombuffer(content, dtype=np.uint8)
                image = cv2.imdecode(array, cv2.IMREAD_COLOR)
                if image is None:
                    continue
                height, width = image.shape[:2]
                if width < 500 or height < 350:
                    continue

                image_hashes.add(digest)
                image = resize_for_processing(image)
                cleaned, _ = remove_wall_logo(image)
                photo_count += 1
                destination = photo_dir / f"photo_{photo_count:02d}.jpg"
                ok = cv2.imwrite(
                    str(destination),
                    cleaned,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 92],
                )
                if not ok:
                    photo_count -= 1
                del image, cleaned, array, content
                gc.collect()

            video_hashes: set[str] = set()
            video_count = 0
            for candidate in video_candidates[:40]:
                lowered = candidate.lower()
                try:
                    if ".m3u8" in lowered:
                        temp_path = video_dir / f"video_{video_count + 1:02d}.ts"
                        if await _download_hls_playlist(
                            client,
                            candidate,
                            EXPORT_PAGE_URL,
                            temp_path,
                        ):
                            digest = hashlib.sha256(temp_path.read_bytes()).hexdigest()
                            if digest in video_hashes:
                                temp_path.unlink(missing_ok=True)
                                continue
                            video_hashes.add(digest)
                            video_count += 1
                        else:
                            temp_path.unlink(missing_ok=True)
                        continue

                    response = await client.get(
                        candidate,
                        headers={**HTTP_HEADERS, "Referer": EXPORT_PAGE_URL},
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue

                content = response.content
                content_type = response.headers.get("content-type", "").lower()
                if len(content) < 100_000:
                    continue
                if not (
                    content_type.startswith("video/")
                    or any(ext in lowered for ext in (".mp4", ".mov", ".m4v", ".webm"))
                ):
                    continue
                digest = hashlib.sha256(content).hexdigest()
                if digest in video_hashes:
                    continue
                video_hashes.add(digest)
                video_count += 1
                ext = _video_extension(candidate, content_type)
                (video_dir / f"video_{video_count:02d}{ext}").write_bytes(content)

        if photo_count == 0:
            raise HTTPException(status_code=502, detail="Не удалось скачать фотографии автомобиля.")

        manifest = work_dir / "README.txt"
        manifest.write_text(
            "Источник: " + EXPORT_PAGE_URL + "\n"
            + f"Фотографий: {photo_count}\n"
            + f"Видео: {video_count}\n"
            + "На фотографиях название на стене удалено/замазано алгоритмом проекта tgautopost.\n",
            encoding="utf-8",
        )

        temp_zip = EXPORT_CACHE_DIR / f"{EXPORT_ZIP.name}.tmp"
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file_path in sorted(photo_dir.glob("*")):
                archive.write(file_path, arcname=f"photos/{file_path.name}")
            for file_path in sorted(video_dir.glob("*")):
                archive.write(file_path, arcname=f"videos/{file_path.name}")
            archive.write(manifest, arcname="README.txt")
        temp_zip.replace(EXPORT_ZIP)
        return EXPORT_ZIP
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()


@app.get(f"/export-2195-{EXPORT_TOKEN}")
async def export_car_2195_media():
    async with PREPARE_LOCK:
        try:
            zip_path = await build_export_zip()
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Источник вернул HTTP {exc.response.status_code}.",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось подключиться к источнику: {exc.__class__.__name__}.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Ошибка экспорта: {exc.__class__.__name__}: {str(exc)[:300]}",
            ) from exc

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="Honda_Civic_2195_photos_videos.zip",
    )
