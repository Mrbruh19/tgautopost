import asyncio
import html
import json
import re
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


TARGETS_PATH = ROOT / "batch_export_targets.json"
OUTPUT_ROOT = ROOT / "batch_export_output"
GROUPS = ("Sagitar", "Lamando", "Audi")


def group_for(car: dict) -> str:
    model = str(car["model"]).casefold()
    if "sagitar" in model or "sagittar" in model:
        return "Sagitar"
    if "lamando" in model:
        return "Lamando"
    return "Audi"


def safe_name(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", value).strip("_")
    return value[:100] or "car"


def plain_caption(caption: str) -> str:
    return html.unescape(re.sub(r"</?b>", "", caption))


async def download_with_retries(car: dict, car_dir: Path):
    last_error = None
    for attempt in range(1, 4):
        try:
            return await app.download_selected_eight_images(car["page_url"], car_dir)
        except Exception as exc:  # Keep the batch moving and report the exact failed car.
            last_error = exc
            for photo in car_dir.glob("photo_*.jpg"):
                photo.unlink(missing_ok=True)
            if attempt < 3:
                await asyncio.sleep(attempt * 8)
    raise RuntimeError(f"{last_error.__class__.__name__}: {last_error}") from last_error


def make_archives() -> None:
    for group in GROUPS:
        group_dir = OUTPUT_ROOT / group
        archive_path = OUTPUT_ROOT / f"{group}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(group_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(group_dir))


async def main() -> int:
    targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    OUTPUT_ROOT.mkdir(parents=True)
    for group in GROUPS:
        (OUTPUT_ROOT / group).mkdir()

    reference_date = date.today()
    cny_rub, eur_rub = await app.fetch_cbr_rates(reference_date)
    catalogs = {group: [] for group in GROUPS}
    failures = []

    for index, car in enumerate(targets, start=1):
        group = group_for(car)
        stock = str(car.get("stock_number", ""))
        folder = f"{index:02d}_{safe_name(str(car['model']))}_{safe_name(stock)}"
        car_dir = OUTPUT_ROOT / group / folder
        car_dir.mkdir(parents=True)
        try:
            photos, page_details = await download_with_retries(car, car_dir)
            price = app.calculate_final_price(car, reference_date, cny_rub, eur_rub)
            caption = app.build_auto_caption(
                car,
                int(price["rounded_total_rub"]),
                page_details,
            )
            (car_dir / "post.txt").write_text(plain_caption(caption), encoding="utf-8")
            (car_dir / "details.json").write_text(
                json.dumps(
                    {
                        "source_url": car["page_url"],
                        "stock_number": stock,
                        "price_date": reference_date.isoformat(),
                        "cny_rub": cny_rub,
                        "eur_rub": eur_rub,
                        "rounded_total_rub": int(price["rounded_total_rub"]),
                        "page_details": page_details,
                        "photos": photos,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            catalogs[group].append(
                f"{folder}: {int(price['rounded_total_rub']):,} ₽".replace(",", " ")
            )
            print(f"[{index}/{len(targets)}] OK {car['model']} {stock}", flush=True)
        except Exception as exc:
            message = f"{car['model']} {stock} — {exc}"
            failures.append(message)
            (car_dir / "ERROR.txt").write_text(message, encoding="utf-8")
            print(f"[{index}/{len(targets)}] FAILED {message}", flush=True)
        await asyncio.sleep(1)

    for group, lines in catalogs.items():
        (OUTPUT_ROOT / group / "catalog.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    if failures:
        (OUTPUT_ROOT / "errors.txt").write_text(
            "\n".join(failures) + "\n", encoding="utf-8"
        )

    make_archives()
    summary = {
        "reference_date": reference_date.isoformat(),
        "cny_rub": cny_rub,
        "eur_rub": eur_rub,
        "requested": len(targets),
        "completed": len(targets) - len(failures),
        "failed": len(failures),
        "groups": {group: len(lines) for group, lines in catalogs.items()},
    }
    (OUTPUT_ROOT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
