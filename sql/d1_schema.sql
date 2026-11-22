-- AED Care · Cloudflare D1(SQLite) 스키마
-- 무료 한도(하루 쓰기 10만 행)를 지키기 위해 보조 인덱스를 두지 않습니다.
-- 행 1건 저장 = 쓰기 1행. 지도 조회는 서버가 전체를 한 번 읽어 메모리에서 처리합니다.
CREATE TABLE IF NOT EXISTS aed_locations (
  source_record_id TEXT PRIMARY KEY,          -- SafeMap objt_id (없으면 fingerprint:sha256)
  source TEXT NOT NULL DEFAULT 'safemap',
  name TEXT NOT NULL,
  address TEXT NOT NULL DEFAULT '',
  detail_address TEXT NOT NULL DEFAULT '',
  sido TEXT NOT NULL DEFAULT '',
  gugun TEXT NOT NULL DEFAULT '',
  postal_code_1 TEXT NOT NULL DEFAULT '',
  postal_code_2 TEXT NOT NULL DEFAULT '',
  latitude REAL NOT NULL CHECK (latitude BETWEEN 33 AND 39),
  longitude REAL NOT NULL CHECK (longitude BETWEEN 124 AND 132),
  source_maker TEXT NOT NULL DEFAULT '',
  source_model TEXT NOT NULL DEFAULT '',
  source_manager TEXT NOT NULL DEFAULT '',     -- 서버 전용, 공개 API로 내보내지 않음
  source_manager_tel TEXT NOT NULL DEFAULT '', -- 서버 전용, 공개 API로 내보내지 않음
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) WITHOUT ROWID;
