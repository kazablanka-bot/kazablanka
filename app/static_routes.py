"""مصدر ثابت لصور الباركودات وأكواد الدفع داخل المشروع."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.models import RouteCode

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROUTES_FILE = PROJECT_ROOT / "assets" / "routes.json"


@dataclass(frozen=True)
class StaticRoute:
    payment_address: str
    barcode_path: Path


def load_static_routes() -> dict[RouteCode, StaticRoute]:
    """يقرأ الأكواد ومسارات الصور من assets/routes.json ويتحقق من وجودها."""
    try:
        raw = json.loads(STATIC_ROUTES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"ملف إعدادات المسارات غير موجود: {STATIC_ROUTES_FILE}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ملف المسارات غير صالح JSON: {STATIC_ROUTES_FILE}") from error

    result: dict[RouteCode, StaticRoute] = {}
    missing: list[str] = []
    for code in RouteCode:
        item = raw.get(code.value)
        if not isinstance(item, dict):
            missing.append(f"{code.value}: إعداد مفقود")
            continue
        address = item.get("payment_address")
        barcode = item.get("barcode")
        if not isinstance(address, str) or not address.strip():
            missing.append(f"{code.value}: payment_address مفقود")
            continue
        if not isinstance(barcode, str) or not barcode.strip():
            missing.append(f"{code.value}: barcode مفقود")
            continue
        barcode_path = (PROJECT_ROOT / barcode).resolve()
        assets_root = (PROJECT_ROOT / "assets" / "barcodes").resolve()
        if assets_root not in barcode_path.parents:
            missing.append(f"{code.value}: مسار الصورة خارج مجلد الأصول ({barcode})")
            continue
        if not barcode_path.is_file():
            missing.append(f"{code.value}: الصورة غير موجودة ({barcode})")
            continue
        result[code] = StaticRoute(payment_address=address.strip(), barcode_path=barcode_path)

    if missing:
        raise RuntimeError(
            "إعدادات الباركودات الثابتة غير مكتملة:\n- " + "\n- ".join(missing)
        )
    return result
