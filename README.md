# 백업 봇

이 브랜치(`backup-bot`)는 [SYU-Taxi-Delivery/taxi-delivery-sharing](https://github.com/SYU-Taxi-Delivery/taxi-delivery-sharing)을
10분마다 확인해 이 저장소에 백업합니다. 원본과 상관없이 이 저장소에서 돌아가므로 원본이 통째로 덮어써져도 멈추지 않습니다.

## 무엇을 백업하나

- 모든 브랜치·태그와 각 PR의 코드(`pr/번호` 브랜치)
- 이슈와 PR 글·댓글(`[원본 이슈 #N]`, `[원본 PR #N]` 이슈로 복사)

## 덮어써졌을 때

원본 브랜치가 강제 푸시나 통째 교체로 덮어써지면, 바뀌기 직전 상태를 먼저
`archive/<브랜치>/<UTC 시각>` 브랜치로 남긴 뒤 새 상태를 따라갑니다. 원본에서 지워진 브랜치와
이슈는 백업에서 지우지 않습니다.

복구하려면 남겨 둔 브랜치를 원본에 다시 올리면 됩니다.

```bash
git clone https://github.com/jennjeong/taxi-delivery-sharing.git
cd taxi-delivery-sharing
git push https://github.com/SYU-Taxi-Delivery/taxi-delivery-sharing.git archive/main/20261110T0100Z:refs/heads/restore-main
```

그다음 원본 저장소에서 `restore-main` → `main` PR을 만들어 병합합니다.

## 알아 둘 것

- GitHub 예약 실행은 몇 분씩 늦어질 수 있습니다. `Actions` 탭의 **Run workflow**로 바로 실행할 수도 있습니다.
- 공개 저장소에 60일 동안 활동이 없으면 GitHub가 예약 실행을 멈춥니다. 그때는 `Actions` 탭에서 다시 켜 주세요.
- 기본 토큰은 워크플로 파일을 바꾸는 커밋을 올릴 수 없습니다. 그런 커밋이 생기면 실행이 실패로 표시되고,
  `BACKUP_TOKEN` 시크릿(이 저장소의 Contents·Workflows 쓰기 권한 토큰)을 넣으면 해결됩니다. 이때도 덮어써진 이전 상태는 먼저 보관됩니다.
