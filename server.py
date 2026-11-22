"""AED Care local server and SafeMap API proxy.

The SafeMap key is loaded from the SAFEMAP_SERVICE_KEY environment variable
or the local .env file. It is never sent to the browser.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import d1_client

ROOT = Path(__file__).resolve().parent
UPSTREAM = "https://safemap.go.kr/openapi2/IF_0004"
CACHE_TTL_SECONDS = 300
MAX_ROWS = 500
_cache: dict[tuple[int, int], tuple[float, bytes]] = {}

# 공공 AED 데이터베이스(Cloudflare D1) 조회 설정: API 토큰은 서버에서만 사용합니다.
DB_MAX_LIMIT = 2000
DB_DEFAULT_LIMIT = 800
NEAREST_DEFAULT_RADIUS_M = 3000   # 가까운 AED 검색 기본 반경(직선거리)
NEAREST_MAX_RADIUS_M = 30000
KOREA_LNG = (124.0, 132.0)
KOREA_LAT = (33.0, 39.0)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")


def safe_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_float(value: str | None, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed != parsed or not (minimum <= parsed <= maximum):
        return None
    return parsed


def db_configured() -> bool:
    return d1_client.is_configured()


class DBError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Cloudflare D1 전국 AED 저장소
# D1 무료 한도(하루 읽기 500만 행)를 지키기 위해, 서버가 전체(약 6만 2천 행)를
# 한 번 읽어 메모리·디스크(.cache/aed_d1.json)에 두고 지도 요청은 메모리에서 처리합니다.
# 하루 D1 읽기 ≈ 6만 2천 × (24시간 / DATASET_TTL) 행.
# ---------------------------------------------------------------------------
DATASET_TTL_SECONDS = 6 * 3600
D1_PAGE_ROWS = 5000
# Vercel 함수는 /tmp만 쓸 수 있습니다(같은 인스턴스가 살아 있는 동안 유지).
_CACHE_DIR_DEFAULT = Path("/tmp/aedcare") if os.environ.get("VERCEL") else ROOT / ".cache"
# AEDCARE_CACHE_DIR: 테스트 등에서 캐시 위치를 바꿀 때 사용 (예: DB 실패 상황 재현)
DATASET_CACHE_FILE = Path(os.environ.get("AEDCARE_CACHE_DIR") or _CACHE_DIR_DEFAULT) / "aed_d1.json"
# 공개 AED 데이터 응답은 CDN(Vercel 등)에 캐시해 함수 실행과 D1 읽기를 줄입니다.
PUBLIC_CACHE = "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400"
ROW_FIELDS = ("id", "name", "address", "detail_address", "sido", "gugun", "latitude", "longitude", "source_maker", "source_model")
_dataset_lock = threading.Lock()
_dataset: dict[str, Any] = {"at": 0.0, "rows": [], "json": b"", "gzip": b"", "count": 0, "generatedAt": ""}


def fetch_all_from_d1() -> list[list[Any]]:
    """기본키 순서로 나눠 읽습니다(OFFSET 없이 읽은 만큼만 과금)."""
    rows: list[list[Any]] = []
    last_id = ""
    while True:
        chunk = d1_client.rows(
            "SELECT source_record_id AS id, name, address, detail_address, sido, gugun, latitude, longitude, "
            "source_maker, source_model FROM aed_locations WHERE source_record_id > ? AND is_active = 1 "
            f"ORDER BY source_record_id LIMIT {D1_PAGE_ROWS}",
            [last_id],
        )
        for item in chunk:
            try:
                lat, lng = float(item["latitude"]), float(item["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append([str(item.get("id") or ""), str(item.get("name") or ""), str(item.get("address") or ""),
                         str(item.get("detail_address") or ""), str(item.get("sido") or ""), str(item.get("gugun") or ""),
                         lat, lng, str(item.get("source_maker") or ""), str(item.get("source_model") or "")])
        if len(chunk) < D1_PAGE_ROWS:
            return rows
        last_id = str(chunk[-1].get("id") or "")
        if not last_id:
            return rows


def build_points_payload(rows: list[list[Any]], generated_at: str) -> bytes:
    """전국 AED 좌표 압축 배열 [위도*1e5, 경도*1e5, 시도번호, ...] + 시도별 건수."""
    sido_names: list[str] = []
    sido_index: dict[str, int] = {}
    sido_stats: dict[int, list[float]] = {}
    flat: list[int] = []
    for row in rows:
        lat, lng = row[6], row[7]
        name = row[4].strip() or "미상"
        if name not in sido_index:
            sido_index[name] = len(sido_names)
            sido_names.append(name)
        idx = sido_index[name]
        flat.extend((round(lat * 1e5), round(lng * 1e5), idx))
        stat = sido_stats.setdefault(idx, [0, 0.0, 0.0])
        stat[0] += 1
        stat[1] += lat
        stat[2] += lng
    sido = [
        {"name": sido_names[i], "count": int(s[0]), "lat": round(s[1] / s[0], 5), "lng": round(s[2] / s[0], 5)}
        for i, s in sorted(sido_stats.items(), key=lambda kv: -kv[1][0])
    ]
    payload = {"count": len(rows), "expected": len(rows), "generatedAt": generated_at, "provider": "cloudflare-d1",
               "scale": 100000, "sidoNames": sido_names, "sido": sido, "points": flat}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _install_dataset(rows: list[list[Any]], at: float, generated_at: str) -> None:
    body = build_points_payload(rows, generated_at)
    _dataset.update(at=at, rows=rows, json=body, gzip=gzip.compress(body, 6), count=len(rows), generatedAt=generated_at)


def get_dataset(force: bool = False) -> dict[str, Any]:
    with _dataset_lock:
        now = time.time()
        if not force and _dataset["rows"] and now - _dataset["at"] < DATASET_TTL_SECONDS:
            return _dataset
        if not force and not _dataset["rows"] and DATASET_CACHE_FILE.exists():
            age = now - DATASET_CACHE_FILE.stat().st_mtime
            if age < DATASET_TTL_SECONDS:
                try:
                    cached = json.loads(DATASET_CACHE_FILE.read_text(encoding="utf-8"))
                    if cached.get("rows"):
                        _install_dataset(cached["rows"], now - age, str(cached.get("generatedAt") or ""))
                        return _dataset
                except (OSError, ValueError):
                    pass
        if not db_configured():
            raise DBError(HTTPStatus.SERVICE_UNAVAILABLE, "DB_NOT_CONFIGURED", "데이터베이스(Cloudflare D1)가 서버에 설정되지 않았습니다.")
        try:
            rows = fetch_all_from_d1()
        except d1_client.D1Error as exc:
            if _dataset["rows"]:  # 갱신 실패 시 이전 데이터로 계속 서비스
                print(f"D1 갱신 실패, 이전 데이터 사용: {exc}")
                _dataset["at"] = now - DATASET_TTL_SECONDS + 600  # 10분 뒤 재시도
                return _dataset
            raise DBError(HTTPStatus.BAD_GATEWAY, "DB_UNREACHABLE", f"데이터베이스 조회 실패: {exc}") from exc
        # 서버 위치(Vercel은 UTC)와 관계없이 한국 시간으로 기록합니다.
        generated_at = time.strftime("%Y-%m-%dT%H:%M:%S+0900", time.gmtime(now + 9 * 3600))
        _install_dataset(rows, now, generated_at)
        try:
            DATASET_CACHE_FILE.parent.mkdir(exist_ok=True)
            DATASET_CACHE_FILE.write_text(json.dumps({"generatedAt": generated_at, "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        except OSError:
            pass
        return _dataset


def get_points() -> dict[str, Any]:
    return get_dataset()


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def aeds_in_bbox(
    min_lng: float, min_lat: float, max_lng: float, max_lat: float,
    limit: int, user_lng: float | None, user_lat: float | None,
) -> list[dict[str, Any]]:
    """영역 안 AED (공개 필드만). 기준점이 있으면 가까운 순."""
    rows = get_dataset()["rows"]
    hits = [r for r in rows if min_lat <= r[6] <= max_lat and min_lng <= r[7] <= max_lng]
    if user_lat is not None and user_lng is not None:
        scored = [(_distance_m(user_lat, user_lng, r[6], r[7]), r) for r in hits]
        scored.sort(key=lambda x: (x[0], x[1][0]))
    else:
        scored = [(None, r) for r in sorted(hits, key=lambda r: r[0])]
    result = []
    for dist, r in scored[:limit]:
        item = dict(zip(ROW_FIELDS, r))
        item["distance_m"] = None if dist is None else round(dist, 2)
        result.append(item)
    return result


def aeds_within_radius(lat: float, lng: float, radius_m: float, limit: int) -> tuple[list[dict[str, Any]], int]:
    """기준점에서 반경(직선거리) 안의 AED를 가까운 순으로 돌려줍니다. (결과, 반경 안 전체 건수)

    사각형 첫 결과에서 멈추지 않고, 반경을 덮는 영역 전체를 거리로 걸러 정렬합니다.
    """
    rows = get_dataset()["rows"]
    # 사전 필터는 넉넉하게(1도≈111.195km, 여유 2%) 잡고, 최종 판정은 하버사인 거리로 한다.
    dlat = radius_m / 111_195.0 * 1.02
    worst_lat = min(89.0, abs(lat) + dlat)
    dlng = dlat / max(0.05, math.cos(math.radians(worst_lat)))
    scored = []
    for r in rows:
        if abs(r[6] - lat) > dlat or abs(r[7] - lng) > dlng:
            continue
        dist = _distance_m(lat, lng, r[6], r[7])
        if dist <= radius_m:
            scored.append((dist, r))
    scored.sort(key=lambda x: (x[0], x[1][0]))
    result = []
    for dist, r in scored[:limit]:
        item = dict(zip(ROW_FIELDS, r))
        item["distance_m"] = round(dist, 2)
        result.append(item)
    return result, len(scored)


def _norm(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def search_aeds(query: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    """이름·주소·상세 위치에 검색어가 모두 들어간 AED (수동 위치 지정용)."""
    terms = [t for t in _norm(query).split(" ") if t]
    if not terms:
        return [], 0
    rows = get_dataset()["rows"]
    hits = []
    for r in rows:
        text = _norm(f"{r[1]} {r[2]} {r[3]} {r[4]} {r[5]}")
        if all(t in text for t in terms):
            hits.append(r)
    hits.sort(key=lambda r: (len(r[1]), r[1], r[0]))
    return [dict(zip(ROW_FIELDS, r)) for r in hits[:limit]], len(hits)


def find_aed(record_id: str) -> dict[str, Any] | None:
    """공공데이터 원본 ID(SafeMap objt_id)로 정확히 일치하는 1건만 찾습니다."""
    for r in get_dataset()["rows"]:
        if r[0] == record_id:
            return dict(zip(ROW_FIELDS, r))
    return None


def prewarm_points() -> None:
    if not db_configured():
        return
    try:
        info = get_dataset()
        print(f"Cloudflare D1: 전국 AED {info['count']}건 준비 완료")
    except DBError as exc:
        print(f"Cloudflare D1: 전국 데이터 준비 실패 ({exc.message})")


class AEDCareHandler(SimpleHTTPRequestHandler):
    server_version = "AEDCare/1.0"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        # 로컬 개발·IDE webview 미리보기를 허용합니다. 실제 배포에서는
        # 허용할 도메인을 정한 CSP frame-ancestors 정책을 적용하세요.
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Browser requests never include the SafeMap key. Keep logs concise.
        super().log_message(format, *args)

    def send_json(self, status: int, payload: dict[str, Any], cache: str = "no-store") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def is_private_path(path: str) -> bool:
        decoded = urllib.parse.unquote(path)
        return any(part.startswith(".") for part in decoded.split("/") if part)

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if self.is_private_path(parsed.path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_HEAD()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if self.is_private_path(parsed.path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "safemapConfigured": bool(os.environ.get("SAFEMAP_SERVICE_KEY", "").strip()),
                    "dbConfigured": db_configured(),
                    "dbProvider": "cloudflare-d1",
                    "maxRows": MAX_ROWS,
                },
            )
            return
        if parsed.path == "/api/safemap/aed":
            self.handle_safemap(parsed.query)
            return
        if parsed.path == "/api/aeds":
            self.handle_aeds_bbox(parsed.query)
            return
        if parsed.path == "/api/aeds/nearest":
            self.handle_aeds_nearest(parsed.query)
            return
        if parsed.path == "/api/aeds/points":
            self.handle_aeds_points()
            return
        if parsed.path == "/api/aeds/search":
            self.handle_aeds_search(parsed.query)
            return
        if parsed.path == "/api/aeds/item":
            self.handle_aeds_item(parsed.query)
            return
        super().do_GET()

    def handle_aeds_points(self) -> None:
        """GET /api/aeds/points — 전국 AED 좌표 묶음 (gzip 지원)."""
        try:
            info = get_points()
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        use_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        body = info["gzip"] if use_gzip else info["json"]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", PUBLIC_CACHE)
        self.end_headers()
        self.wfile.write(body)

    def handle_aeds_bbox(self, query: str) -> None:
        """GET /api/aeds?bbox=minLng,minLat,maxLng,maxLat[&limit=&lat=&lng=]"""
        params = urllib.parse.parse_qs(query)
        parts = ((params.get("bbox") or [""])[0]).split(",")
        if len(parts) != 4:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_BBOX", "message": "bbox=minLng,minLat,maxLng,maxLat 형식이 필요합니다."})
            return
        min_lng = parse_float(parts[0], -180, 180)
        min_lat = parse_float(parts[1], -90, 90)
        max_lng = parse_float(parts[2], -180, 180)
        max_lat = parse_float(parts[3], -90, 90)
        if None in (min_lng, min_lat, max_lng, max_lat):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_BBOX", "message": "bbox 좌표가 올바르지 않습니다."})
            return
        # 국내 범위로 잘라 RPC 조건(124~132, 33~39)을 맞춥니다.
        min_lng, max_lng = max(min_lng, KOREA_LNG[0]), min(max_lng, KOREA_LNG[1])
        min_lat, max_lat = max(min_lat, KOREA_LAT[0]), min(max_lat, KOREA_LAT[1])
        if min_lng >= max_lng or min_lat >= max_lat:
            self.send_json(HTTPStatus.OK, {"items": [], "count": 0, "limit": 0, "truncated": False})
            return
        limit = safe_int((params.get("limit") or [None])[0], DB_DEFAULT_LIMIT, 1, DB_MAX_LIMIT)
        user_lat = parse_float((params.get("lat") or [None])[0], *KOREA_LAT)
        user_lng = parse_float((params.get("lng") or [None])[0], *KOREA_LNG)
        if user_lat is None or user_lng is None:
            user_lat = user_lng = None
        try:
            rows = aeds_in_bbox(min_lng, min_lat, max_lng, max_lat, limit, user_lng, user_lat)
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        self.send_json(HTTPStatus.OK, {"items": rows, "count": len(rows), "limit": limit, "truncated": len(rows) >= limit}, PUBLIC_CACHE)

    def handle_aeds_nearest(self, query: str) -> None:
        """GET /api/aeds/nearest?lat=&lng=[&radius=3000&limit=10]

        정의된 검색 반경(직선거리) 안의 AED를 가까운 순으로 돌려줍니다.
        반경 안에 결과가 없으면 items=[] (조회 성공, 0건)입니다.
        """
        params = urllib.parse.parse_qs(query)
        lat = parse_float((params.get("lat") or [None])[0], *KOREA_LAT)
        lng = parse_float((params.get("lng") or [None])[0], *KOREA_LNG)
        if lat is None or lng is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_LOCATION", "message": "국내 위도(lat)·경도(lng)가 필요합니다."})
            return
        limit = safe_int((params.get("limit") or [None])[0], 10, 1, 50)
        radius = safe_int((params.get("radius") or [None])[0], NEAREST_DEFAULT_RADIUS_M, 100, NEAREST_MAX_RADIUS_M)
        try:
            rows, total = aeds_within_radius(lat, lng, float(radius), limit)
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        self.send_json(
            HTTPStatus.OK,
            {"items": rows, "count": len(rows), "totalInRadius": total, "radius_m": radius, "limit": limit,
             "truncated": total > len(rows), "distanceType": "straight_line"},
            PUBLIC_CACHE,
        )

    def handle_aeds_search(self, query: str) -> None:
        """GET /api/aeds/search?q=부산시청&limit=10 — 장소·주소로 기준 위치를 직접 고를 때 사용."""
        params = urllib.parse.parse_qs(query)
        q = ((params.get("q") or [""])[0]).strip()[:60]
        if len(q) < 2:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_QUERY", "message": "검색어를 2자 이상 입력하세요."})
            return
        limit = safe_int((params.get("limit") or [None])[0], 10, 1, 30)
        try:
            rows, total = search_aeds(q, limit)
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        self.send_json(HTTPStatus.OK, {"items": rows, "count": len(rows), "total": total}, PUBLIC_CACHE)

    def handle_aeds_item(self, query: str) -> None:
        """GET /api/aeds/item?id=SafeMap원본ID — 원본 ID와 정확히 일치하는 공공 AED 1건 (연결 검증용)."""
        params = urllib.parse.parse_qs(query)
        record_id = ((params.get("id") or [""])[0]).strip()[:120]
        if not record_id:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_ID", "message": "공공데이터 원본 ID가 필요합니다."})
            return
        try:
            item = find_aed(record_id)
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        if not item:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND", "message": "해당 공공데이터 원본 ID가 DB에 없습니다."})
            return
        self.send_json(HTTPStatus.OK, {"item": item}, PUBLIC_CACHE)

    def handle_safemap(self, query: str) -> None:
        service_key = os.environ.get("SAFEMAP_SERVICE_KEY", "").strip().lstrip("\ufeff").strip()
        if not service_key:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "SAFEMAP_NOT_CONFIGURED", "message": "SafeMap API 키가 서버에 설정되지 않았습니다."},
            )
            return

        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        page_no = safe_int((params.get("pageNo") or [None])[0], 1, 1, 100000)
        num_rows = safe_int((params.get("numOfRows") or [None])[0], 100, 1, MAX_ROWS)
        cache_key = (page_no, num_rows)
        cached = _cache.get(cache_key)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cached[1])))
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("X-AEDCare-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached[1])
            return

        upstream_query = urllib.parse.urlencode(
            {
                "serviceKey": service_key,
                "pageNo": page_no,
                "numOfRows": num_rows,
                "returnType": "json",
            }
        )
        request = urllib.request.Request(
            f"{UPSTREAM}?{upstream_query}",
            headers={"Accept": "application/json", "User-Agent": "AEDCare/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            header = payload.get("header") or payload.get("response", {}).get("header") or {}
            if str(header.get("resultCode", "")) != "00":
                self.send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "SAFEMAP_UPSTREAM_ERROR",
                        "resultCode": header.get("resultCode"),
                        "message": header.get("errorMsg") or header.get("resultMsg") or "SafeMap 조회에 실패했습니다.",
                    },
                )
                return
            normalized = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            _cache[cache_key] = (time.time(), normalized)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(normalized)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("X-AEDCare-Cache", "MISS")
            self.end_headers()
            self.wfile.write(normalized)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "SAFEMAP_UNREACHABLE", "message": "SafeMap 서버 응답을 확인할 수 없습니다.", "detail": type(exc).__name__},
            )


def main() -> None:
    host = os.environ.get("AEDCARE_HOST", "127.0.0.1")
    port = safe_int(os.environ.get("AEDCARE_PORT"), 5500, 1, 65535)
    handler = partial(AEDCareHandler, directory=str(ROOT))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"AED Care running at http://{host}:{port}")
    print("SafeMap proxy: configured" if os.environ.get("SAFEMAP_SERVICE_KEY") else "SafeMap proxy: not configured")
    print("Cloudflare D1: configured" if db_configured() else "Cloudflare D1: not configured")
    threading.Thread(target=prewarm_points, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
