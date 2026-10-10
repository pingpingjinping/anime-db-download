# Cloudflare D1 애니 검색 인덱스 (1차 연결)

대상: anime-search-api.pingpingjinping.workers.dev, D1 anime-search-db.

## 준비

Cloudflare에서 이미 만든 works, work_aliases 테이블과 DB 바인딩은 그대로 사용한다.

Cloudflare 계정의 API Tokens에서 Account → D1 → Edit 권한만 가진 토큰을 만든다. 토큰은 **채팅, 저장소 코드, 로그에 절대 넣지 않는다.**

anime-db-download → Settings → Secrets and variables → Actions에 다음을 등록한다.

| 종류 | 이름 | 값 |
|---|---|---|
| Secret | CLOUDFLARE_API_TOKEN | 발급한 D1 편집 API 토큰 |
| Secret | CLOUDFLARE_ACCOUNT_ID | Cloudflare 계정 ID |
| Variable | CLOUDFLARE_D1_DATABASE_ID | anime-search-db의 Database ID (UUID) |

## 첫 실행

1. 이 PR을 main에 머지한다.
2. Actions → Cloudflare anime search D1 sync → Run workflow → **dry-run**을 실행한다.
3. 로그에서 works, aliases, inserted_rows, SQL 크기와 검증 오류 여부를 확인한다. 이 실행은 Cloudflare를 바꾸지 않는다.
4. 설정을 다시 확인한 후 **publish**로 실행한다. 실제 D1에 작품/별칭 SQL을 넣는다.
5. anime-search-api Workers URL의 /search?q=검색어 경로로 실제 검색 결과를 확인한다.

publish는 D1 works/work_aliases 테이블을 교체하므로 테스트 작품 test_cf_001은 없어지게 된다.

## 데이터 정책

- Release db-v1의 기준 4출처 작품 SQLite 및 검수 자막 registry를 **읽기 전용**으로 사용한다.
- Anissia 작품은 원래 작품 ID(ani_*)를 그대로 사용한다.
- 검수된 AniAll/네이버 파일은 work_confirmed=1이며 기준 DB의 정확한 (출처, 작품 ID) 조합이 있는 것만 검색에 포함한다.
- 제목 유사성만으로 작품을 결합하지 않는다. 시즌, 리메이크, OVA는 다른 ID라면 별도 카드로 유지한다.
- 누적 자막 파일 본체, 영상, 개인 정보, 미확정 후보는 전송하지 않는다. 공개 검색용 작품 ID/제목/별칭/연도/확정 자막 수/상세 링크만 업로드한다.
- Pi에만 존재하는 로컬 수동 매칭 및 주간 새 AniAll 후보는 현재 범위 밖이다.
- 무료 D1 행 쓰기 제한을 고려해 이번 1차는 작품+별칭 35,000행 초과 시 차단한다. 삭제/인덱스 쓰기가 추가되므로 실제 계측 후 상한을 조정한다.
- 현재 Worker의 instr(alias_norm, q) 검색은 인덱스로 최적화된 부분문자열 검색이 아니다. 실사용 속도/읽기 과금을 확인한 뒤 FTS 등 검색 개선이 별도로 필요하다.
- 안정성 확인 전에는 월간 자동 실행을 연결하지 않는다.

## 로컬 테스트

    python3 -m unittest discover -s scripts -p test_export_cloudflare_search_sql.py -v
    python3 scripts/export_cloudflare_search_sql.py --catalog catalog.db --registry registry.db --output import.sql --manifest manifest.json --max-rows 35000
