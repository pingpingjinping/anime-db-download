# 2026-10-11 자막 본문 검수 적용

확정한 작품 연결 54개, kiss×sis TV 기존 회차 대상 24행, 명시적으로 확정한 회차 60행을 반영한다. 카탈로그에 없는 특전 4작품은 원래 ReAnime ID와 본문 근거로 최소 항목만 보완했다. 자막 원본·ZIP·SHA·경로는 변경하지 않았다.

기존 자동 importer와의 호환을 위해 legacy 회차 테이블은 12,055행을 유지한다. 신규 60행은 `body_audit_confirmed_episodes_v1`에 기록하며 아래 SQL로 PostgreSQL에 적용한다. 일반 SQLite 가져오기만 실행하면 신규 회차는 적용되지 않는다. `is_manual` 파일은 파일 연결과 해당 파일의 회차를 모두 유지한다. 예상하지 못한 파일 해시, 경로, 기존 회차 또는 새 숫자 ID 충돌이 있으면 전체 트랜잭션을 롤백한다. 같은 SQL의 재적용은 동일한 결과를 유지한다.

## WalnutPi 적용

SSH 원격 실행은 수행하지 않았다. `~/streaming-project`에서 GitHub CLI의 기존 인증으로 다운로드한 뒤 실행한다.

```bash
cd ~/streaming-project
mkdir -p work-audit/body-audit-20261011
gh release download body-audit-20261011 --repo pingpingjinping/anime-db-download --pattern body-audit-20261011.sql --pattern body-audit-sha256.json --dir work-audit/body-audit-20261011
python3 - <<'PY'
from pathlib import Path
import hashlib,json
p=Path('work-audit/body-audit-20261011')
m=json.loads((p/'body-audit-sha256.json').read_text())
assert hashlib.sha256((p/'body-audit-20261011.sql').read_bytes()).hexdigest()==m['body-audit-20261011.sql']
print('SQL SHA256 verified')
PY
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > work-audit/body-audit-20261011/postgres-before.dump
docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < work-audit/body-audit-20261011/body-audit-20261011.sql
```

출력의 `applied_files`와 `preserved_manual_files`를 확인한다. 사용자 수동 지정이 없으면 54개 적용, 0개 수동 유지이며 신규 회차 60행을 삽입한다. 실행 로그가 확인되기 전에는 파이 적용 완료로 간주하지 않는다.

카탈로그와 자막 registry 두 ZIP은 현재 `db-v1`에 반영하며, 바꾸기 전 두 ZIP도 `before-body-audit-20261011` 이름으로 보존한다. 해시와 카탈로그/registry 검증 결과는 릴리즈의 JSON 파일에 기록한다.
