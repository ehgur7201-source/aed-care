"""Vercel Python 함수 진입점: /api/* 요청을 server.py의 API 핸들러로 처리합니다.

정적 파일(index.html, vendor/ 등)은 Vercel CDN이 직접 제공하고,
비밀값(SafeMap 키, Cloudflare 토큰)은 Vercel 환경 변수에서만 읽습니다.
"""

import sys
import urllib.parse
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import AEDCareHandler  # noqa: E402

API_PATHS = {"/api/health", "/api/safemap/aed", "/api/aeds", "/api/aeds/nearest", "/api/aeds/points"}


class handler(AEDCareHandler):
    """API 경로만 허용하고, 그 밖의 경로는 파일을 노출하지 않도록 404로 응답합니다."""

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path not in API_PATHS:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()
