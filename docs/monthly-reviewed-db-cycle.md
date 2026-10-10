# 월간 애니 작품 DB 동기화 (한국시간)

**1일 03:00과 04:00은 월넛파이에서 실행한다.** GitHub의 월간
수집/릴리즈 작업은 **기존 오전 10:17**에 실행한다.

| 한국시간 | 실행 위치 | 동작 |
|---|---|---|
| 매월 1일 03:00 | Pi systemd | PostgreSQL에서 관리자 확정 작품 연결 조회/검증, 안전한 로컬 JSON 스냅샷 생성 |
| 매월 1일 04:00 | Pi systemd | 03:00 스냅샷 파일만 GitHub Release에 업로드 |
| 매월 1일 10:17 | GitHub Actions | 업로드한 작품 ID를 db-v1 SQLite에 먼저 병합, 신규 DB 수집, 최종 릴리즈 게시 |
| 매월 2일 02:00 | Pi systemd | 해당 월 성공 마커와 GitHub 파일 SHA-256 검사 후 최신 DB 적용 |

## 안전장치

- GitHub에 업로드하는 리뷰 JSON에는 **작품 ID, 작품 제목, 시즌/형식**만
  저장한다. Pi 디렉터리, ZIP 파일명, 자막 내용 및 사용자 정보는 업로드하지 않는다.
- 관리자가 수동으로 저장한 Pi 작품↔자막 연결은 저장 즉시 사용 가능하다.
  월간 게시를 기다리지 않는다.
- 03:00 Pi DB 조회나 검증이 실패하면 04:00 업로드는 실패한다. 04:00은
  PostgreSQL 재조회 대신 준비된 그 달 파일을 사용한다.
- 04:00 Pi 업로드가 실패/누락되면 10:17 GitHub Action은 릴리즈를
  덮어쓰지 않는다. 확정 검수 ID의 충돌·시즌 혼동도 검증에서 중단한다.
- GitHub Action은 기존 릴리즈의 SHA-256 변경 여부를 확인하고
  백업 ZIP을 올린 다음 최종 ZIP을 게시한다. 새 릴리즈 게시 확인 후
  `monthly-db-refresh-YYYY-MM.json` 성공 마커를 업로드한다.
- 2일 Pi 동기화는 성공 마커와 실제 GitHub 릴리즈 SHA-256이 맞을 때만
  실행한다. 기존 Pi 관리자 수동 자막 경로 기록은 보존한다.
- GitHub Actions 실행은 플랫폼 사정으로 예약 시각보다 지연될 수 있다.
  2일까지 완료되지 않았으면 수동으로 실패 단계를 복구해야 한다.

## 한 번만 Pi에 적용

먼저 **streaming-project**와 **anime-db-download** 두 GitHub PR을 머지한다.

```bash
cd ~/streaming-project && git pull --ff-only
cd ~/anime-db-download && git pull --ff-only
```

Pi의 `/home/pi/.config/streaming-project/anime-db.env` 파일에
기존 릴리즈 다운로드 토큰 `ANIME_DB_GITHUB_TOKEN` 외에 **GitHub
fine-grained upload 토큰** `ANIME_DB_GITHUB_UPLOAD_TOKEN`을 설정한다.
권한은 `pingpingjinping/anime-db-download` 저장소의
**Contents: Read and write**로 제한한다. 토큰 값은 채팅/로그/커밋에
붙여넣지 말고 Pi 계정 전용 권한(0600)으로 관리한다.

03시/04시 및 2일 타이머를 설치/교체한다.

```bash
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-review-export.service /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-review-export.timer /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-review-upload.service /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-review-upload.timer /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-sync.service /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable anime-monthly-review-export.timer anime-monthly-review-upload.timer anime-monthly-sync.timer
sudo systemctl restart anime-monthly-review-export.timer anime-monthly-review-upload.timer anime-monthly-sync.timer
systemctl list-timers --all | grep anime-monthly
```

검증 전용 테스트. **실제 GitHub 업로드/DB 변경 없음.**

```bash
cd ~/anime-db-download
python3 scripts/pi_export_reviewed_work_groups.py --prepare --dry-run --force
```

운영 로그:

```bash
journalctl -u anime-monthly-review-export.service -n 80 --no-pager
journalctl -u anime-monthly-review-upload.service -n 80 --no-pager
journalctl -u anime-monthly-sync.service -n 80 --no-pager
```

첫째 날 03시와 04시는 서로 다른 Pi systemd 서비스이며
GitHub Actions가 아니다. GitHub의 10:17 월간 작업은
`.github/workflows/monthly-db.yml` 하나로 관리한다.
