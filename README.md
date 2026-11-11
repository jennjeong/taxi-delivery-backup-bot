# 백업 봇

원본 저장소 두 개를 10분마다 확인해 각각의 백업 저장소에 복제합니다.

| 구분 | 원본 | 백업 |
| --- | --- | --- |
| 백엔드 | [SYU-Taxi-Delivery/taxi-delivery-sharing](https://github.com/SYU-Taxi-Delivery/taxi-delivery-sharing) | [jennjeong/taxi-delivery-sharing](https://github.com/jennjeong/taxi-delivery-sharing) |
| 프론트 | [SYU-Taxi-Delivery/taxi-delivery-sharing-fe](https://github.com/SYU-Taxi-Delivery/taxi-delivery-sharing-fe) | [jennjeong/taxi-delivery-sharing-fe](https://github.com/jennjeong/taxi-delivery-sharing-fe) |

프론트는 코드(브랜치·태그·PR 코드)만 복제하고 이슈·PR 글은 백업하지 않습니다.
저장소마다 따로 실행되므로 한쪽이 실패해도 다른 쪽 백업은 계속되고, 텔레그램 알림 앞에 `[백엔드]`, `[프론트]`가 붙습니다.
`BACKUP_TOKEN`은 두 백업 저장소 모두에 접근할 수 있어야 합니다.
봇은 원본과 백업 어느 쪽에도 없는 이 저장소에서 돌아가므로, 원본이 통째로 덮어써져도 멈추지 않고
백업 저장소에는 원본의 브랜치만 남습니다.

## 무엇을 백업하나

- 원본 Git이 추적하는 모든 파일과 커밋 이력: 모든 브랜치·태그, 각 PR의 코드(`pr/번호` 브랜치)
- 이슈와 PR 글·댓글(백업 저장소에 `[원본 이슈 #N]`, `[원본 PR #N]` 이슈로 복사)

## 백업 저장소 구조

- 기본 브랜치 `source/main`: 원본 `main`을 그대로 따라갑니다. `git clone`하면 원본과 같은 파일이 같은 경로에 내려옵니다.
- `main`: 처음 백업한 시점 그대로 고정(PR로만 바꿀 수 있는 보호 규칙). 봇이 덮어쓰지 않습니다.
- 그 밖의 브랜치·태그: 원본과 같은 이름
- `archive/...`: 지워지거나 덮어써진 이전 버전(아래)
- `backup-bot`: 봇을 이 저장소로 옮기기 전의 봇 설정(보존용, 실행되지 않음)

## 지워지거나 덮어써졌을 때

백업을 최신 상태로 바꾸기 **전에**, 지금 백업에 있는 버전을 복구용 브랜치로 먼저 남기고
백업 저장소에서 그 브랜치가 실제로 그 커밋을 가리키는지 확인합니다.

| 원본에서 일어난 일 | 남기는 복구용 브랜치 |
| --- | --- |
| 강제 푸시·이력 재작성 | `archive/<브랜치>/<UTC 시각>` |
| 파일을 지운 푸시 | `archive/<브랜치>/<UTC 시각>` |
| 브랜치 삭제 | `archive/<브랜치>/<UTC 시각>-deleted` (백업의 원래 브랜치도 그대로 둠) |
| 태그 덮어쓰기·삭제 | 태그 `archive/<태그>/<UTC 시각>[-deleted]` |

복구용 브랜치를 남기거나 확인하는 데 실패하면 그 브랜치의 백업은 **바꾸지 않고 그대로 둔 채**
실행을 실패로 표시하고 텔레그램으로 알립니다. 다음 실행에서 다시 시도합니다.
백업에서는 아무것도 지우지 않습니다.

복구하려면 남겨 둔 브랜치를 원본에 다시 올리면 됩니다.

```bash
git clone https://github.com/jennjeong/taxi-delivery-sharing.git
cd taxi-delivery-sharing
git push https://github.com/SYU-Taxi-Delivery/taxi-delivery-sharing.git origin/archive/main/20261110T0100Z:refs/heads/restore-main
```

그다음 원본 저장소에서 `restore-main` → `main` PR을 만들어 병합합니다. 파일 하나만 필요하면
텔레그램 알림의 파일 링크에서 내용을 바로 볼 수 있습니다.

## 알아 둘 것

- GitHub 예약 실행은 몇 분씩 늦어질 수 있습니다. `Actions` 탭의 **Run workflow**로 바로 실행할 수도 있습니다.
- 공개 저장소에 60일 동안 활동이 없으면 GitHub가 예약 실행을 멈춥니다. 그때는 `Actions` 탭에서 다시 켜 주세요.
- 이 저장소의 시크릿 세 개가 필요합니다.
  - `BACKUP_TOKEN`: 백업 저장소 하나에만 접근하는 fine-grained 토큰. 권한은 Contents·Workflows·Issues를 Read and write로.
    만료되면 실행이 실패하고 텔레그램으로 알려 줍니다. 새로 만든 뒤 **Run workflow**에서 "BACKUP_TOKEN 확인"으로 점검합니다.
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: 아래 텔레그램 알림

## 텔레그램 알림

원본에 푸시가 있을 때 푸시 한 건마다 메시지 하나를 보냅니다. 바뀐 게 없으면 실행할 때마다 `✅ 원본 변경 없음`을 보냅니다.

- `🔔 GitHub 원본 변경 감지`: 브랜치, 푸시한 사람, 시각(KST), 새 커밋(최대 3개)
- `⚠️ 변경 유실 주의`: 다른 사람의 커밋이 강제 푸시·브랜치 삭제로 빠졌거나, 일반 푸시가 다른 사람의 변경을
  되돌린 것이 비교로 확인됐을 때. 커밋이 이력에서만 빠졌는지, 파일 내용까지 사라졌는지 구분해 적습니다.
  리베이스·스쿼시로 해시만 바뀌고 같은 변경이 남아 있으면 유실로 보지 않습니다.
- `⚠️ 덮어쓰기 감지 · 유실 여부 확인 필요`: 덮어쓰기는 확인됐지만 이전 커밋을 읽을 수 없어 비교하지 못했을 때
- `💾 최신 파일 백업`: 완료(백업의 커밋·파일이 원본과 같음) / 보류(이전 버전 보관 실패로 기존 백업 유지) / 실패. 완료가 아니면 아직 백업에 없는 파일을 함께 적습니다.
- `🛟 이전 버전 복구`: 덮어써지거나 지워진 버전을 백업에서 다시 받아 열어 본 경우에만 "복구 가능"과 그 버전·파일 링크
- `⚠️ 이전 버전 복구 미확인`: 원본에서 사라진 버전을 백업에서도 찾지 못했을 때
- `✅ 원본 변경 없음`: 지난 확인 뒤로 바뀐 게 없고 백업이 원본과 같을 때 (확인 시각 포함)
- `⚠️ 백업 불일치`: 실행 뒤 원본의 모든 브랜치·태그와 기본 브랜치를 백업과 다시 비교해, 다른 곳이 남아 있을 때 브랜치와 파일을 알려 줍니다.

설정:

1. 텔레그램에서 `@BotFather`에게 `/newbot`을 보내 봇을 만들고 토큰을 받습니다.
2. 만든 봇과 대화를 열어 아무 메시지나 보낸 뒤, 브라우저에서
   `https://api.telegram.org/bot<토큰>/getUpdates`를 열어 `"chat":{"id": …}`의 숫자를 확인합니다.
3. 이 저장소의 **Settings → Secrets and variables → Actions → New repository secret**에서 두 개를 추가합니다.
   - `TELEGRAM_BOT_TOKEN`: 1번의 토큰
   - `TELEGRAM_CHAT_ID`: 2번의 숫자
4. **Actions → 원본 저장소 백업 → Run workflow**에서
   - "텔레그램 테스트 알림 보내기": 바뀐 게 없어도 테스트 알림 한 통
   - "알림 예시 보내기": 테스트용 임시 저장소에서 여러 상황을 재현해 만든 예시 알림

## 검사

`python3 tests/scenarios.py`는 임시 저장소 두 개(원본·백업 역할)로 일반 푸시, 강제 푸시, 리베이스,
덮어쓴 변경, 파일·브랜치 삭제, 보관 실패를 재현하고, 백업만으로 지워진 파일을 되살릴 수 있는지 확인합니다.
