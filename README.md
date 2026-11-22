# AED Care

단일 HTML 프런트엔드와 Python 표준 라이브러리 서버(SafeMap 프록시 + Cloudflare D1 조회)로 구성됩니다.

## 로컬 실행

Python 3.10 이상(표준 라이브러리만 사용)이 필요합니다.

```powershell
Copy-Item .env.example .env      # 값 채우기 (DB 없이도 실행 가능)
python server.py                 # http://localhost:5500
```

- `.env`에 Cloudflare D1 값이 없으면 앱은 실행되지만, 지도에 **'DB 설정 누락'** 안내가 나오고 공공 AED는 조회되지 않습니다. 기관 등록 기기·시연 모드는 그대로 동작합니다.
- D1 값이 있는데 조회가 실패하면 **'DB 조회 실패'**, 조회는 됐지만 결과가 없으면 **'조회 성공 · 0건'**으로 구분해 안내합니다.
- 시연: 관리자(비밀번호 `1234@`) → 운영 설정 → **시연 모드로 전환**. 시연 기기 A·B·C가 운영 데이터와 분리된 저장소(`aedcare.demo.v1`)에 만들어집니다.

## 출입 조건 · 기관 확인 상태 · 대안 추천

- **출입 상태**: 기관이 입력한 요일별 운영시간표(Asia/Seoul)로 `운영시간표상 출입 가능 / 출입 제한 / 출입 미확인`을 계산합니다. 자정을 넘는 구간(`22:00-06:00`), 기관 휴일(운영 설정)과 기기별 예외일을 처리하고, 시간표가 없으면 출입 미확인입니다.
- **기기 상태**: `관리자 점검 기록 있음 / 복구 중 / 사용 불가 / 수리 필요 / 소모품 만료 / 점검 기한 초과 / 상태 미확인`을 기록 근거와 함께 표시합니다. 사용 불가·수리 필요·복구 미완료·소모품 만료는 우선 추천에서 제외합니다.
- **추천**: 기준 위치(내 위치·지도 중심·장소 검색)에서 반경 3km(직선거리) 안의 '확인된 접근 후보'를 가까운 순 최대 3곳 보여 주고, 부족하면 미확인 후보와 부족한 이유를 따로 표시합니다. '접근 어려움 → 다음 후보'로 바로 다음 기기를 찾습니다.
- **공공데이터 연결**: 기관 기기와 공공 AED는 SafeMap 원본 ID가 정확히 일치할 때만 연결합니다. 이름·주소·좌표가 비슷해도 자동으로 합치지 않습니다.
- **현장 제보**: 시민 제보(문이 잠김 / 기기가 보이지 않음 / 위치가 다름)는 검토 대기로 저장되고, 관리자가 승인한 것만 공개됩니다. 메모의 연락처 형태는 저장 전에 지웁니다.
- **반영 범위**: 서버 인증·기관별 쓰기 권한이 없으므로 관리자 상태 변경은 같은 브라우저(localStorage)에서만 시민 화면에 반영됩니다. 운영 DB 반영은 서버 인증 구현 후 가능합니다.

## SafeMap 설정

1. `.env.example`을 참고해 `.env`에 `SAFEMAP_SERVICE_KEY`를 설정합니다.
2. 키는 `index.html`에 넣지 않습니다.
3. 공공 AED는 브라우저에 저장하지 않고 Cloudflare D1에서만 조회합니다. 관리자 모드 → 데이터 연동에서 DB 연결 상태와 시도별 건수를 확인합니다.

실제 API 엔드포인트는 `https://safemap.go.kr/openapi2/IF_0004`입니다. `www`가 붙은 호스트에서는 같은 키가 거부될 수 있습니다.

## 공공 AED 데이터베이스 (Cloudflare D1)

### 설정

1. Cloudflare 대시보드 → 계정 홈에서 **Account ID**를 복사합니다.
2. **My Profile → API Tokens → Create Token → Custom token**에서 권한 `Account · D1 · Edit`로 토큰을 만듭니다.
3. `.env`에 값을 넣습니다. (`D1_DATABASE_ID`는 비워 두면 자동 생성·기록)

```text
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...
D1_DATABASE_ID=
```

### 적재

```powershell
python scripts\sync_safemap_to_d1.py --limit-pages 1   # 시험 500건
python scripts\sync_safemap_to_d1.py                   # 전체 약 62,000건
```

- `sql/d1_schema.sql`을 자동 적용합니다. 기본키는 SafeMap `objt_id`이며 다시 실행해도 중복이 생기지 않습니다.
- 내용이 바뀐 행만 갱신하므로 재실행 시 쓰기 사용량이 거의 없습니다.
- 실패하면 표시된 `--start-page N`으로 이어서 실행합니다.

### 무료 한도와 설계

D1 무료 플랜은 하루 쓰기 10만 행, 읽기 500만 행이며, 넘으면 다음 날 00:00 UTC(한국 09:00)까지 조회가 실패합니다.

- 보조 인덱스를 두지 않아 1건 저장 = 쓰기 1행입니다. 전체 첫 적재는 약 6만 2천 행입니다.
- 서버가 시작할 때 D1 전체를 한 번 읽어 메모리와 `.cache/aed_d1.json`에 6시간 보관하고, 지도 요청은 메모리에서 처리합니다. 사용자가 많아도 D1 읽기는 하루 최대 약 25만 행입니다.
- D1 갱신에 실패하면 이전 데이터로 계속 서비스합니다.

### 서버 API

- `GET /api/aeds?bbox=minLng,minLat,maxLng,maxLat&lat=&lng=&limit=800`: 영역 안의 AED (최대 2000건, 기준점이 있으면 가까운 순)
- `GET /api/aeds/nearest?lat=&lng=&radius=3000&limit=10`: 반경(직선거리, 100m~30km) 안 AED를 거리순으로 반환 (`totalInRadius`, `distanceType: straight_line`)
- `GET /api/aeds/search?q=부산시청`: 이름·주소로 기준 위치를 직접 고를 때 사용
- `GET /api/aeds/item?id=원본ID`: SafeMap 원본 ID와 정확히 일치하는 1건 (연결 검증용, 없으면 404)
- `GET /api/aeds/points`: 전국 AED 좌표 전체와 시도별 건수 (gzip)
- 응답에는 이름·주소·좌표·제조사·모델·거리만 포함되며 담당자 이름·연락처는 포함하지 않습니다.
- 지도는 전국에서 시작합니다. 확대 수준 14 미만은 숫자 묶음, 14 이상은 개별 위치와 상세 정보를 표시합니다.

## Vercel 배포

운영 주소: https://aed-care.vercel.app

- 정적 파일(`index.html`, `vendor/`)은 Vercel CDN이, `/api/*`는 Python 함수 `api/index.py`(서울 `icn1`)가 처리합니다. 함수는 `server.py`의 API 코드를 그대로 사용합니다.
- 환경 변수 `SAFEMAP_SERVICE_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `D1_DATABASE_ID`를 Vercel 프로젝트(Production)에 비밀값으로 등록합니다.
- 공공 AED 응답은 CDN에 1시간 캐시합니다. 함수가 새로 시작될 때만 D1 전체(약 6만 2천 행)를 읽습니다.
- 다시 배포: `npx vercel deploy --prod`

## 보안 범위

현재 관리자 로그인과 관리 기기 저장(브라우저 localStorage)은 시연용입니다. 실제 배포 전 서버 인증, 기관별 권한, 비밀관리, 백업을 추가해야 합니다.

## 지도 실행 방식

- Leaflet 1.9.4를 `vendor/leaflet`에서 로컬 제공하므로 CDN 차단의 영향을 받지 않습니다.
- 배경 타일은 OpenStreetMap, OpenStreetMap DE, CARTO 순서로 자동 전환됩니다.
- 모든 외부 타일이 차단돼도 등록된 위도·경도로 좌표 기반 오프라인 지도를 표시합니다.
- 지도 데이터 및 타일 제공자의 이용조건과 저작자 표시를 유지해야 합니다.

## Third-party license

Leaflet 1.9.4 is included under the BSD 2-Clause License. See `vendor/leaflet/LICENSE`.
