# 동작 원리 — 이 프로젝트는 어떻게 구현되어 있나

이 문서는 코드를 처음부터 끝까지 읽지 않고도 "무슨 일이 어떤 순서로 일어나는지"를
따라갈 수 있게 쓴 로직 설명서입니다. 함수와 상수는 이름으로 가리킵니다(줄 번호는
금방 어긋나므로 `grep -n "def 이름"`으로 찾으시면 됩니다). 설계 결정의 배경은
각 절 끝의 링크에 있습니다.

기준은 v0.5.0 이후의 `main`입니다(프롬프트 압축, `current.md` 아카이브, `chat`
명령 포함).

## 1. 한 줄 요약

Codex·Claude Code·Antigravity(`agy`) 같은 **구독형 CLI** 사이에서, 한쪽이 한도(quota, rate
limit, context 등)에 걸리면 **작업 폴더의 파일**을 통해 다음 CLI에게 일을 넘기는
도구입니다. 대화 기록을 옮기지 않습니다. 모든 CLI가 같은 폴더의 파일
(`.handoff/current.md`, git 상태)을 읽고 쓰기 때문에, 어느 쪽이 이어받아도 같은
출발점에서 시작합니다.

## 2. 큰 그림

```text
  [입구]
  터미널 CLI · chat  웹 UI(브라우저)       데스크톱 앱(Tauri)      HTTP 원격(선택)
  handoff_bridge.py  handoff_webui.py      src-tauri/ + sidecar   remote_handoff_server.py
        \                 |                     |                     /
         \                v                     v                    /
          '-------->  handoff_bridge.py run  (subprocess로 호출)  <--'
                              |
                  run_command → run_provider
                              |
        .---------------------+---------------------.
        v                     v                     v
  codex exec --json   claude -p ... stream-json   agy --output-format json
        \                     |                     /
         '------------> 결과 파싱 · 분류 <----------'
                              |
                              v
        .handoff/state.json   .handoff/current.md   .handoff/runs/<id>/
          (기계가 읽는 상태)     (사람·에이전트가 읽는 로그)   (원본 로그)
```

핵심은 **모든 입구가 결국 같은 한 곳(`handoff_bridge.py run`)을 거친다**는
점입니다(`chat`은 서브프로세스 대신 같은 함수 `run_command`를 직접 부릅니다). 웹 UI도 데스크톱 앱도 로직을 따로 갖지 않고, 사람이 터미널에서 칠 명령과
똑같은 명령을 서브프로세스로 실행합니다. 그래서 동작이 어디서든 같습니다.

## 3. 파일 지도

| 파일 | 하는 일 |
|---|---|
| `handoff_bridge.py` | 핵심. 설치, 작업 패킷 생성, 프롬프트 구성, provider 실행, 결과 분류, 상태 기록, 대화형 `chat` |
| `handoff_webui.py` | 웹 UI의 HTTP 서버(표준 라이브러리 `ThreadingHTTPServer`)와 API 라우팅 |
| `webui_common.py` | 웹 UI가 `handoff_bridge.py`를 서브프로세스로 부르는 경계(`bridge_command_prefix`) 등 공용 도구 |
| `webui_bridge_run.py` | `/api/run`이 실제로 provider를 돌리는 부분 |
| `webui_chat_storage.py` | 채팅 기록 저장(월별 JSONL, 지난 달은 gzip) |
| `webui_workspace.py` | 작업 폴더 선택·파일 트리·자동 폴더 생성 |
| `webui_credentials.py` | provider별 기본 `--model` 설정만 저장(키는 저장하지 않음) |
| `webui/` | 브라우저 화면(vanilla JS) |
| `src-tauri/` | 데스크톱 앱 셸(Rust). 웹 UI 서버를 사이드카로 띄우고 창에 보여줌 |
| `scripts/validate_handoff.py` 등 | 토큰 없이 도는 검증(`handoff_bridge.py check`) |
| `docs/` | 두 CLI가 따르는 규약 문서와 설계 문서 |

## 4. 명령 한 번이 실행되는 과정

`python3 handoff_bridge.py run auto --execute "작업 이어서 해줘"` 기준입니다.

1. **작업 폴더로 이동**: `main()`이 `--workspace` 폴더로 `chdir`합니다. 이후
   모든 경로(`.handoff/...`)는 이 폴더 기준 상대 경로입니다. 웹 UI가 프로세스
   안에서 직접 함수를 부르지 않고 서브프로세스를 쓰는 이유가 이것입니다 — `chdir`은
   프로세스 전체에 영향을 주므로 여러 요청이 도는 서버 안에서는 쓸 수 없습니다.
2. **실행 락 획득**: `run_command()`가 `.handoff/.run.lock`을 잡습니다. 같은 폴더에
   `run`이 둘 동시에 돌면 상태가 꼬이므로, 두 번째는 기다리다(최대 3600초) 종료
   코드 75로 실패합니다.
3. **상태 읽기**: `load_state()`가 `.handoff/state.json`을 읽습니다. 파일이 없거나
   깨졌으면 `default_state()`로 시작합니다. 처음이면 이때 `task`, `primary_provider`
   (기본 `codex`)를 정해 저장합니다.
4. **provider 선택**: `auto`면 `choose_auto_provider()`가 정합니다.
   - 직전 실행이 "넘겨야 함"(`status == "handoff_needed"`)이면
     `next_available_provider(last_provider)` — 설치된 다음 CLI.
   - 아니면 `primary_provider`(설치돼 있을 때). 없으면 설치된 첫 CLI.
5. **`run_provider()` 실행**
   1. (`--execute`일 때) `current.md`가 60,000자를 넘으면 오래된 항목을
      `.handoff/archive/`로 옮기고 오프셋을 저장합니다(9절). 그다음 `build_prompt()`로
      프롬프트를 만듭니다(5절).
   2. `provider_command()`로 실행 명령을 만듭니다(6절).
   3. `--execute`가 없으면 여기서 끝납니다. 프롬프트를 `.handoff/next-prompt.md`에
      쓰고 "토큰을 쓰지 않았다"고 알린 뒤 종료하는 **미리보기**입니다.
   4. 실행 디렉터리 `.handoff/runs/<UTC시각>-<provider>/`를 만들고 `prompt.md`를
      저장합니다.
   5. `subprocess.run(command, input=prompt, ...)`로 CLI를 실행합니다. 프롬프트는
      **표준입력**으로 보냅니다(긴 프롬프트를 인자로 넘기면 OS 길이 제한에 걸리고
      프로세스 목록에 노출되기 때문). 인코딩은 UTF-8로 고정합니다(한국어
      Windows의 cp949에서 실제로 터진 적이 있습니다).
   6. 종료 코드, stdout, stderr를 `runs/` 아래에 저장합니다. CLI가 없으면 127,
      시간 초과면 124로 처리합니다.
   7. provider별 파서로 결과를 요약하고(6절), `classify_handoff()`로 "넘겨야
      하는가"를 판정합니다(7절).
   8. 세션 ID를 `state["sessions"]`에 저장합니다(다음 턴에 `--resume`으로 이어서
      쓰기 위해서).
   9. `state["history"]`에 기록을 추가하고 `save_state()`, 이어서 `append_current()`로
      `.handoff/current.md`에 사람이 읽는 블록을 덧붙입니다.
   10. 넘겨야 한다면 자동 전환을 처리합니다(8절).

### 측정: `stats`

`run_provider()`는 매 실행의 소요 시간(`duration_seconds`)을 기록에 남기고,
`handoff_bridge.py stats`(`compute_stats`)가 `state.json`의 기록을 provider별로 집계합니다:
실행 수, 핸드오프 없이 끝난 수(ok)와 사유, CLI가 보고한 토큰, 시간, 그리고 **ok 실행 1회당
토큰·시간**(측정값이 있는 실행만 모아 그중 ok 수로 나눔. 일부 실행에 측정값이 없으면 "partial"로
표시)과, 핸드오프를 이어받은 실행(`handoff_from`으로 연결된 기록)이 다시 핸드오프 없이 끝난 비율. ok는 "핸드오프 신호가 없었다"는 뜻일 뿐 결과가 맞다는 보증이 아니며, 토큰은 CLI마다
기준이 달라 provider끼리 비교할 수 없고 가격은 적용하지 않습니다.

### 대화형 입구: `chat`

`handoff_bridge.py chat`은 입력 한 줄을 `run_command()`에 그대로 넘기는 REPL입니다
(`ChatSession`). 상태 파일, 세션 재개, 자동 전환, 프롬프트 구성이 `run --execute`와 같고,
턴마다 토큰 줄을 덧붙입니다. `sent ~N`은 그 턴 `prompt.md` 글자 수 ÷ 4 추정치이고,
`reported ...`는 provider CLI가 돌려준 `usage`(`usage_token_counts`)입니다. 입력 중
Ctrl-D·Ctrl-C는 반쯤 쓴 메시지를 버리고, 실행 중 Ctrl-C는 provider 프로세스를 멈추며
그 턴은 기록되지 않습니다.

## 5. 프롬프트 만들기 — `build_prompt()`

프롬프트는 세 가지 경우로 나뉩니다.

| 경우 | 조건 | 내용 |
|---|---|---|
| **이어서 쓰기** | 그 provider에 살아있는 세션 ID가 있고 넘김 사유가 없음 | 새 메시지, git 상태, 그 provider가 마지막으로 본 뒤 `current.md`에 추가된 부분만 (약 600토큰) |
| **처음 / 넘겨받기** | 세션이 없거나, 다른 provider가 한도에 걸려 넘어온 경우 | 작업 설명, 사용자 메시지, 넘김 사유, 규약 요약("Working Rules"), `current.md` 최신 항목 5,000자, git 상태 (약 1,200~2,200토큰) |
| **미리보기** | `--execute` 없음 | 위와 같은 프롬프트를 파일로만 저장 |

이어서 쓰기가 작은 이유: 그 provider의 CLI 세션이 이미 첫 턴의 규약·작업을
기억하고 있어 다시 보낼 필요가 없습니다. "마지막으로 본 위치"는
`state["prompt_context_offsets"][provider]`에 **provider별로** 저장합니다. 중간에 다른
provider가 한 턴 돌았더라도 이 provider가 못 본 부분을 건너뛰지 않게 하려는
것입니다.

**컴팩트 모드(기본)와 전체 모드.** 처음/넘겨받기 프롬프트는 규약 문서 3개(약 9,400자)
전문 대신 짧은 요약(`PROTOCOL_SUMMARY`)을 담고, `current.md`는 `##`/`###` 항목 단위로
최신 것부터 5,000자까지만 담습니다(`recent_log_for_prompt`). 이 저장소의 실제 로그 기준
약 8,200토큰이 약 2,200토큰으로 줄었습니다. 워크스페이스가 세 문서 중 하나라도
번들 원본과 다르게 고쳐 쓴 경우(`protocol_docs_customized`, 줄바꿈 차이는 무시)
컴팩트 모드가 스스로 전체 모드로 바뀌어 고친 규칙이 빠지지 않습니다.
`run --full-prompt`로 강제할 수도 있습니다. 전체 모드는 문서 3개 전문과
20,000자 꼬리를 보냅니다.

항상 들어가는 두 개의 고정 안내문은 실제로 겪은 문제에서 나왔습니다.

- `SELF_CONTAINED_NOTICE`: 이미 프롬프트에 포함된 문서를 에이전트가 다시 열어
  왕복을 낭비하거나, 이 프로젝트의 "handoff"를 전혀 다른 전역 스킬과 혼동하던 문제.
- `SCOPE_DISCIPLINE_NOTICE`: 요청하지 않은 버그를 찾아 고치거나 전체 테스트를 돌리며
  한 마디 메시지가 수 분짜리 작업으로 부풀던 문제.

`git_snapshot()`은 `git status --short`와 `git diff --stat`을 붙입니다. git 저장소가
아니면 `git diff`는 건너뜁니다(저장소 밖의 `git diff`가 7KB짜리 도움말을 뱉기
때문).

사용자가 `.handoff/shared-context.md`를 써 두었다면(비어 있지 않을 때만) 모든
프롬프트에 "Project Context"로 포함됩니다.

## 6. provider 호출과 결과 읽기

| provider | 명령 (요약) | 출력 형식 | 이어서 쓰기 |
|---|---|---|---|
| codex | `codex exec --json --skip-git-repo-check --sandbox workspace-write` | JSONL 이벤트 | `codex exec resume ... <세션ID> -` |
| claude | `claude -p --input-format text --output-format stream-json --verbose --permission-mode auto` | JSONL 이벤트 | `claude --resume <세션ID> ...` |
| agy | `agy --output-format json` (프롬프트는 stdin, `-p` 사용 안 함) | 끝에 JSON 객체 하나 | `--conversation <대화ID>` |

`--model`은 진짜 모델 이름일 때만 넘깁니다. `app-selected default`, `provider default`,
`default`, `unknown`은 기록용 라벨이라 CLI에 넘기지 않습니다(`model_override_arg()`).

결과 파서(`summarize_codex` / `summarize_claude` / `summarize_agy`)는 공통으로
세션 ID, 사용량(`usage`), 최종 답변(`final_text`), 오류 목록(`errors`)을 뽑습니다.
Claude는 `cost_usd`도 있습니다. 모델이 파일을 읽다가 "rate limit"이라는 글자를
*본* 것과 *실제로 걸린* 것을 구분하는 일은 다음 절의 판정이 맡습니다.

## 7. "넘겨야 하는가" 판정 — `classify_handoff()`

반환값은 `(넘겨야 하는가, 사유 문자열)`이고 사유는 항상 정해진 라벨로 시작합니다:
`quota`, `rate_limit`, `auth`, `billing`, `context_limit`, `overloaded`,
`tool_failure`, `unknown` (없으면 `none`). 이 어휘는 `HANDOFF_LABELS`와
`docs/shared-agent-contract.md`가 일치해야 하고 `check`가 불일치를 잡습니다.

판정 순서:

1. 검사 대상은 **stderr, stdout 중 JSON이 아닌 줄, 그리고 파서가 모르는 종류의 JSON
   이벤트가 가진 오류성 필드(`error`·`message`·`status` 등)**뿐입니다. codex/claude의
   알려진 이벤트 줄과 agy의 JSON 한 덩어리는 답변, 중간 설명, 도구 출력이 섞여 있어
   통째로 제외합니다(`_signal_scan_text()`). 알려진 이벤트의 진짜 실패는 오류
   목록(`errors`)으로 따로 들어오고 2번에서 처리합니다. 앞으로 CLI가
   `{"type":"warning","message":"usage limit reached"}` 같은 새 이벤트를 내보내도
   놓치지 않으려는 안전장치입니다. '알려진 이벤트'는 provider별로 따로 정해서(codex의
   `item.completed`, claude의 `result` 등), 한쪽 CLI의 정상 이벤트 이름이 다른 쪽의
   새 오류 이벤트를 가리지 못하게 합니다. 이 검사는 agy의 JSON 한 덩어리를 뺀 모든
   stdout에 적용되므로 이벤트가 한 줄뿐이어도 똑같이 걸립니다. 예전에는 최종 답변과 도구 출력을 문자열로 *지운 뒤* 검사했는데,
   한글 줄이 지워지지 않거나(CLI는 UTF-8 그대로, 지우는 쪽은 `\\uXXXX` 형태) 짧은 조각이
   먼저 지워져 긴 줄이 깨지면서 정상 응답이 "quota"로 판정되는 오탐이 실제로 있었습니다
   (2026-10-29).
2. provider가 **기계가 읽을 수 있는 오류 이벤트**(`errors`)를 냈다면 `ERROR_PATTERNS`
   정규식을 차례로 대조해 라벨을 붙이고, 매칭이 없으면 `unknown`으로 분류합니다.
3. 오류 이벤트가 없어도 남은 텍스트가 패턴에 맞으면 해당 라벨로 분류합니다.
4. 종료 코드가 127이면 `tool_failure: command not found`, 0이 아니면
   `tool_failure: exited with code N`.
5. 모두 아니면 `none`.

정규식 판정이라 완벽하지 않습니다. 그래서 "정상 응답이 우연히 패턴 단어를 포함"하는
경우를 줄이는 방향(1번, 그리고 `auth` 패턴이 `set an auth method`처럼 CLI 고유
문구를 쓰는 식)으로 계속 보강해 왔습니다. 남은 약점은 JSON이 아닌 일반 텍스트 줄, stderr, 모르는 이벤트의 오류성 필드에
우연히 패턴 단어가 있는 경우입니다.

## 8. 넘기기와 자동 전환

`run`에 `--auto-fallback`을 주면 넘겨야 할 때 바로 다른 CLI를 실행합니다.

- 다음 provider는 `next_available_provider()`가 정합니다. `PROVIDERS` 순서
  (`codex → claude → agy → codex ...`)로 돌되 **설치되지 않은 CLI는 건너뜁니다.**
- 설치된 다른 CLI가 없으면(자기 자신만 남으면) 같은 provider를 다시 돌리지 않고
  멈춥니다.
- 전환은 **한 번만** 합니다. 재귀 호출에 `auto_fallback=False`를 넘기기 때문입니다.
  한도에 걸린 둘 사이를 오가며 토큰을 무한정 쓰는 일을 막기 위한 설계입니다.
- 전환할 때는 `instruction_type="handoff"`와 **원래 사용자 메시지**를 그대로
  넘깁니다(사유는 별도 블록으로 전달). 일반 문구로 바꾸면 받는 쪽이 무슨 일을 하던
  중인지 모르게 됩니다.
- `--auto-fallback`이 없으면 전환하지 않고, 다음 provider용 프롬프트만
  `.handoff/next-prompt.md`에 써 둡니다. 사람이 확인하고 직접 실행할 수 있습니다.

## 9. 상태 파일과 동시성

| 파일 | 누가 쓰나 | git |
|---|---|---|
| `.handoff/state.json` | 브리지만 (세션 ID, 기록, 상태, 오프셋) | 제외 |
| `.handoff/current.md` | 브리지가 실행 기록을 덧붙이고, 에이전트가 Changed/Verified/Remaining/Blocked/Next를 직접 씀 | 제외하지 않음(프로젝트와 함께 추적 가능) |
| `.handoff/archive/current-<시각>.md` | 브리지 (`current.md`에서 밀려난 오래된 항목) | 제외하지 않음(`current.md`와 함께 이동) |
| `.handoff/runs/<id>/` | 브리지 (프롬프트, stdout, stderr 원본) | 제외 |
| `.handoff/next-prompt.md` | 브리지 (미리보기·다음 프롬프트) | 제외 |
| `.handoff/shared-context.md` | 사용자 (프로젝트 공통 맥락) | 제외하지 않음 |

쓰기 안전장치 두 가지가 있습니다.

- **`atomic_write_text`**: 임시 파일에 쓰고 `os.replace`로 교체합니다. 쓰는 도중
  프로세스가 죽어도 반쯤 쓰인 파일이 남지 않습니다.
- **`WriteLock`**: `.handoff/.write.lock`을 `O_CREAT|O_EXCL`로 만들어 얻는
  프로세스 간 락입니다. `fcntl`/`msvcrt` 대신 이 방식을 쓰는 이유는 macOS·Linux·
  Windows에서 한 구현으로 동작하고, 작업마다 별도 프로세스가 도는 HTTP 원격
  서버에도 적용되기 때문입니다.

**아카이브.** `current.md`는 계속 늘어나는데, "current.md를 읽어라"는 지시를 따르는
에이전트는 프롬프트와 무관하게 파일 전체를 엽니다(281KB ≈ 7만 토큰). 그래서
`--execute` 실행 직전 60,000자를 넘으면 가장 오래된 항목부터 `archive/`로 옮기고 약
30,000자만 남깁니다(`archive_old_current_entries`, 삭제는 하지 않음). `state.json`에
저장된 provider별 읽기 위치(오프셋)는 옮긴 만큼 같이 이동시켜 provider 호출 전에
저장하며, 오프셋이 파일 길이를 넘으면 0으로 취급해 새 기록을 건너뛰지 않게 합니다.

한계: 락은 **개별 쓰기**를 보호할 뿐, 읽고-수정하고-쓰는 전체 주기를 보호하지는
않습니다. 같은 폴더에서 `run` 둘이 겹치지 않게 하는 것은 앞의 `.run.lock`의
역할입니다.

## 10. 웹 UI 계층

- **서버**: `handoff_webui.py`가 표준 라이브러리 `ThreadingHTTPServer`로 로컬에서만
  뜹니다. 프론트는 `webui/`의 vanilla JS입니다.
- **`POST /api/run`**: 입력 검증 후 `run_provider_via_bridge()`가 4절의 명령을
  **서브프로세스**로 실행합니다. 프롬프트는 임시 파일로 `--prompt-file`에 넘깁니다.
  provider별 제한 시간은 600초(`PROVIDER_RUN_TIMEOUT_SECONDS`)이고, 바깥 래퍼는
  자동 전환 두 번 + 락 대기 여유를 합쳐 1260초에서 강제로 끊습니다.
- **결과 읽기**: 실행 전후의 `state["history"]` 길이를 비교해 새로 생긴 기록을
  채팅 메시지로 바꿉니다. 서브프로세스가 기록조차 못 남기고 죽으면 가짜 기록을
  만들어 항상 사용자에게 무언가 보이게 합니다.
- **동시 실행 방지**: 같은 작업 폴더의 실행은 폴더별 락 하나(`get_run_lock_for`)로
  직렬화하고, 이미 실행 중이면 기다리지 않고 409로 즉시 알립니다. 서로 다른
  폴더는 병렬로 돌 수 있습니다.
- **채팅 저장**: `.handoff/webui/chat/YYYY-MM.jsonl`(지난 달은 gzip). 같은
  `WriteLock`으로 보호합니다.
- **설정**: CLI별 기본 `--model`만 `~/Documents/Agent Handoff Bridge/credentials.json`의
  `cli_models`에 저장합니다. v0.5.0에서 API 키 모드와 사용자 정의 provider를
  제거했으므로 새 키는 저장하지 않습니다(DEC-29).

## 11. 데스크톱 앱

- Tauri(Rust) 셸이 **사이드카** 실행 파일을 띄우고, 사이드카가 127.0.0.1:8787에서
  웹 UI 서버를 서비스하면 창에 그 주소를 보여줍니다. 사이드카는 PyInstaller로 만든
  독립 실행 파일 4개(`agent-handoff-bridge-server`, `-cli`, `-validate`, `-scan`)라서
  사용자가 Python을 설치하지 않아도 됩니다.
- 사이드카 안에서는 `sys.executable`이 진짜 Python이 아니라 얼어붙은(frozen)
  서버 바이너리입니다. 그래서 4절의 서브프로세스 호출은 `bridge_command_prefix()`가
  같은 폴더의 `agent-handoff-bridge-cli` 바이너리를 직접 가리키도록 분기합니다.
- 앱을 닫을 때 사이드카를 함께 종료하고, 8787 포트가 이미 쓰이면 안내 메시지를
  띄웁니다(둘 다 실제로 깨져 있다가 고친 부분).
- 설치형 앱의 자동 업데이트는 Tauri 업데이터 플러그인이 릴리스의 `latest.json`을
  확인합니다. 소스 zip 사용자는 `check_for_update()`가 `gh release view`로 새 버전
  배지를 띄우며, `gh`가 없거나 오프라인이면 "알 수 없음"으로 조용히 처리합니다.

## 12. 품질 게이트와 릴리스

`python3 handoff_bridge.py check`는 모델을 부르지 않고 `scripts/validate_handoff.py`의
`main()`이 차례로 부르는 일곱 가지를 확인합니다: 필수 파일 존재, 규약 문서 참조,
JSON 문법, Python 문법, 실패 라벨 어휘 일치, 시크릿 스캔(`scan_secrets.py`),
`tests/` 전체 실행. 브랜치 이름 규칙(`type/short-description`)은 별도 스크립트이고
git 훅으로 로컬에서 강제합니다. 자동 CI는 GitHub Actions 한도 때문에 제거되어
있으므로 PR 전에 직접 돌립니다([Quality Gates](quality-gates.md)).

릴리스는 버전 4곳 갱신 → 릴리스 노트 → `check` → 소스 zip 생성 → 태그 →
(Actions) 인스톨러 빌드 → 서명된 `latest.json` → GitHub Release 순서입니다
([Release Process](release-process.md)).

## 13. 주요 설계 결정과 이유

| 결정 | 이유 |
|---|---|
| 대화 기록이 아니라 **파일**로 넘김 | CLI마다 기록 형식과 접근 방법이 다르고 숨겨진 상태가 있어 옮길 수 없음. 폴더는 모두가 읽을 수 있는 공통 기반 |
| 웹 UI·앱이 **서브프로세스**로 CLI 호출 | `chdir`이 프로세스 전역이라 서버 안에서 직접 호출 불가. 사람이 쓰는 명령과 경로가 같아 동작이 일치 |
| 프롬프트는 **표준입력**으로 전달 | 인자 길이 제한, 프로세스 목록 노출, 파서 호환성 문제 회피 |
| 자동 전환은 **한 번만** | 둘 다 한도인 상황에서 토큰이 무한히 소모되는 것을 방지 |
| 이어서 쓰기는 **델타만** 전송 | CLI 세션이 이미 기억하므로 반복 전송은 순수 낭비 |
| **API 키 모드 제거** (DEC-29) | API는 "한도에 걸려 넘긴다"는 전제가 없고, 셸 실행 도구 루프가 앱에서 가장 큰 위험 면이었음 |
| 코드 서명 안 함 (DEC-24) | 현재 사용자 규모에서 인증서 비용·운영 부담이 이득보다 큼 |
| 의존성 없는 표준 라이브러리 | 의존성 파일 없이 어디서나 `python3 handoff_bridge.py`로 실행 |

설계 결정 번호(DEC-xx)와 미해결 충돌(CFL-xx)의 전체 기록은
`docs/design-system/flutter-mapping.html`에 있습니다.

## 14. 코드를 읽는 순서 추천

1. `handoff_bridge.py`의 `main` → `run_command` → `run_provider` (4절의 흐름)
2. `build_prompt` (5절)
3. `classify_handoff`와 `ERROR_PATTERNS` (7절)
4. `choose_auto_provider`, `next_available_provider` (8절)
5. `webui_bridge_run.py`의 `_run_provider_via_bridge_locked` (10절)
6. `tests/test_handoff_bridge.py` — 각 동작이 어떤 경우를 보장하는지의 가장 좋은 설명서
