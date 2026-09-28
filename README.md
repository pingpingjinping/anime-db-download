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

## 월별 자막 아카이브

GitHub Actions의 **Monthly subtitle archive** workflow가 매월 1일 14:17 KST에 실행됩니다. 실제 자막 다운로드는 GitHub runner가 담당하고 Pi는 완성된 Release만 내려받습니다.

초기 기준 시각은 **2026-09-25 23:59:59 KST**입니다. 이 시각 이전 자료는 이미 수동으로 확보했다고 가정하며, 첫 실행부터 2026-09-26 이후 자료만 자동 수집합니다. 초기 PC 수집본과 일부 중복될 수 있지만 기준일 이후 누락을 피하는 쪽을 우선합니다.

- AniAll: 자막자료실의 새 게시글을 확인하고 첨부 자막을 저장합니다.
- Anissia: 전체 작품의 자막 API를 확인해 새 자막 링크를 내려받습니다. 오래된 작품에 새 자막이 붙는 경우도 확인하기 위해 작품 목록 전체를 순회합니다.
- 성공한 항목만 `state/subtitle-collector.json`에 완료 처리합니다.
- 다운로드 실패나 캡차/Cloudflare/빈 링크는 retry 상태와 `failed.jsonl`에 남겨 다음 실행에서 다시 시도합니다.
- 특정 Anissia 작품의 자막 API 조회 자체가 실패하면 그 작품은 다음 실행에서 최초 기준일 이후를 다시 확인합니다.
- 월별 결과는 `subs-YYYY-MM-DD-<run id>-<attempt>` 형식의 **immutable private Release**로 남깁니다.

Release에는 다음 파일이 들어갑니다.

```text
subtitle-delta.tar.gz
manifest.json
failed.jsonl
subtitle-state.json
```

압축 파일 내부는 아래처럼 Pi의 `subtitle-data`에 바로 합칠 수 있는 구조입니다.

```text
aniall_subtitles/
  작품명/
    subtitle.zip

anissia_subtitles/
  AnimeNo__작품명/
    12화/
      제작자/
        subtitle.zip
```

첫 수집은 머지 후 **Actions → Monthly subtitle archive → Run workflow**에서 `main` 브랜치로 수동 실행하면 됩니다.

### Pi 자동 반영

Pi는 자막 사이트를 직접 크롤링하지 않습니다. 매월 2일 10:45 KST에 최신 DB를 먼저 적용한 뒤, 아직 적용하지 않은 `subs-*` Release를 오래된 순서부터 모두 내려받아 `~/streaming-project/subtitle-data`에 합칩니다. Pi가 한 달 이상 꺼져 있어도 빠진 Release를 순서대로 적용합니다.

기존 DB updater에서 사용하는 `/home/pi/.config/streaming-project/anime-db.env`의 `ANIME_DB_GITHUB_TOKEN`을 그대로 사용합니다.

```bash
cd ~
git clone git@github.com:pingpingjinping/anime-db-download.git

sudo cp ~/anime-db-download/ops/systemd/anime-monthly-sync.service /etc/systemd/system/
sudo cp ~/anime-db-download/ops/systemd/anime-monthly-sync.timer /etc/systemd/system/

# 기존 DB 전용 timer는 통합 timer와 중복되므로 비활성화
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

Pi 적용 기록은 `~/.local/state/anime-db-download/subtitle-releases.json`에 저장됩니다. 실패 목록과 각 Release manifest는 각각 `~/streaming-project/subtitle-data/failed/`, `~/streaming-project/subtitle-data/manifests/`에 보존됩니다.
