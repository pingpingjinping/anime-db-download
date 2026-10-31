# 웹에서 요청하는 에루샤 자막

`streaming-project`의 자막 선택창에서 **자막 받아오기**를 누르면 공유된
`subtitle-data/.erulabo-web/jobs/`에 요청이 저장됩니다. 파이의 이 서비스가
주간 수집과 같은 Chromium 프로필·다운로드 버튼 클릭 코드를 사용해 처리합니다.
페이지를 닫아도 요청과 파일은 남습니다. 완료된 파일은 웹이 요청 회차를 검증한 뒤
기존 자막 캐시에 저장하고 적용합니다. 이미 주간 수집으로 저장한 자막을 찾으면
새 다운로드 없이 바로 적용할 수 있습니다.

## 파이에 적용

두 저장소의 관련 PR을 반영하고 새 웹 이미지를 배포한 뒤 한 번 실행합니다.
기존 주간 수집용 Python 가상환경, Chromium, Xvfb를 그대로 사용합니다.

```bash
cd ~/anime-db-download
bash scripts/install-erulabo-web-worker.sh
```

기본 경로는 `/home/pi/anime-db-download`,
`/home/pi/streaming-project/subtitle-data`이며 서비스는 `pi` 사용자로 실행됩니다.
기존 `/home/pi/.config/streaming-project/anime-db.env`의 `STREAMING_REPO`,
`ERULABO_BROWSER_PROFILE`, `ERULABO_CDP_PORT` 설정을 따릅니다.
웹 컨테이너가 마운트한 자막 폴더와 서비스의 대상 폴더가 같아야 합니다.
다른 사용자·경로를 쓰는 설치는 서비스의 User, Group, WorkingDirectory,
EnvironmentFile, ExecStart를 실제 설치에 맞추세요.

```bash
systemctl status anime-erulabo-web.service --no-pager -l
journalctl -u anime-erulabo-web.service -n 50 --no-pager
```

서비스가 준비되지 않으면 웹은 새 요청 버튼을 비활성화합니다.
이미 받은 자막은 서비스가 꺼져 있어도 적용할 수 있습니다.

## 작업과 장애 처리

- 같은 게시물은 사용자·탭·회차가 달라도 하나의 다운로드 요청을 공유합니다.
  묶음 ZIP의 다른 회차는 받은 원본에서 다시 선택합니다.
- 실패 시 브라우저를 한 번 재시작해 재시도합니다. 최종 실패는 웹에서
  **다시 시도**를 눌러 새 요청으로 보낼 수 있습니다.
- 서비스가 재시작되면 끝나지 않은 요청을 다시 처리합니다.
- 주간 수집 CLI와 웹 서비스는 `${ERULABO_BROWSER_PROFILE}.lock`을 공유합니다.
  주간 정리 과정도 이 잠금을 얻은 뒤 브라우저를 종료하므로 진행 중인 요청을
  끊지 않습니다. 주간 작업 중 웹 요청은 순서를 기다립니다.
- 요청은 숫자 게시물 주소를 가진 `https://erulabo.com/`으로 제한됩니다.
  이 기능은 캡차 성공을 보장하지 않으며 수동 확인이 필요하면 실패로 표시합니다.
- 원본과 요청은 `.erulabo-web/jobs/` 아래 남습니다. 서비스 상태 JSON과
  브라우저 프로필은 외부에 공개하지 않습니다. 별도 HTTP/CDP 포트를 열지 않습니다.

적용 후 실제 파이에서 에루샤 항목의 버튼, 진행 상태, 페이지 재진입,
완료 후 적용, 두 번째 즉시 적용을 확인하세요.
