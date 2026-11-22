"""SafeMap 전국 AED를 Cloudflare D1에 적재합니다.

필요한 .env 값: SAFEMAP_SERVICE_KEY, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
D1_DATABASE_ID가 비어 있으면 'aedcare' 데이터베이스를 찾거나 만들고 .env에 기록합니다.

쓰기 한도: 무료 플랜은 하루 10만 행. 처음 적재는 약 6만 2천 행이고,
다시 실행하면 내용이 바뀐 행만 갱신하므로 쓰기가 거의 생기지 않습니다.

사용 예:
  python scripts\\sync_safemap_to_d1.py --dry-run --limit-pages 1
  python scripts\\sync_safemap_to_d1.py --limit-pages 1
  python scripts\\sync_safemap_to_d1.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import d1_client  # noqa: E402

SAFEMAP_URL = "https://safemap.go.kr/openapi2/IF_0004"
MAX_STATEMENT_BYTES = 90_000  # D1 SQL 문장 최대 100KB
COLUMNS = (
    "source_record_id", "source", "name", "address", "detail_address", "sido", "gugun",
    "postal_code_1", "postal_code_2", "latitude", "longitude", "source_maker", "source_model",
    "source_manager", "source_manager_tel", "is_active", "created_at", "updated_at",
)
# 내용이 바뀐 경우에만 갱신(쓰기 절약). created_at은 유지.
COMPARED = [c for c in COLUMNS if c not in ("source_record_id", "created_at", "updated_at")]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def set_env_value(path: Path, key: str, value: str) -> None:
    """.env의 key 값을 채웁니다(다른 줄은 그대로)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def safemap_page(service_key: str, page_no: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
    query = urllib.parse.urlencode({"serviceKey": service_key, "pageNo": page_no, "numOfRows": page_size, "returnType": "json"})
    last: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(f"{SAFEMAP_URL}?{query}", headers={"Accept": "application/json", "User-Agent": "AEDCare-Sync/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            header = payload.get("header", {})
            if str(header.get("resultCode", "")) != "00":
                raise RuntimeError(header.get("errorMsg") or header.get("resultMsg") or "SafeMap error")
            body = payload.get("body", {})
            item = body.get("items", {}).get("item", [])
            rows = item if isinstance(item, list) else ([item] if item else [])
            return rows, int(body.get("totalCount") or 0)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"SafeMap 요청 실패: {type(last).__name__}")


def text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def source_id(row: dict[str, Any]) -> str:
    original = text(row, "objt_id")
    if original:
        return original
    fingerprint = "|".join([text(row, k) for k in ("org", "addres", "buildaddress", "buildplace", "lat", "lon")]).lower()
    return "fingerprint:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def map_row(row: dict[str, Any], now: str) -> dict[str, Any] | None:
    try:
        latitude = float(row.get("lat") or row.get("y"))
        longitude = float(row.get("lon") or row.get("x"))
    except (TypeError, ValueError):
        return None
    if not (33 <= latitude <= 39 and 124 <= longitude <= 132):
        return None
    return {
        "source_record_id": source_id(row),
        "source": "safemap",
        "name": text(row, "org") or text(row, "buildplace") or "AED 설치처",
        "address": text(row, "addres"),
        "detail_address": text(row, "buildaddress") or text(row, "buildplace"),
        "sido": text(row, "sido"),
        "gugun": text(row, "gugun"),
        "postal_code_1": text(row, "zipcode1"),
        "postal_code_2": text(row, "zipcode2"),
        "latitude": latitude,
        "longitude": longitude,
        "source_maker": text(row, "mfg"),
        "source_model": text(row, "model"),
        "source_manager": text(row, "manager"),
        "source_manager_tel": text(row, "managertel"),
        "is_active": 1,
        "created_at": now,
        "updated_at": now,
    }


def upsert_statements(rows: list[dict[str, Any]]) -> list[str]:
    """100KB 이하 INSERT ... ON CONFLICT 문장들로 나눕니다."""
    head = f"INSERT INTO aed_locations ({', '.join(COLUMNS)}) VALUES "
    tail = (
        " ON CONFLICT(source_record_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in COMPARED + ["updated_at"])
        + " WHERE " + " OR ".join(f"aed_locations.{c} IS NOT excluded.{c}" for c in COMPARED)
    )
    budget = MAX_STATEMENT_BYTES - len(head.encode()) - len(tail.encode())
    statements: list[str] = []
    values: list[str] = []
    size = 0
    for row in rows:
        tuple_sql = "(" + ", ".join(d1_client.sql_literal(row[c]) for c in COLUMNS) + ")"
        tuple_bytes = len(tuple_sql.encode("utf-8")) + 2
        if values and size + tuple_bytes > budget:
            statements.append(head + ", ".join(values) + tail)
            values, size = [], 0
        values.append(tuple_sql)
        size += tuple_bytes
    if values:
        statements.append(head + ", ".join(values) + tail)
    return statements


def clear_server_cache() -> None:
    for name in ("aed_d1.json",):
        target = ROOT / ".cache" / name
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SafeMap AED data to Cloudflare D1")
    parser.add_argument("--page-size", type=int, default=500, choices=[100, 200, 500])
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--limit-pages", type=int, default=0, help="0이면 전체")
    parser.add_argument("--dry-run", action="store_true", help="SafeMap 조회·SQL 생성만 하고 D1에 쓰지 않음")
    args = parser.parse_args()

    env_path = ROOT / ".env"
    load_dotenv(env_path)
    safemap_key = os.environ.get("SAFEMAP_SERVICE_KEY", "").strip()
    if not safemap_key:
        print("SAFEMAP_SERVICE_KEY가 없습니다.", file=sys.stderr)
        return 2

    if not args.dry_run:
        if not d1_client.account_config():
            print("CLOUDFLARE_ACCOUNT_ID 또는 CLOUDFLARE_API_TOKEN이 .env에 없습니다.", file=sys.stderr)
            return 2
        try:
            if not d1_client.database_id():
                uuid, created = d1_client.find_or_create_database()
                set_env_value(env_path, "D1_DATABASE_ID", uuid)
                print(f"D1 데이터베이스 '{d1_client.DEFAULT_DATABASE_NAME}' {'생성' if created else '확인'} · .env에 D1_DATABASE_ID 기록")
            d1_client.query((ROOT / "sql" / "d1_schema.sql").read_text(encoding="utf-8"))
            print("스키마 적용 완료")
        except d1_client.D1Error as exc:
            print(f"D1 준비 실패: {exc}", file=sys.stderr)
            return 1

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    first_rows, total = safemap_page(safemap_key, args.start_page, args.page_size)
    total_pages = math.ceil(total / args.page_size)
    end_page = total_pages if args.limit_pages <= 0 else min(total_pages, args.start_page + args.limit_pages - 1)
    processed = mapped_total = skipped = written = 0
    seen: set[str] = set()

    for page in range(args.start_page, end_page + 1):
        rows = first_rows if page == args.start_page else safemap_page(safemap_key, page, args.page_size)[0]
        mapped = []
        for row in rows:
            item = map_row(row, now)
            if not item or item["source_record_id"] in seen:
                skipped += 1
                continue
            seen.add(item["source_record_id"])
            mapped.append(item)
        processed += len(rows)
        mapped_total += len(mapped)
        statements = upsert_statements(mapped)
        page_written = 0
        if not args.dry_run:
            for sql in statements:
                try:
                    results = d1_client.query(sql)
                except d1_client.D1Error as exc:
                    print(f"page {page}: D1 쓰기 실패 - {exc}", file=sys.stderr)
                    print(f"중단 위치: --start-page {page} 로 이어서 실행할 수 있습니다.", file=sys.stderr)
                    return 1
                page_written += sum(int((r.get("meta") or {}).get("rows_written") or 0) for r in results)
        written += page_written
        print(f"page {page}/{total_pages}: received={len(rows)} mapped={len(mapped)} statements={len(statements)} rows_written={page_written}", flush=True)
        if not rows:
            break

    if args.dry_run:
        print(f"DRY RUN: expected={total} processed={processed} mapped={mapped_total} skipped={skipped}")
        return 0
    clear_server_cache()
    count = d1_client.rows("SELECT COUNT(*) AS n FROM aed_locations WHERE is_active = 1")
    print(f"SYNC COMPLETE: expected={total} processed={processed} mapped={mapped_total} skipped={skipped} rows_written={written} d1_active_rows={count[0]['n'] if count else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
