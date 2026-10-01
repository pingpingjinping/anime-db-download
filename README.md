# anime-db-download

AniList, ReAnime, Onnada, Anissia 네 출처의 애니메이션 DB를 비공개 GitHub Release에 보관하고, 신규 항목만 월별로 추가합니다. AniAll은 이 기준 DB에 넣지 않고 별도의 매칭 작업에서만 참조합니다.

## 기준 DB 월별 갱신

매월 1일 10:17 KST GitHub Actions가 Release `db-v1`의 `anime_four_sources_multilingual_prepared.zip`을 내려받아 네 출처만 갱신합니다.

- AniList: `UPDATED_AT_DESC` 순으로 저장한 체크포인트 이후만 확인합니다. 첫 증분 실행에 체크포인트가 없으면 기존 DB에 저장된 AniList `updatedAt` 최댓값을 시작점으로 삼고, 그 값도 없으면 무작정 장기간 조회하지 않고 중단합니다. API 요청 사이에 5초 기다리며 최근 하루를 겹쳐 확인해 페이지 경계 누락을 줄입니다.
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

## 월별 자막 발견 + Pi 다운로드

자막은 **GitHub에서 실제 파일을 받지 않습니다.** 역할을 둘로 나눕니다.

- GitHub Actions: 신규 자막 후보만 발견해서 queue Release 생성
- Pi: queue에 들어 있는 URL만 실제로 열어서 자막 파일 다운로드

초기 기준 시각은 **2026-09-25 23:59:59 KST**입니다. 이 시각 이전 자료는 이미 수동으로 확보했다고 가정합니다. 첫 discovery는 2026-09-26 이후 항목을 다시 queue에 넣으므로, 첫 PC 수집본과 일부 중복될 수 있지만 누락 방지를 우선합니다.

### GitHub discovery

**Monthly subtitle discovery** workflow는 매월 1일 14:17 KST에 실행됩니다.

- AniAll: 자막자료실 목록과 게시글 메타데이터만 확인합니다. 첨부파일은 다운로드하지 않습니다.
- Anissia: 전체 작품의 caption API만 확인합니다. caption의 `website` 페이지에는 접속하지 않습니다.
- 새 항목은 `subtitle-queue.json`으로 묶습니다.
- Anissia API 자체가 실패한 작품은 다음 실행에서 최초 기준일 이후를 다시 확인합니다.
- discovery state는 `state/subtitle-discovery.json`에 저장합니다.
- queue Release tag는 `subs-queue-YYYY-MM-DD-<run id>-<attempt>` 형식입니다.

queue 예시:

```json
{
  "id": "anissia:...",
  "source": "anissia",
  "anime_no": "3495",
  "anime_title": "정반대의 너와 나 2기",
  "episode": "12",
  "creator": "스모모",
  "source_url": "https://sumomomo.tistory.com/492"
}
```

### Pi 다운로드

Pi는 매월 2일 10:45 KST에 다음 순서로 동작합니다.

1. 기존 `db-v1` Release를 받아 애니 DB import
2. 아직 처리하지 않은 `subs-queue-*` Release를 순서대로 받음
3. queue에 들어 있는 소수의 자막 페이지에만 실제 접속
4. 첨부 ZIP/ASS/SMI 등을 `~/streaming-project/subtitle-data`에 저장
5. 실패 항목은 Pi 로컬 retry state에 남기고 다음 실행에서 먼저 재시도
6. 같은 URL/항목이 3개월 동안 계속 실패하면 4개월차부터 active retry에서 제거

즉 Anissia 수천 작품 API 확인은 GitHub가 하고, Pi는 **신규 후보 몇 건의 실제 페이지/파일 다운로드만** 담당합니다. subtitle 다운로드 프로세스는 `nice -n 15` + `ionice -c3`로 낮은 우선순위로 실행됩니다.

기존 DB updater에서 쓰는 `/home/pi/.config/streaming-project/anime-db.env`의 `ANIME_DB_GITHUB_TOKEN`을 그대로 사용합니다.

Pi 준비:

```bash
cd ~
git clone git@github.com:pingpingjinping/anime-db-download.git
cd ~/anime-db-download

sudo apt update
sudo apt install -y python3-venv

python3 -m venv ~/.local/share/anime-db-download/venv
~/.local/share/anime-db-download/venv/bin/pip install -r ~/anime-db-download/requirements-pi-subtitles.txt

sudo cp ~/anime-db-download/ops/systemd/anime-monthly-sync.service /etc/systemd/system/
sudo cp ~/anime-db-download/ops/systemd/anime-monthly-sync.timer /etc/systemd/system/

# 기존 DB 전용 timer와 중복 실행되지 않게 비활성화
sudo systemctl disable --now streaming-anime-db-update.timer 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable --now anime-monthly-sync.timer
systemctl list-timers anime-monthly-sync.timer
```

수동 테스트:

```bash
sudo systemctl start anime-monthly-sync.service
journalctl -u anime-monthly-sync.service -n 200 --no-pager
```

Pi 다운로드 state는 `~/.local/state/anime-db-download/subtitle-queue-state.json`, 실패 이력은 `~/.local/state/anime-db-download/subtitle-download-failed.jsonl`에 저장됩니다. 실패 기간은 실행 횟수가 아니라 **달력 월 기준**으로 계산합니다. 예를 들어 9월에 처음 실패한 항목은 9·10·11월까지 재시도하고 12월(4개월차)부터 retry에서 제거합니다. 제거된 ID는 재처리 방지를 위해 state의 `retired`에 남고, 상세 이력은 `~/.local/state/anime-db-download/subtitle-download-retired.jsonl`에 기록됩니다. 같은 달에 수동 실행을 여러 번 해도 실패 개월 수는 늘어나지 않습니다. Pi가 한 달 이상 꺼져 있어도 미처리 queue Release를 오래된 순서부터 모두 소비합니다.

다운로드 state에는 진단 메타데이터도 함께 기록합니다.

- `waiting_url.reason`: 회차가 아직 없거나 0인 빈 URL은 `future_or_unreleased`, 회차는 있는데 URL만 비어 있으면 `no_url_yet`로 분리합니다. 이 항목은 retry/retire 개월 수에 포함하지 않습니다.
- `retry.reason`: DNS, SSL, timeout, HTTP 상태, parser, broken link 등을 분류해 마지막 오류와 함께 저장합니다. 실패 JSONL에도 같은 reason이 기록됩니다.
- `success.verification` / `success.coverage`: 다운로드 파일명과 ZIP 내부 자막 파일명을 보수적으로 읽어 요청 회차 확인 여부, 발견 회차·범위·시즌을 기록합니다. 번호 체계가 다르면 offset을 추측하지 않고 `unverified`로 남깁니다.
- 자동 history backfill은 작품 제목을 부분 문자열로 맞추지 않고, 회차/시즌 표기만 제거한 전체 작품 identity가 같을 때만 이전 회차를 받습니다. 부제가 다른 후속작은 자동으로 섞지 않습니다.
- 직접 자막 파일 형식으로 `.srt`도 허용합니다.

이 메타데이터는 다운로드를 막거나 플레이어 회차를 자동 배치하기 위한 것이 아니라, 잘못 받은 자료를 나중에 안전하게 검토하기 위한 근거입니다.

첫 전환 테스트는 머지 후 **Actions → Monthly subtitle discovery → Run workflow**를 한 번 실행해 2026-09-26 이후 queue를 새로 만든 다음, Pi에서 `anime-monthly-sync.service`를 수동 실행하면 됩니다.
