"""Cloudflare D1 REST API 클라이언트 (서버·스크립트 공용).

필요한 .env 값 (서버 전용, 브라우저로 보내지 않음):
  CLOUDFLARE_ACCOUNT_ID   Cloudflare 계정 ID
  CLOUDFLARE_API_TOKEN    권한: Account > D1 > Edit
  D1_DATABASE_ID          D1 데이터베이스 UUID (비어 있으면 sync 스크립트가 만들고 채웁니다)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_DATABASE_NAME = "aedcare"


class D1Error(Exception):
    """D1 호출 실패. message에는 토큰 등 비밀값을 넣지 않습니다."""


def account_config() -> tuple[str, str] | None:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account or not token:
        return None
    return account, token


def database_id() -> str:
    return os.environ.get("D1_DATABASE_ID", "").strip()


def is_configured() -> bool:
    return account_config() is not None and bool(database_id())


def _api(method: str, path: str, payload: Any = None, timeout: int = 40, attempts: int = 3) -> Any:
    config = account_config()
    if not config:
        raise D1Error("CLOUDFLARE_ACCOUNT_ID 또는 CLOUDFLARE_API_TOKEN이 설정되지 않았습니다.")
    account, token = config
    url = f"{API_BASE}/accounts/{urllib.parse.quote(account)}{path}"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "AEDCare/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not data.get("success", False):
                raise D1Error(_error_text(data))
            return data.get("result")
        except urllib.error.HTTPError as exc:
            try:
                detail = _error_text(json.loads(exc.read().decode("utf-8", "replace")))
            except (json.JSONDecodeError, ValueError):
                detail = ""
            # 4xx(권한·SQL 오류·한도 초과)는 재시도해도 같으므로 바로 알린다. 429만 잠시 후 재시도.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise D1Error(f"HTTP {exc.code} {detail}".strip()) from None
            last = D1Error(f"HTTP {exc.code} {detail}".strip())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    raise D1Error(f"Cloudflare API 요청 실패: {last}")


def _error_text(data: dict[str, Any]) -> str:
    errors = data.get("errors") or []
    return "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors if isinstance(e, dict))[:400]


def query(sql: str, params: list[Any] | None = None, db_id: str | None = None) -> list[dict[str, Any]]:
    """SQL 1개(또는 ;로 이은 여러 개)를 실행하고 각 문장의 결과 객체 목록을 돌려줍니다."""
    target = db_id or database_id()
    if not target:
        raise D1Error("D1_DATABASE_ID가 설정되지 않았습니다.")
    payload: dict[str, Any] = {"sql": sql}
    if params:
        payload["params"] = [None if p is None else str(p) for p in params]
    result = _api("POST", f"/d1/database/{urllib.parse.quote(target)}/query", payload)
    return result if isinstance(result, list) else []


def rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """SELECT 결과 행 목록."""
    results = query(sql, params)
    return list(results[0].get("results") or []) if results else []


def find_or_create_database(name: str = DEFAULT_DATABASE_NAME) -> tuple[str, bool]:
    """이름으로 D1을 찾고 없으면 만듭니다. (uuid, 새로 만들었는지)"""
    found = _api("GET", f"/d1/database?name={urllib.parse.quote(name)}&per_page=50") or []
    for item in found:
        if isinstance(item, dict) and item.get("name") == name and item.get("uuid"):
            return str(item["uuid"]), False
    try:
        created = _api("POST", "/d1/database", {"name": name, "primary_location_hint": "apac"})
    except D1Error:
        created = _api("POST", "/d1/database", {"name": name})
    uuid = str((created or {}).get("uuid") or "")
    if not uuid:
        raise D1Error("D1 데이터베이스를 만들었지만 ID를 받지 못했습니다.")
    return uuid, True


def sql_literal(value: Any) -> str:
    """D1(SQLite) 리터럴. 바인딩 파라미터 한도(100개) 때문에 대량 INSERT에 사용합니다."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return "NULL"
        return repr(value) if isinstance(value, int) else repr(number)
    text = str(value).replace("\x00", "")
    return "'" + text.replace("'", "''") + "'"
