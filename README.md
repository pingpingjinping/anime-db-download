# anime-db-download

AniList, Onnada, Anissia, ReAnime, AniAll 5개 출처의 애니메이션·자막 블로그 자료 DB를 비공개 GitHub Release에 보관하고, 신규 항목만 월별로 추가합니다.

## 월별 갱신

매월 1일 10:17 KST GitHub Actions가 Release `db-v1`의 `anime_four_sources_multilingual_prepared.zip`을 내려받아 갱신합니다.

- AniList: `UPDATED_AT_DESC` 순으로 최근 30일(첫 실행) 또는 저장한 체크포인트 이후만 확인합니다. API 요청 사이에 5초 기다리고, DB에 없는 ID만 추가합니다. 최근 하루를 겹쳐 확인해 페이지 경계에서 생길 수 있는 누락을 줄입니다.
- Anissia: 공개 목록 API에서 최근 페이지를 확인합니다.
- ReAnime: 연도 내림차순 API에서 신규 항목을 확인합니다.
- Onnada: 목록 페이지를 순서대로 확인하고, 새 ID의 상세 페이지는 1.2초 간격으로 가져옵니다.

기존 항목의 출처 간 검토 연결과 결정은 보존합니다. 새 작품과 제목·연도·형식이 바뀐 항목은 `monthly_review`에 기록하고, 새 매칭은 자동 확정하지 않습니다. 목록 형식이 바뀌거나 불완전하면 부분 결과를 Release에 올리지 않고 작업을 실패시킵니다. DB가 바뀌면 같은 `db-v1` Release의 ZIP 자산을 갱신합니다.

## 실행 및 확인

초기 DB ZIP은 Release `db-v1`에 이미 올라가 있습니다. GitHub 저장소의 **Actions → Monthly anime DB refresh → Run workflow**에서 수동 실행할 수 있고, 이후 매월 자동 실행됩니다. 실행 결과는 해당 workflow run 로그에서 출처별 추가 건수를 확인합니다.

## Pi에 반영

갱신된 Release ZIP을 내려받아 SQLite 파일을 압축 해제한 다음, Pi의 `streaming-project`에서 가져옵니다.

```bash
cd ~/streaming-project
git pull --ff-only
docker compose up -d db
python3 scripts/import-anime-db.py /path/to/anime_four_sources_multilingual_prepared.db
docker compose up -d --build anime caddy
docker compose ps
```

Pi importer는 고정 행 수가 아닌 네 출처가 모두 있는지 확인하므로 월별 DB 증가분을 받아들입니다. 기존 스트리밍 링크와 DB의 검토 연결은 importer가 다시 계산하거나 덮어쓰지 않습니다.

새로운 source ID를 발견했다고 AniList·ReAnime·Anissia 간 매칭이 검증된 것은 아닙니다. `monthly_review`를 검토한 뒤에만 서비스에서 새 연결로 사용합니다.
