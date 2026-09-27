# anime-db-download

AniList, ReAnime, Onnada, Anissia 네 출처의 애니메이션 DB를 비공개 GitHub Release에 보관하고, 신규 항목만 월별로 추가합니다. AniAll은 이 기준 DB에 넣지 않고 별도의 매칭 작업에서만 참조합니다.

## 기준 DB 월별 갱신

매월 1일 10:17 KST GitHub Actions가 Release `db-v1`의 `anime_four_sources_multilingual_prepared.zip`을 내려받아 네 출처만 갱신합니다.

- AniList: `UPDATED_AT_DESC` 순으로 최근 30일(첫 실행) 또는 저장한 체크포인트 이후만 확인합니다. API 요청 사이에 5초 기다리고, DB에 없는 ID만 추가합니다. 최근 하루를 겹쳐 확인해 페이지 경계 누락을 줄입니다.
- Anissia: 공개 목록 API에서 최근 페이지를 확인합니다.
- ReAnime: 연도 내림차순 API에서 신규 항목을 확인합니다.
- Onnada: 목록 페이지와 신규 상세 페이지를 1.2초 간격으로 확인합니다.



초기 DB ZIP은 Release `db-v1`에 올라가 있습니다. 기준 DB를 수동 갱신하려면 **Actions → Monthly anime DB refresh → Run workflow**를 실행합니다.

기존 출처 간 검토 연결과 결정은 보존합니다. 새 작품과 제목·연도·형식이 바뀐 항목은 `monthly_review`에 기록하고, 새 매칭을 자동 확정하지 않습니다. DB가 바뀌면 같은 `db-v1` Release의 ZIP 자산을 갱신합니다.

## AniAll 별도 매칭

AniAll은 기준 DB의 출처 행으로 저장하지 않습니다. 매월 1일 12:17 KST 별도의 **Aniall Match Audit** workflow가 최신 4출처 DB를 받아 AniAll 자막자료실 전체 목록을 천천히 다시 확인하고, 제목·연도·시즌·형식 후보를 대조합니다. 시즌 종료 뒤 예전 작품의 자막 글이 추가될 수 있어, 최근 글만 훑지 않고 전체 목록을 다시 대조합니다.

결과는 기준 DB ZIP과 분리된 GitHub Actions artifact로 보관하며, 후보는 검토용입니다. workflow는 **Actions → Aniall Match Audit → Run workflow**에서 수동 실행할 수도 있습니다. 기본적으로 1.2초 간격으로 요청합니다.

## Pi에 기준 DB 반영

갱신된 Release ZIP을 내려받아 SQLite 파일을 압축 해제한 다음, Pi의 `streaming-project`에서 가져옵니다.

```bash
cd ~/streaming-project
git pull --ff-only
docker compose up -d db
python3 scripts/import-anime-db.py /path/to/anime_four_sources_multilingual_prepared.db
docker compose up -d --build anime caddy
docker compose ps
```

Pi importer는 고정 행 수가 아닌 네 출처가 모두 있는지 확인하므로 월별 DB 증가분을 받아들입니다. 기존 스트리밍 링크와 기준 DB의 검토 연결은 importer가 다시 계산하거나 덮어쓰지 않습니다. AniAll 매칭 후보는 이 기준 DB 파일에 병합하지 않습니다.
