# anime-db-download

애니메이션 제목 매칭용 DB 파일을 보관하기 위한 저장소입니다.

현재 목적은 `streaming-project`에서 사용하는 4-source 애니메이션 DB를 GitHub Releases에 올려두고, GitHub Actions나 테스트 스크립트에서 내려받아 사용할 수 있게 하는 것입니다.

## 용도

- AniList / Onnada / Anissia / ReAnime 기반 제목 매칭 DB 보관
- `streaming-project` 자막 매칭 테스트용 DB 제공
- 대용량 DB 파일은 저장소에 직접 커밋하지 않고 **GitHub Releases**에 업로드

## 예정 파일

`anime_four_sources_multilingual_prepared.db`

DB 파일은 Release asset으로 관리합니다.
