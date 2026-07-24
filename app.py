import asyncio
import gc
import hashlib
import html
import math
import random
import sqlite3
import xml.etree.ElementTree as ET
import logging
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import cv2
import httpx
import numpy as np
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="Telegram Car Publisher", version="3.0.0")
logger = logging.getLogger("tgautopost")
PREPARE_LOCK = asyncio.Lock()
AUTO_PUBLISH_LOCK = asyncio.Lock()
SCHEDULER_TASK: asyncio.Task | None = None
RATE_CACHE: dict[str, tuple[float, float]] = {}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_KEY = os.getenv("PUBLISH_API_KEY", "").strip()
MEDIA_TTL_HOURS = int(os.getenv("MEDIA_TTL_HOURS", "24"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

STATE_ROOT = Path(os.getenv("STATE_ROOT", "/data"))
try:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    STATE_ROOT = Path("/tmp/tgautopost_state")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(STATE_ROOT / "media")))
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = STATE_ROOT / "queue.db"
SEED_PATH = Path(__file__).with_name("queue_seed.json")

AUTO_PUBLISH_ENABLED = os.getenv("AUTO_PUBLISH_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
AUTO_PUBLISH_TZ = os.getenv("AUTO_PUBLISH_TZ", "Asia/Yekaterinburg").strip()
AUTO_PUBLISH_TIMES = tuple(
    value.strip()
    for value in os.getenv("AUTO_PUBLISH_TIMES", "12:00,16:00,20:00").split(",")
    if value.strip()
)
AUTO_PUBLISH_START_DATE = date.fromisoformat(
    os.getenv("AUTO_PUBLISH_START_DATE", "2026-07-18").strip()
)
AUTO_PUBLISH_CATCHUP_MINUTES = int(os.getenv("AUTO_PUBLISH_CATCHUP_MINUTES", "90"))
AUTO_PUBLISH_RETRY_MINUTES = int(os.getenv("AUTO_PUBLISH_RETRY_MINUTES", "10"))
CONTACT_TELEGRAM = os.getenv("CONTACT_TELEGRAM", "@latypovars").strip()
COMMISSION_RUB = int(os.getenv("COMMISSION_RUB", "50000"))
BROKER_RUB = int(os.getenv("BROKER_RUB", "49000"))
INTERNAL_MARKUP_CNY = int(os.getenv("INTERNAL_MARKUP_CNY", "6000"))

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


def resize_for_processing(image: np.ndarray) -> np.ndarray:
    """Limit memory use before logo detection and seamless cloning."""
    height, width = image.shape[:2]
    max_side = 1600
    longest = max(width, height)
    if longest <= max_side:
        return image
    scale = max_side / longest
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)


async def download_first_six_images(page_url: str, output_dir: Path) -> list[dict]:
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
            if len(accepted) >= 6:
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

            image = resize_for_processing(image)
            cleaned, logo_removed = remove_wall_logo(image)
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
            del image, cleaned, array, content
            gc.collect()

    if len(accepted) < 6:
        raise HTTPException(
            status_code=400,
            detail=f"Удалось получить только {len(accepted)} подходящих фотографий из 6.",
        )
    return accepted



class TelegramDeliveryUncertainError(RuntimeError):
    """Telegram may have accepted the album even though the response timed out."""


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_queue_database() -> None:
    with db_connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_url TEXT NOT NULL UNIQUE,
                model TEXT NOT NULL,
                configuration TEXT NOT NULL,
                production_year INTEGER NOT NULL,
                production_month INTEGER NOT NULL,
                mileage_km INTEGER NOT NULL,
                body_color TEXT,
                interior_color TEXT,
                paint_condition TEXT,
                horsepower INTEGER NOT NULL,
                price_cny INTEGER NOT NULL,
                engine_cc INTEGER NOT NULL,
                engine_display TEXT NOT NULL,
                source_row INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                published_at TEXT,
                telegram_message_ids TEXT
            );

            CREATE TABLE IF NOT EXISTS publish_slots (
                slot_key TEXT PRIMARY KEY,
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                car_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at TEXT,
                last_error TEXT,
                published_at TEXT,
                telegram_message_ids TEXT,
                FOREIGN KEY(car_id) REFERENCES cars(id)
            );

            CREATE INDEX IF NOT EXISTS idx_cars_status ON cars(status);
            CREATE INDEX IF NOT EXISTS idx_slots_status ON publish_slots(status);
            """
        )
        # Recover safely after an application restart.
        connection.execute(
            "UPDATE cars SET status='pending' WHERE status='processing'"
        )
        connection.execute(
            """
            UPDATE publish_slots
            SET status='failed',
                retry_at=?,
                last_error=COALESCE(last_error, 'Перезапуск сервиса во время обработки')
            WHERE status='processing'
            """,
            (datetime.now(ZoneInfo(AUTO_PUBLISH_TZ)).isoformat(),),
        )

        if not SEED_PATH.is_file():
            logger.warning("Queue seed file is missing: %s", SEED_PATH)
            return

        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        for car in seed:
            connection.execute(
                """
                INSERT OR IGNORE INTO cars (
                    page_url, model, configuration, production_year,
                    production_month, mileage_km, body_color, interior_color,
                    paint_condition, horsepower, price_cny, engine_cc,
                    engine_display, source_row, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    car["page_url"],
                    car["model"],
                    car["configuration"],
                    car["production_year"],
                    car["production_month"],
                    car["mileage_km"],
                    car.get("body_color", ""),
                    car.get("interior_color", ""),
                    car.get("paint_condition", ""),
                    car["horsepower"],
                    car["price_cny"],
                    car["engine_cc"],
                    car["engine_display"],
                    car.get("source_row"),
                ),
            )


def queue_counts() -> dict[str, int]:
    if not DB_PATH.exists():
        return {"pending": 0, "processing": 0, "published": 0, "failed": 0, "uncertain": 0}
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM cars GROUP BY status"
        ).fetchall()
    counts = {"pending": 0, "processing": 0, "published": 0, "failed": 0, "uncertain": 0}
    for row in rows:
        counts[str(row["status"])] = int(row["count"])
    return counts


def get_public_base_url() -> str:
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return (
        PUBLIC_BASE_URL
        or (f"https://{railway_domain}" if railway_domain else "")
    ).rstrip("/")


def parse_schedule_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if not match:
        raise ValueError(f"Некорректное время публикации: {value}")
    return int(match.group(1)), int(match.group(2))


def scheduled_slots_for_day(day: date) -> list[datetime]:
    timezone = ZoneInfo(AUTO_PUBLISH_TZ)
    slots: list[datetime] = []
    for value in AUTO_PUBLISH_TIMES:
        hour, minute = parse_schedule_time(value)
        slots.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone))
    return sorted(slots)


def next_slot_iso(now: datetime | None = None) -> str | None:
    timezone = ZoneInfo(AUTO_PUBLISH_TZ)
    now = now or datetime.now(timezone)
    for offset in range(0, 3):
        day = now.date() + timedelta(days=offset)
        if day < AUTO_PUBLISH_START_DATE:
            continue
        for slot in scheduled_slots_for_day(day):
            if slot > now:
                return slot.isoformat()
    return None


def age_in_months(car: sqlite3.Row, reference_date: date) -> int:
    return (
        (reference_date.year - int(car["production_year"])) * 12
        + reference_date.month
        - int(car["production_month"])
    )


def is_safe_for_automatic_price(car: sqlite3.Row, reference_date: date) -> bool:
    months = age_in_months(car, reference_date)
    # The Excel file contains only production month. At exactly 3 or 5 years,
    # the day is required to choose the correct customs tariff.
    if months in {36, 60}:
        return False
    horsepower = int(car["horsepower"])
    return 0 < horsepower <= 160


async def fetch_cbr_rates(reference_date: date) -> tuple[float, float]:
    cache_key = reference_date.isoformat()
    if cache_key in RATE_CACHE:
        return RATE_CACHE[cache_key]

    url = "https://www.cbr.ru/scripts/XML_daily.asp"
    params = {"date_req": reference_date.strftime("%d/%m/%Y")}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=20.0, read=40.0, write=20.0, pool=20.0),
        follow_redirects=True,
        headers={"User-Agent": HTTP_HEADERS["User-Agent"]},
    ) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()

    root = ET.fromstring(response.content)
    rates: dict[str, float] = {}
    for valute in root.findall("Valute"):
        code = (valute.findtext("CharCode") or "").strip()
        nominal = int((valute.findtext("Nominal") or "1").strip())
        value = float((valute.findtext("Value") or "0").replace(",", "."))
        rates[code] = value / nominal

    if "CNY" not in rates or "EUR" not in rates:
        raise RuntimeError("Банк России не вернул курсы CNY и EUR.")

    result = (rates["CNY"], rates["EUR"])
    RATE_CACHE[cache_key] = result
    return result


def used_car_rate_eur_per_cc(engine_cc: int, older_than_five: bool) -> float:
    if older_than_five:
        if engine_cc <= 1000:
            return 3.0
        if engine_cc <= 1500:
            return 3.2
        if engine_cc <= 1800:
            return 3.5
        if engine_cc <= 2300:
            return 4.8
        if engine_cc <= 3000:
            return 5.0
        return 5.7

    if engine_cc <= 1000:
        return 1.5
    if engine_cc <= 1500:
        return 1.7
    if engine_cc <= 1800:
        return 2.5
    if engine_cc <= 2300:
        return 2.7
    if engine_cc <= 3000:
        return 3.0
    return 3.6


def under_three_duty_eur(customs_value_eur: float, engine_cc: int) -> float:
    if customs_value_eur <= 8500:
        percent, minimum = 0.54, 2.5
    elif customs_value_eur <= 16700:
        percent, minimum = 0.48, 3.5
    elif customs_value_eur <= 42300:
        percent, minimum = 0.48, 5.5
    elif customs_value_eur <= 84500:
        percent, minimum = 0.48, 7.5
    elif customs_value_eur <= 169000:
        percent, minimum = 0.48, 15.0
    else:
        percent, minimum = 0.48, 20.0
    return max(customs_value_eur * percent, engine_cc * minimum)


def calculate_final_price(
    car: sqlite3.Row,
    reference_date: date,
    cny_rub: float,
    eur_rub: float,
) -> dict[str, float | int]:
    months = age_in_months(car, reference_date)
    if months in {36, 60}:
        raise RuntimeError(
            "Для автомобиля на границе 3 или 5 лет требуется точный день выпуска."
        )

    car_value_rub = (int(car["price_cny"]) + INTERNAL_MARKUP_CNY) * cny_rub
    customs_value_eur = car_value_rub / eur_rub
    engine_cc = int(car["engine_cc"])

    if months < 36:
        duty_eur = under_three_duty_eur(customs_value_eur, engine_cc)
        recycling_fee = 3400
    elif months < 60:
        duty_eur = used_car_rate_eur_per_cc(engine_cc, False) * engine_cc
        recycling_fee = 5200
    else:
        duty_eur = used_car_rate_eur_per_cc(engine_cc, True) * engine_cc
        recycling_fee = 5200

    duty_rub = duty_eur * eur_rub
    total = car_value_rub + duty_rub + recycling_fee + COMMISSION_RUB + BROKER_RUB
    rounded_total = int(math.floor(total / 1000 + 0.5) * 1000)

    return {
        "car_value_rub": car_value_rub,
        "duty_rub": duty_rub,
        "recycling_fee": recycling_fee,
        "total_rub": total,
        "rounded_total_rub": rounded_total,
    }


MONTH_NAMES_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


def normalize_paint_condition(value: str) -> str:
    lowered = value.strip().lower()
    if "оригин" in lowered:
        return "оригинальный"
    return value.strip()


def build_auto_caption(car: sqlite3.Row, rounded_total_rub: int) -> str:
    model = html.escape(str(car["model"]).strip())
    configuration = html.escape(str(car["configuration"]).strip())
    engine_display = html.escape(str(car["engine_display"]).strip())
    body_color = html.escape(str(car["body_color"]).strip())
    interior_color = html.escape(str(car["interior_color"]).strip())
    paint = html.escape(normalize_paint_condition(str(car["paint_condition"])))
    month = MONTH_NAMES_RU[int(car["production_month"])]
    year = int(car["production_year"])
    mileage = f'{int(car["mileage_km"]):,}'.replace(",", " ")
    price = f"{rounded_total_rub:,}".replace(",", " ")
    contact = html.escape(CONTACT_TELEGRAM)

    title = f"{model} {configuration}".upper()
    lines = [
        f"🚘 <b>{title}</b>",
        "",
        f"Автомобиль {model} в комплектации {configuration}.",
        "",
        f"▫️ Год выпуска: {month} {year}",
        f"▫️ Двигатель: {engine_display}",
        f"▫️ Мощность: {int(car['horsepower'])} л. с.",
        f"▫️ Пробег: {mileage} км",
        f"▫️ Цвет кузова: {body_color}",
    ]
    if interior_color:
        lines.append(f"▫️ Цвет салона: {interior_color}")
    lines.extend(
        [
            f"▫️ Комплектация: {configuration}",
            f"▫️ Состояние окраса: {paint}",
            "",
            f"💰 <b>Цена с растаможкой и оформлением: {price} ₽</b>",
            "",
            (
                "В стоимость включены таможенная пошлина, утильсбор, комиссия, "
                "услуги брокера, полный пакет документов, лаборатория и хранение "
                "на СВХ до 7 дней."
            ),
            "",
            "ГЛОНАСС и доставка до города покупателя в стоимость не включены.",
            "",
            f"📩 По вопросам приобретения автомобиля: {contact}",
        ]
    )
    caption = "\n".join(lines)
    if len(caption) > 1024:
        raise RuntimeError(
            f"Подпись для {car['model']} превышает лимит Telegram: {len(caption)}."
        )
    return caption


async def send_album_from_job(job_id: str, caption: str) -> list[int]:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не настроены.")

    base_url = get_public_base_url()
    if not base_url:
        raise RuntimeError("PUBLIC_BASE_URL или RAILWAY_PUBLIC_DOMAIN не настроен.")

    media = []
    for index in range(1, 7):
        item: dict[str, str] = {
            "type": "photo",
            "media": f"{base_url}/media/{job_id}/photo_{index}.jpg",
        }
        if index == 1:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=20.0)
        ) as client:
            response = await client.post(
                telegram_url,
                data={
                    "chat_id": CHAT_ID,
                    "media": json.dumps(media, ensure_ascii=False),
                },
            )
    except httpx.TimeoutException as exc:
        raise TelegramDeliveryUncertainError(
            "Telegram не ответил вовремя; публикация могла состояться."
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Не удалось подключиться к Telegram: {exc.__class__.__name__}."
        ) from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Telegram вернул неожиданный ответ HTTP {response.status_code}."
        ) from exc

    if not result.get("ok"):
        error_code = int(result.get("error_code", response.status_code))
        description = str(result.get("description", "неизвестная ошибка"))
        if error_code == 504:
            raise TelegramDeliveryUncertainError(
                f"Telegram API 504: {description}"
            )
        raise RuntimeError(f"Telegram API {error_code}: {description}")

    return [
        int(message["message_id"])
        for message in result.get("result", [])
        if isinstance(message, dict) and message.get("message_id") is not None
    ]


def ensure_slot_record(slot: datetime) -> sqlite3.Row:
    slot_key = slot.isoformat()
    with db_connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO publish_slots(slot_key, scheduled_at, status)
            VALUES (?, ?, 'pending')
            """,
            (slot_key, slot.isoformat()),
        )
        row = connection.execute(
            "SELECT * FROM publish_slots WHERE slot_key=?", (slot_key,)
        ).fetchone()
    if row is None:
        raise RuntimeError("Не удалось создать запись временного слота.")
    return row


def choose_car_for_slot(slot: datetime) -> sqlite3.Row | None:
    timezone = ZoneInfo(AUTO_PUBLISH_TZ)
    now = datetime.now(timezone)
    slot_key = slot.isoformat()

    with db_connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        slot_row = connection.execute(
            "SELECT * FROM publish_slots WHERE slot_key=?", (slot_key,)
        ).fetchone()
        if slot_row is None:
            connection.execute(
                """
                INSERT INTO publish_slots(slot_key, scheduled_at, status)
                VALUES (?, ?, 'pending')
                """,
                (slot_key, slot.isoformat()),
            )
            slot_row = connection.execute(
                "SELECT * FROM publish_slots WHERE slot_key=?", (slot_key,)
            ).fetchone()

        if slot_row["status"] in {"success", "uncertain", "no_content"}:
            connection.commit()
            return None

        retry_at = slot_row["retry_at"]
        if retry_at and datetime.fromisoformat(retry_at) > now:
            connection.commit()
            return None

        car: sqlite3.Row | None = None
        if slot_row["car_id"] is not None:
            car = connection.execute(
                "SELECT * FROM cars WHERE id=?", (slot_row["car_id"],)
            ).fetchone()
            if car is not None and car["status"] in {"failed", "published", "uncertain"}:
                car = None

        if car is None:
            pending = connection.execute(
                "SELECT * FROM cars WHERE status='pending'"
            ).fetchall()
            safe = [
                row for row in pending
                if is_safe_for_automatic_price(row, now.date())
            ]
            if not safe:
                connection.execute(
                    """
                    UPDATE publish_slots
                    SET status='no_content', last_error='Нет подходящих автомобилей'
                    WHERE slot_key=?
                    """,
                    (slot_key,),
                )
                connection.commit()
                return None
            car = random.SystemRandom().choice(safe)
            connection.execute(
                "UPDATE publish_slots SET car_id=? WHERE slot_key=?",
                (car["id"], slot_key),
            )

        connection.execute(
            "UPDATE cars SET status='processing' WHERE id=?", (car["id"],)
        )
        connection.execute(
            """
            UPDATE publish_slots
            SET status='processing', retry_at=NULL
            WHERE slot_key=?
            """,
            (slot_key,),
        )
        connection.commit()
        return car


def mark_slot_success(
    slot: datetime,
    car_id: int,
    message_ids: list[int],
) -> None:
    now_iso = datetime.now(ZoneInfo(AUTO_PUBLISH_TZ)).isoformat()
    encoded = json.dumps(message_ids)
    with db_connect() as connection:
        connection.execute(
            """
            UPDATE cars
            SET status='published', published_at=?, telegram_message_ids=?,
                last_error=NULL
            WHERE id=?
            """,
            (now_iso, encoded, car_id),
        )
        connection.execute(
            """
            UPDATE publish_slots
            SET status='success', published_at=?, telegram_message_ids=?,
                last_error=NULL, retry_at=NULL
            WHERE slot_key=?
            """,
            (now_iso, encoded, slot.isoformat()),
        )


def mark_slot_uncertain(slot: datetime, car_id: int, error: str) -> None:
    now_iso = datetime.now(ZoneInfo(AUTO_PUBLISH_TZ)).isoformat()
    with db_connect() as connection:
        connection.execute(
            """
            UPDATE cars
            SET status='uncertain', last_error=?, attempts=attempts+1
            WHERE id=?
            """,
            (error[:1000], car_id),
        )
        connection.execute(
            """
            UPDATE publish_slots
            SET status='uncertain', last_error=?, attempts=attempts+1,
                published_at=?, retry_at=NULL
            WHERE slot_key=?
            """,
            (error[:1000], now_iso, slot.isoformat()),
        )


def mark_slot_failure(slot: datetime, car_id: int, error: str) -> None:
    timezone = ZoneInfo(AUTO_PUBLISH_TZ)
    retry_at = datetime.now(timezone) + timedelta(minutes=AUTO_PUBLISH_RETRY_MINUTES)
    with db_connect() as connection:
        car = connection.execute(
            "SELECT attempts FROM cars WHERE id=?", (car_id,)
        ).fetchone()
        attempts = int(car["attempts"]) + 1 if car else 1

        if attempts >= 3:
            connection.execute(
                """
                UPDATE cars
                SET status='failed', attempts=?, last_error=?
                WHERE id=?
                """,
                (attempts, error[:1000], car_id),
            )
            connection.execute(
                """
                UPDATE publish_slots
                SET status='failed', car_id=NULL, attempts=attempts+1,
                    last_error=?, retry_at=?
                WHERE slot_key=?
                """,
                (error[:1000], retry_at.isoformat(), slot.isoformat()),
            )
        else:
            connection.execute(
                """
                UPDATE cars
                SET status='pending', attempts=?, last_error=?
                WHERE id=?
                """,
                (attempts, error[:1000], car_id),
            )
            connection.execute(
                """
                UPDATE publish_slots
                SET status='failed', attempts=attempts+1, last_error=?,
                    retry_at=?
                WHERE slot_key=?
                """,
                (error[:1000], retry_at.isoformat(), slot.isoformat()),
            )


async def process_scheduled_slot(slot: datetime) -> None:
    async with AUTO_PUBLISH_LOCK:
        ensure_slot_record(slot)
        car = choose_car_for_slot(slot)
        if car is None:
            return

        job_id = uuid.uuid4().hex
        job_dir = MEDIA_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=False)

        try:
            cny_rub, eur_rub = await fetch_cbr_rates(slot.date())
            price = calculate_final_price(car, slot.date(), cny_rub, eur_rub)
            caption = build_auto_caption(car, int(price["rounded_total_rub"]))

            async with PREPARE_LOCK:
                photos = await download_first_six_images(car["page_url"], job_dir)
                metadata = {
                    "job_id": job_id,
                    "page_url": car["page_url"],
                    "created_at": int(time.time()),
                    "public_base_url": get_public_base_url(),
                    "photos": photos,
                    "auto_publish": True,
                    "car_id": int(car["id"]),
                    "slot": slot.isoformat(),
                    "cny_rub": cny_rub,
                    "eur_rub": eur_rub,
                    "rounded_total_rub": int(price["rounded_total_rub"]),
                }
                (job_dir / "metadata.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            message_ids = await send_album_from_job(job_id, caption)
            mark_slot_success(slot, int(car["id"]), message_ids)
            logger.info(
                "Auto-published %s for slot %s; Telegram IDs: %s",
                car["model"], slot.isoformat(), message_ids,
            )
        except TelegramDeliveryUncertainError as exc:
            logger.exception("Telegram delivery status is uncertain")
            mark_slot_uncertain(slot, int(car["id"]), str(exc))
        except Exception as exc:
            logger.exception("Automatic publication failed")
            mark_slot_failure(
                slot,
                int(car["id"]),
                f"{exc.__class__.__name__}: {str(exc)[:900]}",
            )
        finally:
            gc.collect()


async def scheduler_loop() -> None:
    timezone = ZoneInfo(AUTO_PUBLISH_TZ)
    logger.info(
        "Automatic publisher started: %s at %s (%s)",
        AUTO_PUBLISH_START_DATE, AUTO_PUBLISH_TIMES, AUTO_PUBLISH_TZ,
    )
    while True:
        try:
            now = datetime.now(timezone)
            if AUTO_PUBLISH_ENABLED and now.date() >= AUTO_PUBLISH_START_DATE:
                grace = timedelta(minutes=AUTO_PUBLISH_CATCHUP_MINUTES)
                for slot in scheduled_slots_for_day(now.date()):
                    if slot <= now <= slot + grace:
                        row = ensure_slot_record(slot)
                        if row["status"] not in {"success", "uncertain", "no_content"}:
                            await process_scheduled_slot(slot)
                            break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler loop error")
        await asyncio.sleep(20)


@app.on_event("startup")
async def start_background_scheduler() -> None:
    global SCHEDULER_TASK
    initialize_queue_database()
    if AUTO_PUBLISH_ENABLED:
        SCHEDULER_TASK = asyncio.create_task(scheduler_loop())


@app.on_event("shutdown")
async def stop_background_scheduler() -> None:
    global SCHEDULER_TASK
    if SCHEDULER_TASK is not None:
        SCHEDULER_TASK.cancel()
        try:
            await SCHEDULER_TASK
        except asyncio.CancelledError:
            pass
        SCHEDULER_TASK = None


@app.get("/health")
async def health() -> dict:
    counts = queue_counts()
    return {
        "status": "ok",
        "version": "3.0.0",
        "auto_publish_enabled": AUTO_PUBLISH_ENABLED,
        "timezone": AUTO_PUBLISH_TZ,
        "times": list(AUTO_PUBLISH_TIMES),
        "next_slot": next_slot_iso(),
        "queue": counts,
    }


@app.post("/prepare")
async def prepare_car_photos(
    payload: PrepareRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> dict:
    verify_api_key(x_api_key)

    # Railway has limited memory. Serialize image preparation so two vehicles
    # cannot run OpenCV processing at the same time.
    async with PREPARE_LOCK:
        cleanup_expired_jobs()

        job_id = uuid.uuid4().hex
        job_dir = MEDIA_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=False)

        if PUBLIC_BASE_URL:
            base_url = PUBLIC_BASE_URL
        else:
            forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            base_url = f"{forwarded_proto}://{forwarded_host}".rstrip("/")

        try:
            photos = await download_first_six_images(str(payload.page_url), job_dir)
            metadata = {
                "job_id": job_id,
                "page_url": str(payload.page_url),
                "created_at": int(time.time()),
                "public_base_url": base_url,
                "photos": photos,
            }
            (job_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except httpx.HTTPStatusError as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.exception("Source page returned HTTP error")
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Страница автомобиля вернула HTTP {exc.response.status_code}. "
                    "Не удалось получить фотографии."
                ),
            ) from exc
        except httpx.RequestError as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.exception("Source page request failed")
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось подключиться к странице автомобиля: {exc.__class__.__name__}.",
            ) from exc
        except Exception as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.exception("Unexpected photo preparation error")
            raise HTTPException(
                status_code=500,
                detail=f"Ошибка обработки фотографий: {exc.__class__.__name__}: {str(exc)[:240]}",
            ) from exc
        finally:
            gc.collect()

        public_photos = [
            f"{base_url}/media/{job_id}/photo_{index}.jpg" for index in range(1, 7)
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
    if not re.fullmatch(r"photo_[1-6]\.jpg", filename):
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
    photo_paths = [job_dir / f"photo_{index}.jpg" for index in range(1, 7)]

    if not all(path.is_file() for path in photo_paths):
        raise HTTPException(
            status_code=404,
            detail="Подготовленные фотографии не найдены или срок хранения истёк.",
        )

    if len(payload.caption) > 1024:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Подпись содержит {len(payload.caption)} символов. "
                "Telegram допускает не более 1024 символов для подписи к альбому."
            ),
        )

    published_marker = job_dir / "published.json"
    if published_marker.is_file():
        try:
            stored = json.loads(published_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        return {
            "ok": True,
            "message": "Этот набор фотографий уже был опубликован в Telegram.",
            "job_id": payload.job_id,
            "already_published": True,
            "telegram_message_ids": stored.get("telegram_message_ids", []),
        }

    metadata_path = job_dir / "metadata.json"
    metadata = {}
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    base_url = (
        metadata.get("public_base_url")
        or PUBLIC_BASE_URL
        or (f"https://{railway_domain}" if railway_domain else "")
    ).rstrip("/")

    if not base_url:
        raise HTTPException(
            status_code=500,
            detail="Не удалось определить публичный HTTPS-адрес сервиса.",
        )

    photo_urls = [
        f"{base_url}/media/{payload.job_id}/photo_{index}.jpg"
        for index in range(1, 7)
    ]

    # Telegram сам загружает изображения по HTTPS-ссылкам. Это надёжнее,
    # чем передавать шесть тяжёлых файлов через один multipart-запрос.
    media = []
    for index, photo_url in enumerate(photo_urls, start=1):
        item = {"type": "photo", "media": photo_url}
        if index == 1:
            item["caption"] = payload.caption
            item["parse_mode"] = "HTML"
        media.append(item)

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)
        ) as client:
            response = await client.post(
                telegram_url,
                data={
                    "chat_id": CHAT_ID,
                    "media": json.dumps(media, ensure_ascii=False),
                },
            )
    except httpx.TimeoutException as exc:
        logger.exception("Telegram request timed out")
        raise HTTPException(
            status_code=504,
            detail=(
                "Telegram не успел получить фотографии по HTTPS-ссылкам. "
                "Пост не подтверждён как опубликованный."
            ),
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
            detail=f"Telegram вернул неожиданный ответ: HTTP {response.status_code}.",
        ) from exc

    if not result.get("ok"):
        description = result.get("description", "неизвестная ошибка")
        error_code = result.get("error_code", response.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"Telegram API {error_code}: {description}",
        )

    messages = result.get("result", [])
    message_ids = [
        message.get("message_id")
        for message in messages
        if isinstance(message, dict) and message.get("message_id") is not None
    ]

    published_marker.write_text(
        json.dumps(
            {
                "published_at": int(time.time()),
                "telegram_message_ids": message_ids,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "message": "Пост опубликован в Telegram.",
        "job_id": payload.job_id,
        "already_published": False,
        "telegram_message_ids": message_ids,
    }
