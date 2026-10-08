# anime-db-download

1분 시청 캐시 정리: 새 다운로드 성공 시 `.cache-cleanup-pending`에 해당 파일 경로만 기록합니다. 주간 마지막 단계에서 실행 중인 최신 anime 이미지의 정리 도구를 낮은 우선순위로 호출합니다. 같은 작품·회차에서 디코딩된 내용과 형식까지 동일한 캐시만 삭제하며, 원본 자막·ZIP·DB는 수정하지 않습니다. 읽기 실패나 사이트 중지 시 대기 항목을 보존해 다음 실행에서 이어갑니다. 기존 캐시는 같은 로컬 자막을 재생할 때 정리합니다.

주간 신규 자막은 애니시아 `제목/회차/제작자`, 애니올 `제목`으로 저장하며, 같은 작품명 폴더가 있으면 이어서 사용합니다. 번호를 새 폴더명에 붙이지 않고 파일별 작품번호·게시글번호·원본 SHA-256을 `subtitle-data/.weekly-subtitle-sources.json`에 기록합니다. 동일 제목의 다른 출처 항목은 파일별 번호로 구분하며, 같은 파일명의 다른 내용은 덮어쓰지 않고 별도 버전으로 보존합니다. 기존 번호 폴더만 있다면 그 폴더를 재사용하고, 기존 작품명 폴더·검수 ZIP·등록된 경로는 이동하거나 이름을 바꾸지 않습니다. 애니시아 과거 회차와 Erulabo 다운로드에도 같은 저장 규칙을 적용합니다. 애니올 다운로드가 끝나면 공개된 매칭 Release를 가져오고, streaming-project의 `scripts/match-weekly-aniall.py`로 새로 성공한 게시글만 현재 DB와 대조합니다. 확실한 제목·별칭 및 시즌·연도·형식 일치만 자동 연결하고, 애매한 후보는 검토 대상으로 기록합니다. 기존 수동 판정과 확정 회차는 수정하지 않습니다. 두 저장소를 모두 업데이트한 뒤 주간 서비스를 실행해야 합니다.

AniList, ReAnime, Onnada, Anissia 네 출처의 애니메이션 DB를 비공개 GitHub Release에 보관하고, 신규 항목만 월별로 추가합니다. AniAll은 네 출처 작품 목록의 별도 출처 행으로 추가하지 않고, 별도 매칭 작업에서 참조합니다. `db-v1`은 작품 DB `anime_four_sources_multilingual_prepared.zip`과 검수 자막 registry `anime_reviewed_subtitle_registry_v1.zip` 두 자산으로 분리합니다. 자막 파일 본체는 DB에 넣지 않고 Pi의 `subtitle-data`에 보관합니다. streaming-project Release updater는 두 자산의 SHA-256과 import 상태를 독립적으로 관리하며, 필요하면 `--catalog-only` 또는 `--registry-only`로 각각만 반영할 수 있습니다.

## 기준 DB 월별 갱신

매월 1일 10:17 KST GitHub Actions가 Release `db-v1`의 `anime_four_sources_multilingual_prepared.zip`을 내려받아 네 출처만 갱신합니다.

- AniList: `ID_DESC` 순으로 최신 AniList ID부터 확인하고, 현재 DB에 저장된 가장 큰 AniList ID를 만나면 즉시 중단합니다. 따라서 AniList의 `updatedAt` 대량 변경과 무관하게 신규 ID만 짧게 확인하며, API 요청 사이에는 5초 간격을 둡니다.
- Anissia: 공개 목록 API에서 최근 페이지를 확인합니다.
- ReAnime: 연도 내림차순 API에서 신규 항목을 확인합니다.
- Onnada: 목록 페이지와 신규 상세 페이지를 1.2초 간격으로 확인합니다.



초기 DB ZIP은 Release `db-v1`에 올라가 있습니다. 기준 DB를 수동 갱신하려면 **Actions → Monthly anime DB refresh → Run workflow**를 실행합니다.

기존 출처 간 검토 연결과 결정은 보존합니다. 새 작품과 제목·연도·형식이 바뀐 항목은 `monthly_review`에 기록하고, 새 매칭을 자동 확정하지 않습니다. DB가 바뀌면 같은 `db-v1` Release의 ZIP 자산을 갱신합니다.

## Anikoto 원본 목록 수집

Anikoto는 기존 네 출처 기준 DB와 별도로 **목록 메타데이터만** 보관합니다. **2026-10-04 20:56 KST에 수집 완료한 299페이지·8,949작품**이 `data/anikoto/anikoto_listing_20261004.zip`에 초기 DB로 들어 있습니다. 이 결과를 바로 사용할 수 있으므로 처음부터 다시 크롤링할 필요가 없습니다. `seed.json`에 ZIP·SQLite SHA-256, 수집 완료 시각, 수집 코드 커밋과 페이지·작품 수를 기록합니다.

**Actions → Anikoto catalog crawl → Run workflow**의 기본 `mode=use_existing`은 기존 DB를 복원하고 아래 파일을 내보내기만 하며 Anikoto에 접속하지 않습니다. `resume=true`이면 최신 artifact의 DB를 우선 사용하고, artifact가 없거나 `resume=false`이면 저장소의 초기 DB를 사용합니다. 자동 예약은 없습니다. artifact 보관 기간이 지난 뒤에는 초기 DB로 돌아가므로, 갱신 결과를 장기 보관하려면 artifact를 별도로 내려받아 보관합니다.

- `anikoto-catalog.sqlite`: 작품 원문 정보, 페이지별 수집 근거와 재개 상태
- `anikoto-catalog.jsonl`: 해당 실행에서 관찰한 작품 목록
- `anikoto-crawl-report.json`: 완료/부분 수집 여부, 페이지·작품 수, 무결성 검사

결과는 `anikoto-catalog` Actions artifact로 90일 보관합니다. 제목순 목록을 요청 사이 1.2초 간격으로 읽고, 작품 ID·사이트 ID·표시 제목·원제 표기·형식·장르·포스터 URL·자막/더빙/전체 회차 수·평점을 기록합니다. 상세 페이지·영상·자막 파일은 받지 않습니다. 목록에 없는 연도·방영 상태는 추정하지 않고 비워 두며, 작품 연결은 `unmatched`로 남깁니다. 기존 `db-v1` Release, 검수 작품 연결과 확정 자막 회차에는 반영하지 않습니다.

로컬에서 초기 DB를 복원하거나 이미 있는 DB를 내보내기 (사이트 요청 없음):

```bash
python3 -m pip install requests beautifulsoup4
python3 scripts/prepare_anikoto_catalog.py --db state/anikoto-catalog.sqlite --export-dir output/anikoto
```

사이트 목록을 갱신할 때만 Actions의 `mode=refresh`를 선택합니다. `max_pages=0`은 전체 목록이고 숫자를 지정하면 해당 실행의 페이지 수를 제한합니다. `resume=true`이면 중단된 수집을 이어가며, 완료된 DB에서는 새 전체 스캔을 시작합니다. 기본 모드로 중단된 DB를 내보내면 보고서의 `complete=false`를 그대로 유지합니다. 로컬에서 갱신하려면:

```bash
python3 scripts/crawl_anikoto.py --db state/anikoto-catalog.sqlite --export-dir output/anikoto
```

접속 도메인이 바뀌면 `--base-url https://새도메인`을 지정합니다. 목록이 중복되거나 페이지 수가 스캔 중 바뀌면 완료로 처리하지 않고 기존 수집분을 남깁니다. 이때 원인 확인 후 `--fresh`로 새 스캔을 시작할 수 있습니다. 새 스캔에서 관찰되지 않은 기존 작품을 삭제하지 않으며 JSONL에는 해당 실행의 관찰분만 내보냅니다.

## Anikoto 작품 연결

매월 1일 10:17 KST에 시작하는 **Monthly anime DB refresh**가 성공하면 **Anikoto reviewed catalog matching**이 이어서 전체 목록을 갱신하고 현재 기준 DB에 매칭합니다. 실패·부분 수집은 새 Anikoto 배포본으로 반영하지 않습니다. DB 갱신과 같은 동시 실행 그룹을 사용하고 업로드 직전 기준 Release의 SHA-256을 다시 확인해 다른 최신 DB를 덮어쓰지 않습니다.

매칭 코드·검토 교정 데이터·이 workflow가 `main`에 반영될 때도 저장된 최신 완료 목록으로 매칭하고 배포합니다. 이 최초/코드 변경 반영은 사이트를 다시 크롤링하지 않습니다. **Actions → Anikoto reviewed catalog matching → Run workflow**에서는 기본 `mode=use_existing`, `publish=true`로 저장된 목록을 즉시 반영할 수 있으며, `mode=refresh`는 전체 목록 갱신, `publish=false`는 검증과 artifact 출력만 실행합니다.

확정 연결과 교정을 포함한 압축 DB는 기존 **`db-v1/anime_four_sources_multilingual_prepared.zip`**에 반영됩니다. ZIP에는 기존 형식과 같은 단일 SQLite DB만 들어갑니다. 전체 수집 원문은 **`anikoto-catalog/anikoto_listing_latest.zip`** Release에 별도 보관하고 다음 갱신의 기준으로 사용합니다. 실패한 수집의 재개 상태·전체 검토 결과·출처 충돌 목록은 `anikoto-matched-database` artifact로 90일 보관합니다. 미연결 후보는 배포 DB에 넣지 않습니다.

전체 제목·형식·총화수가 일치하고 양쪽 작품 후보가 각각 하나인 경우만 자동 연결합니다. 제목의 시즌·파트·별표·숫자는 보존하며 동명 리메이크, 회차 차이, 연도 없는 기준 작품, 메타데이터 부족은 보류합니다. 기준 DB의 다른 출처 ID도 제목·연도 및 있는 형식·회차 정보가 뒷받침될 때만 함께 연결합니다. 2026-10-04 초기 결과는 **8,949작품 중 4,522개 자동 연결, 2,994개 후보 검토, 1,433개 정확한 제목 후보 없음**입니다. 발견한 기존 출처 충돌 575건은 확인 목록이며 모두 오류라는 뜻은 아닙니다.

`data/anikoto/catalog-corrections.json`에는 공식 근거를 확인한 수정만 기록합니다. 현재는 히다마리 3기의 Anissia:26 / Onnada:2050 연결과 본편 총화수를 교정합니다. 수정은 제목·연도와 기존 행을 검증한 뒤 적용하며 원래 입력 DB는 읽기 전용으로 유지합니다.

```bash
python3 scripts/match_anikoto.py --baseline /path/to/reviewed-baseline.db \
  --catalog state/anikoto-catalog.sqlite --output output/matching \
  --corrections data/anikoto/catalog-corrections.json \
  --corrected-db output/anime_four_sources_multilingual_prepared.db
```

새 DB는 작품 카탈로그와 Anikoto 연결만 다루며 `anikoto_catalog`, `anikoto_work_links`, `anikoto_match_metadata`를 추가합니다. Anikoto는 기존 네 출처의 `entries`에 섞지 않습니다. **runtime 형식 2**는 연결된 작품만 저장하며 기본 제목·매칭된 연도·장르·형식·포스터·회차 수와 정확한 출처 ID만 유지합니다. 중복 URL·점수·수집 추적 필드·긴 자동 매칭 근거는 빼고 SQLite `VACUUM` 후 압축합니다. 검수 자막 파일·확정 회차 registry는 별도 Release 자산에 있으므로 Anikoto 월간 배포가 registry를 다시 압축하거나 덮어쓰지 않습니다.

이 DB를 소비하는 앱은 `anikoto_work_links.payload_json`의 `source_refs`로 정확한 출처 ID를 조회해야 하며, 기존 importer가 이 표를 읽지 않으면 플레이어 동작은 아직 바뀌지 않습니다. 기존 DB의 `reviewed`·`rejected` 연결은 후보 선택과 다음 DB 생성에서 유지합니다. 자동 제안은 수동으로 확정한 작품을 중복으로 차지할 수 없습니다.

`anikoto-matches.json`은 확정된 연결과 근거, `anikoto-match-review.jsonl`은 미연결 후보와 사유, `existing-source-conflicts.json`은 기존 출처 충돌, `anikoto-match-report.json`은 입력 SHA-256과 결과 수입니다. 수동 검수는 `--decisions` JSON으로 전달할 수 있습니다. 예: `{"slug":{"status":"accepted","target":"AniList:1852","reason":"원제 및 시즌 직접 확인"}}`. 거절은 `status=rejected`와 이유를 기록합니다. 파일별 검수와 확정 회차는 이 명령에서 재판정하지 않습니다.

## AniAll 별도 매칭

AniAll은 기준 DB의 출처 행으로 저장하지 않습니다. 매월 1일 10:17 KST에 시작하는 **Monthly anime DB refresh**가 성공적으로 끝나면 **Aniall Match Audit** workflow가 자동으로 이어서 실행됩니다. 따라서 DB 갱신이 늦어져도 audit가 먼저 시작하지 않으며, DB 갱신이 실패하면 자동 audit도 실행하지 않습니다. audit는 최신 4출처 DB를 받아 AniAll 자막자료실 전체 목록을 천천히 다시 확인하고, 제목·연도·시즌·형식 후보를 대조합니다. 시즌 종료 뒤 예전 작품의 자막 글이 추가될 수 있어, 최근 글만 훑지 않고 전체 목록을 다시 대조합니다.

결과는 기준 DB ZIP과 분리된 GitHub Actions artifact로 보관하며, 후보는 검토용입니다. workflow는 **Actions → Aniall Match Audit → Run workflow**에서 수동 실행할 수도 있습니다. 기본적으로 1.2초 간격으로 요청합니다.

## Pi에 기준 DB 반영

Pi의 기본 updater는 `db-v1`의 작품 DB와 검수 registry를 각각 확인해 바뀐 자산만 내려받고 import합니다. 작품 DB import 뒤에는 기존 자막 행을 변경하지 않은 채 catalog-derived ReAnime lookup만 보충합니다. 검수 registry import는 `is_manual=true`인 수동 수정 행을 덮어쓰지 않으며 기존 확정 회차 행도 삭제하지 않습니다.

```bash
cd ~/streaming-project
git pull --ff-only
docker compose up -d db
python3 scripts/import-anime-db.py /path/to/anime_four_sources_multilingual_prepared.db
docker compose up -d --build anime caddy
docker compose ps
```

Pi의 자동 반영은 매월 3일 02:00 KST에 실행됩니다. GitHub의 DB Release 갱신이 성공하면 AniAll audit가 바로 이어서 실행되므로, Pi는 준비된 최신 자산을 3일 새벽에 받아옵니다.

Pi importer는 고정 행 수가 아닌 네 출처가 모두 있는지 확인하므로 월별 DB 증가분을 받아들입니다. 기존 스트리밍 링크와 기준 DB의 검토 연결은 importer가 다시 계산하거나 덮어쓰지 않습니다. 검수 registry는 별도 digest로 관리하므로 월간 작품 DB 갱신만 있을 때 대용량 registry를 다시 다운로드하지 않습니다. AniAll 매칭 후보는 기준 DB 파일에 병합하지 않습니다.

수동으로 자산 하나만 확인/반영할 때는 `python3 scripts/update-anime-db-release.py --catalog-only` 또는 `--registry-only`를 사용합니다. 기존 합본 `db-v1`도 registry 자산이 아직 없으면 자동으로 감지해 호환 import하며, 분리 마이그레이션 전후 어느 쪽에서도 월간 timer가 동작합니다.

## 주간 자막 발견 + Pi 다운로드

자막은 **GitHub에서 실제 파일을 받지 않습니다.** 역할을 둘로 나눕니다.

- GitHub Actions: 신규 자막 후보만 발견해서 queue Release 생성
- Pi: queue에 들어 있는 URL만 실제로 열어서 자막 파일 다운로드

초기 기준 시각은 **2026-09-25 23:59:59 KST**입니다. 이 시각 이전 자료는 이미 수동으로 확보했다고 가정합니다. 첫 discovery는 2026-09-26 이후 항목을 다시 queue에 넣으므로, 첫 PC 수집본과 일부 중복될 수 있지만 누락 방지를 우선합니다.

### GitHub discovery

**Weekly subtitle discovery** workflow는 매주 토요일 04:00 KST에 실행됩니다. Pi의 실제 자막 다운로드는 다음 날인 일요일 04:00 KST에 시작하므로 약 24시간의 여유를 둡니다.

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

Pi는 역할을 두 timer로 분리합니다.

- 매월 3일 02:00 KST: 최신 `db-v1` Release를 받아 애니 DB를 반영하고 새 AniAll 매칭을 import
- 매주 일요일 04:00 KST: 아직 처리하지 않은 `subs-queue-*` Release를 순서대로 받고 실제 자막 페이지/파일을 다운로드한 뒤 Erulabo pending도 이어서 처리

주간 자막 다운로드에서는 queue에 들어 있는 소수의 실제 페이지에만 접속하고, 첨부 ZIP/ASS/SMI 등을 `~/streaming-project/subtitle-data`에 저장합니다. 실패 항목은 Pi 로컬 retry state에 남겨 다음 주 실행에서 먼저 재시도하며, 같은 URL/항목이 3개월 동안 계속 실패하면 4개월차부터 active retry에서 제거합니다.

즉 Anissia 수천 작품 API 확인은 GitHub가 하고, Pi는 **신규 후보 몇 건의 실제 페이지/파일 다운로드만** 담당합니다. subtitle 다운로드 프로세스는 `nice -n 15` + `ionice -c3`로 낮은 우선순위로 실행됩니다.

기존 DB updater에서 쓰는 `/home/pi/.config/streaming-project/anime-db.env`의 `ANIME_DB_GITHUB_TOKEN`을 그대로 사용합니다.

Pi 준비:

```bash
cd ~
git clone git@github.com:pingpingjinping/anime-db-download.git
cd ~/anime-db-download

sudo apt update
sudo apt install -y python3-venv chromium xvfb x11vnc novnc websockify xdotool

python3 -m venv ~/.local/share/anime-db-download/venv
~/.local/share/anime-db-download/venv/bin/pip install -r ~/anime-db-download/requirements-pi-subtitles.txt

sudo cp ~/anime-db-download/ops/systemd/anime-monthly-sync.service /etc/systemd/system/
sudo cp ~/anime-db-download/ops/systemd/anime-monthly-sync.timer /etc/systemd/system/
sudo cp ~/anime-db-download/ops/systemd/anime-weekly-subtitles.service /etc/systemd/system/
sudo cp ~/anime-db-download/ops/systemd/anime-weekly-subtitles.timer /etc/systemd/system/

# 기존 DB 전용 timer와 중복 실행되지 않게 비활성화
sudo systemctl disable --now streaming-anime-db-update.timer 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable --now anime-monthly-sync.timer anime-weekly-subtitles.timer
systemctl list-timers anime-monthly-sync.timer anime-weekly-subtitles.timer
```

수동 테스트:

```bash
sudo systemctl start anime-monthly-sync.service
journalctl -u anime-monthly-sync.service -n 200 --no-pager

sudo systemctl start anime-weekly-subtitles.service
journalctl -u anime-weekly-subtitles.service -n 200 --no-pager
```

Pi 다운로드 state는 `~/.local/state/anime-db-download/subtitle-queue-state.json`, 실패 이력은 `~/.local/state/anime-db-download/subtitle-download-failed.jsonl`에 저장됩니다. Erulabo URL은 일반 retry와 분리해 `erulabo_pending`에 보관합니다. 주간 subtitle sync는 일반 queue 다운로드를 모두 끝낸 뒤 `scripts/pi_download_erulabo.py`를 자동 실행합니다. pending이 있으면 첫 Erulabo 페이지를 GUI Chromium에 열고 다운로드 버튼을 화면 중앙에 배치한 뒤 X11 커서를 버튼 위로 이동합니다. pending이 없으면 helper는 바로 종료합니다. helper는 버튼을 클릭하거나 success state를 변경하지 않습니다.

helper는 필요하면 Xvfb, Chromium, x11vnc, noVNC를 localhost 전용으로 기동하며 Chromium 프로필은 `~/.local/share/anime-db-download/erulabo-browser-profile`에 유지합니다. noVNC를 사용할 때는 Pi의 6080 포트를 외부에 직접 열지 말고 SSH 터널로 접속합니다. 수동 재실행이 필요하면 다음 명령을 사용할 수 있습니다.

```bash
~/.local/share/anime-db-download/venv/bin/python ~/anime-db-download/scripts/pi_download_erulabo.py
```

실패 기간은 실행 횟수가 아니라 **달력 월 기준**으로 계산합니다. 예를 들어 9월에 처음 실패한 항목은 9·10·11월까지 재시도하고 12월(4개월차)부터 retry에서 제거합니다. 제거된 ID는 재처리 방지를 위해 state의 `retired`에 남고, 상세 이력은 `~/.local/state/anime-db-download/subtitle-download-retired.jsonl`에 기록됩니다. 같은 달에 수동 실행을 여러 번 해도 실패 개월 수는 늘어나지 않습니다. Pi가 한 달 이상 꺼져 있어도 미처리 queue Release를 오래된 순서부터 모두 소비합니다.

다운로드 state에는 진단 메타데이터도 함께 기록합니다.

- `waiting_url.reason`: 회차가 아직 없거나 0인 빈 URL은 `future_or_unreleased`, 회차는 있는데 URL만 비어 있으면 `no_url_yet`로 분리합니다. 이 항목은 retry/retire 개월 수에 포함하지 않습니다.
- `retry.reason`: DNS, SSL, timeout, HTTP 상태, parser, broken link 등을 분류해 마지막 오류와 함께 저장합니다. 실패 JSONL에도 같은 reason이 기록됩니다.
- `success.verification` / `success.coverage`: 다운로드 파일명과 ZIP 내부 자막 파일명을 보수적으로 읽어 요청 회차 확인 여부, 발견 회차·범위·시즌을 기록합니다. 번호 체계가 다르면 offset을 추측하지 않고 `unverified`로 남깁니다.
- 자동 history backfill은 작품 제목을 부분 문자열로 맞추지 않고, 회차/시즌 표기만 제거한 전체 작품 identity가 같을 때만 이전 회차를 받습니다. 부제가 다른 후속작은 자동으로 섞지 않습니다.
- 직접 자막 파일 형식으로 `.srt`도 허용합니다.

이 메타데이터는 다운로드를 막거나 플레이어 회차를 자동 배치하기 위한 것이 아니라, 잘못 받은 자료를 나중에 안전하게 검토하기 위한 근거입니다.

첫 전환 테스트는 머지 후 **Actions → Weekly subtitle discovery → Run workflow**를 한 번 실행해 queue를 만든 다음, Pi에서 `anime-weekly-subtitles.service`를 수동 실행하면 됩니다.

## 신규 작품 수집·매칭 규칙

월간 갱신은 애니시아 API 0페이지부터 전체 목록을 확인하고, 기존 작품의 원제·방영일 수정도 반영합니다. 시즌·파트·형식을 제목과 별도로 판정하고 검증된 외부 ID만 별칭으로 사용합니다. 확정된 충돌 후보와 정보 부족 후보를 구분하며, 기존 검토 연결과 자막 검수 결과는 보존합니다.

규칙과 50개 표본 재생 결과, 읽기 전용 검증 명령은 [신규 작품 매칭 문서](docs/new-source-matching.md)를 참고하세요.
