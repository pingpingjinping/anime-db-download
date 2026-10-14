# 타작품 연결 교정 2026-10-14

배포 중인 10월 11일 DB 두 ZIP을 해시로 고정해 검사했다. CSV 1~12번 중 실제 교정할 11개 사례를 반영한다.

| 대상 | 교정 |
| --- | --- |
| 코난 극장판 4기 | Anikoto 수동 연결 AniList 1362 → 1363, 카탈로그 연도 1994 → 2000 |
| 방위부 LOVE! LOVE! | AniList 21293 및 해당 ReAnime TV 2기 |
| 방위부 LOVE! LOVE! LOVE! | AniList 98599 및 해당 ReAnime OVA |
| RINGING FATE | Ringing Bell 연결 해제, 기존 Ringing Fate ReAnime 항목 연결 |
| KILLTUBE | Kill Ao 연결 해제, 기존 KILLTUBE ReAnime 항목 연결 |
| 아라포 현자 생활 일기 | 이세계 통판 연결 해제, 정확한 일본어 제목의 기존 ReAnime 항목 연결 |
| Plantopia / Ninja Skooler | 타작품 ID 제거 후 미매칭 유지 |
| Dimension High School / Beyblade X | 다른 작품 ID만 제거, 정확한 연결 유지 |
| BECK 자막 26개 | Anissia 57 나카이모 → AniList 57 BECK, 작품 제목 및 ReAnime 연결 교정 |

CUE! 파일명으로 올라온 자막은 실제 본문에 종말의 하렘 8화 제목과 줄거리 근거가 있는 10월 11일 교정본이다. 이 파일의 연결을 유지한다. 연도 차이 후보 테이블 13건은 실제 적용된 연결 오류와 구분해 이번 교정에 넣지 않는다. CSV 권장 ID 21870은 방위부 2기와 다른 작품이므로 사용하지 않는다.

원본 자막 경로·압축 파일·파일 SHA·기존 회차 매핑은 보존한다. BECK의 원래 잘못된 분류는 출처 기록으로 남기고 유효 작품 연결을 교정한다. 추가 자막 런타임 제목은 명시적인 검수 제목을 우선하며 이전 원본 분류는 그대로 보존한다. 로컬에 없는 AniList 항목을 임의로 생성하지 않는다. 동일한 작품에 대한 기존 정확한 ReAnime 항목을 연결한다.

PR 검증에서 두 DB 무결성, 이전 검수 원장, 관련 없는 테이블 보존, PostgreSQL 재적용·수동 파일 보존·충돌 롤백을 검사한다. 머지 후 워크플로가 현재 릴리즈 해시를 다시 비교하고 원본 두 ZIP을 백업한 다음 교정본을 `db-v1`에 게시한다. 별도 `other-work-20261014` 릴리즈에도 결과를 남긴다.

## WalnutPi 적용

릴리즈 워크플로 성공 후 기존 카탈로그 갱신을 실행한다. 카탈로그 importer는 사용자 수동 연결을 유지한다. BECK 26개는 별도 추가 자막 데이터셋이므로 아래 SQL도 필요하다. 실제 Pi에서 이 명령을 실행하기 전까지 Pi 적용 완료가 아니다.

```bash
cd ~/streaming-project
mkdir -p work-audit/other-work-20261014
gh release download other-work-20261014 --repo pingpingjinping/anime-db-download --pattern 'other-work-20261014.sql' --pattern 'other-work-sha256-20261014.json' --dir work-audit/other-work-20261014
python3 - <<'PY'
from pathlib import Path
import hashlib,json
p=Path('work-audit/other-work-20261014')
m=json.loads((p/'other-work-sha256-20261014.json').read_text())
assert hashlib.sha256((p/'other-work-20261014.sql').read_bytes()).hexdigest()==m['other-work-20261014.sql']
print('SQL SHA256 verified')
PY
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > work-audit/other-work-20261014/postgres-before.dump
sudo systemctl start anime-db-update.service
docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < work-audit/other-work-20261014/other-work-20261014.sql
```

SQL은 `is_manual` 자막을 보존한다. 원래 파일 해시·경로 또는 예상한 기존/교정 타깃이 다르면 전체 트랜잭션을 중단한다. 신규 회차는 배정하지 않는다. 현재 Pi에 수동 지정된 파일의 실제 적용 개수는 SQL의 `applied_files`, `preserved_manual_files`로 확인한다.
