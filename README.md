# anime-db-download

4곳(AniList, Onnada, Anissia, ReAnime)에서 만든 애니메이션 제목 매칭 DB를 개인용 비공개 Release asset으로 보관하고, 허용된 출처의 신규/변경 항목을 주기적으로 반영합니다.

## 최초 설정

- 현재 기준 DB는 43,310개 source rows를 담은 SQLite 파일입니다.
- `anime.db.gz`를 압축 Release asset으로 올립니다. 파일 크기는 약 8.8 MB입니다.
- GitHub Release를 tag `latest`로 한 번 만들고, 자산 파일 이름을 정확히 `anime.db.gz`로 지정합니다.
- 이 seed asset이 있어야 월별 Actions가 실행됩니다.

## 월별 갱신

매월 1일 10:17 KST GitHub Actions가 `latest` Release의 DB를 내려받아 처리합니다.

- Anissia: 공개 목록 API의 마지막 페이지부터 최근 작품을 확인합니다.
- ReAnime: 연도 내림차순 API에서 새 항목을 확인합니다.
- Onnada: 제공한 Colab 수집기처럼 목록 페이지를 한 장씩 1.2초 간격으로 확인하고, 기존 목록에 없는 ID의 상세 페이지만 가져옵니다.
- AniList: `UPDATED_AT_DESC` 순으로 최근 30일(첫 실행) 또는 저장 체크포인트 이후의 항목만 확인합니다. 요청 간격은 5초로 두고, DB에 없는 새 ID만 추가합니다.

기존 작품은 `(source, source_record_id)`로 중복을 막습니다. 기존 source 간 검토 연결과 결정은 보존하며, 새 작품 및 제목/연도/형식이 바뀐 작품은 `monthly_review` 표에 기록합니다. 새 후보를 자동 확정하지 않습니다. 원본 목록이 비었거나 응답 형식이 예상과 다르면 불완전한 스냅샷을 게시하지 않고 작업을 실패시킵니다. 변경된 데이터가 있을 때만 Release asset을 덮어씁니다.

## 수동 입력 파일 병합

AniList JSONL 또는 업데이트된 출처 export가 이미 있을 때 실행합니다.

```bash
python3 scripts/update_db.py --snapshot anime.db.gz --input AniList=/path/to/anilist_complete.jsonl
python3 scripts/update_db.py --snapshot anime.db.gz --input Onnada=/path/to/onnada_jp_anime_v2.jsonl
```

Pi에 적용할 때는 업데이트된 Release asset을 내려받아 압축을 풀고, `streaming-project`의 importer로 PostgreSQL에 넣습니다. 매칭 테이블은 별도 검토 없이 새 ID를 자동 연결하지 않습니다.