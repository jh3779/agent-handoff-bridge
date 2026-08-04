# v0.2 Roadmap — MVP to Final Goal

**최종 목표**: [flutter-mapping.html DEC-01](flutter-mapping.html#s1b)이 가리키는
프로덕션 앱 — 프레임워크 전환(Tauri/Electron류) 스택 위에서, Codex/Claude/
Gemini를 실제로 호출하고, 워크스페이스를 자유롭게 오가며, 여러 프로젝트의
대화 기록을 한 곳에서 훑어보고, 최신 버전을 스스로 확인하는 채팅형
에이전트 클라이언트.

**지금 상태 (Phase 0, 완료)**: `handoff_webui.py` + `webui/` — 파일 브라우징,
드래그/클릭 첨부, VS Code식 Open Folder, 워크스페이스별 로컬 채팅 기록
(월별 gzip 압축), 네이티브 창(pywebview, 선택적 의존성). **provider 호출은
아직 없다.** 92개 테스트로 커버됨.

이 문서는 Phase 0과 최종 목표 사이를 순서가 있는 단계로 쪼갠다. 각 단계는
[Conflict List](flutter-mapping.html#s2)의 특정 항목을 해소하는 것을
목표로 하며, 정본은 여전히 그 Conflict List다 — 여기서는 실행 순서와
근거만 정리한다.

## 순서를 정한 기준

1. **가치가 큰 것 먼저**: "파일만 보는 도구"를 "진짜 에이전트와 대화하는
   도구"로 만드는 Phase 1이 최우선 — 나머지는 전부 그 위의 부가 기능.
2. **이미 있는 코드를 재사용할 수 있는 것 먼저**: `handoff_bridge.py`가
   이미 구현한 CLI 호출·핸드오프 분류 로직을 재사용하는 단계(Phase 1)가
   API 키를 새로 설계해야 하는 단계(Phase 4)보다 앞선다.
3. **비싸고 되돌리기 어려운 것은 마지막**: 프레임워크 전환(Phase 7)은
   기능이 계속 바뀌는 지금 시점에 하면 이중 작업이 된다. 저비용 스택
   (stdlib + pywebview) 위에서 기능을 먼저 검증하고, 안정된 기능 집합을
   한 번에 옮기는 편이 싸다.
4. **서로 독립적인 것은 순서를 유연하게**: Phase 2(자동 폴더 생성)와
   Phase 6(업데이트 확인)은 다른 단계에 의존하지 않는다 — 우선순위가
   바뀌어도 안전하게 끼워 넣을 수 있다.

## Phase 1 — Provider 연결 (CLI 모드)

**목표**: 채팅에서 실제로 Codex/Claude CLI를 호출하고 응답을 받는다.

- `POST /api/run` 신설: `handoff_bridge.py`의 `run_provider()` /
  `classify_handoff()` / `provider_command()`를 그대로 재사용(새로 만들지
  않음 — 이미 검증된 로직).
- DEC-02(세션당 첫 전송만 확인, 이후 즉시 실행) 적용 — 지금은 send가
  로컬에만 저장하므로 실제 "확인 후 실행" 흐름이 아직 없다.
- 상태 배지(대기/실행/완료/핸드오프/실패, 이미 [컴포넌트 §3](components.html#s3)/[§9](components.html#s9)에 디자인됨)를
  메시지에 실제로 붙인다.
- auto-fallback 발생 시 전환 사실을 시스템 메시지로 스레드에 표시 —
  [CFL-03](flutter-mapping.html#s2) 해소.
- **함께 하면 싼 것**: 코드블록 렌더링(DEC-03, 아직 프론트엔드에
  미구현) — Phase 1부터 provider가 코드/diff 섞인 응답을 자주 반환하므로
  이 시점에 필요해진다.

**해소하는 항목**: CFL-01, CFL-03, DEC-02/DEC-03 실제 적용.
**끝났다고 볼 수 있는 기준**: 채팅에서 실제 작업을 지시하면 Codex 또는
Claude가 응답하고, 핸드오프가 발생하면 자동으로 다른 provider로 넘어가는
것까지 실제로 동작.

## Phase 2 — 워크스페이스 미선택 시 자동 폴더 생성

**목표**: [SCR-05](wireframes.html#s7)를 실제 코드로 — 폴더를 고르지 않고
바로 메시지를 보내면 `~/Documents/Agent Handoff Bridge/<날짜-요약>`을
자동 생성해 워크스페이스로 쓴다.

작고 독립적이라 다른 단계와 순서를 바꿔도 안전하다. 온보딩 마찰을 줄이는
효과가 커서 Phase 1 직후에 배치했다.

## Phase 3 — 멀티 프로젝트 히스토리 드로어

**목표**: [SCR-03](wireframes.html#s5)의 "여러 프로젝트를 한 드로어에서"를
완성한다. Phase 0에서 이미 워크스페이스별 저장(`.handoff/webui/chat/`)은
끝났으므로, 남은 건 "최근에 연 워크스페이스 목록"을 앱 레벨에 기억하는
레지스트리와 그걸 보여주는 드로어 UI뿐이다.

**해소하는 항목**: [CFL-10](flutter-mapping.html#s2) 완전 해소.

## Phase 4 — CLI 미설치 사용자 온보딩 + API 키 모드

**목표**: [SCR-06](wireframes.html#s8)을 실제로 동작시킨다 — CLI가 없는
사용자가 API 키로 provider에 연결.

**왜 여기 오는가**: [CFL-12](flutter-mapping.html#s2)가 이미 지적하듯, 이건
"UI 옵션 하나 추가"가 아니라 `run_provider()`의 서브프로세스 구조와
근본적으로 다른 경로(HTTP/SDK 직접 호출, 세션 재개 재설계)가 필요하다.
Phase 1로 CLI 경로가 실제로 검증된 뒤에 그 대안 경로를 설계하는 게
순서상 맞다. 상세 절차는 [provider-extensibility.md](../provider-extensibility.md)
"Adding An API-Key-Based Provider" 참고.

**해소하는 항목**: CFL-12.

## Phase 5 — Gemini CLI 실사용 지원 + provider 확장성 리팩터

**목표**: 세 번째 provider를 실제로 붙인다.

1. 먼저 실제 조사 — `docs/research.md`가 Codex/Claude에 했던 것과 동일한
   조사를 Gemini CLI에 대해 수행([CFL-13](flutter-mapping.html#s2)이
   이미 "검증 안 됨"이라고 명시).
2. `handoff_bridge.py`의 `other_provider()`(이진 토글)를 N-way fallback으로
   리팩터 — [provider-extensibility.md](../provider-extensibility.md)
   "The Current Code Assumes Exactly Two Providers"에 이미 정확히 이
   문제가 문서화되어 있다.
3. `PROVIDERS` 튜플 확장, `provider_command()`/`summarize_gemini()` 추가.

Phase 1(CLI 연결)이 끝나야 의미가 있어 그 뒤에 배치했다.

## Phase 6 — 자동 업데이트 확인

**목표**: [SCR-07](wireframes.html#s9)을 실제로 동작시킨다.

**막힌 지점**: [CFL-11](flutter-mapping.html#s2) — 이 저장소가 private라
익명으로 GitHub Releases를 못 읽는다. 둘 중 하나 결정 필요:
사용자의 로컬 `gh` 인증을 재사용하거나, 버전 번호만 공개로 노출하는
별도 정적 엔드포인트를 둔다. 다른 단계와 독립적이라 언제 해도 되지만,
"릴리즈"라는 개념 자체가 의미 있어지는 시점(= 실제로 배포가 반복되기
시작하는 시점) 근처가 자연스러워 마지막 쪽에 뒀다.

## Phase 7 — 프레임워크 전환 (DEC-01, 최종 목표)

**목표**: `handoff_webui.py` + `webui/`(stdlib + 선택적 pywebview)를
DEC-01이 가리키는 실제 프로덕션 스택(Tauri/Electron류)으로 이관.

**왜 마지막인가**: Phase 1–6에서 기능·UX가 계속 바뀐다. 그 상태에서
무거운 프레임워크 전환부터 하면 같은 화면을 두 번 만드는 셈이 된다.
저비용 스택 위에서 기능을 전부 검증한 뒤, 안정된 기능 집합을 한 번만
옮기는 편이 싸다.

**해소하는 항목**: [CFL-06](flutter-mapping.html#s1b)(선택 완료, 실행만
남음), [CFL-09](flutter-mapping.html#s2)(배포 파이프라인 재설계 —
"zip 하나로 git 없이 실행" 모델이 여기서 끝나므로
[release-process.md](../release-process.md)도 이 단계에서 다시 써야 함).

---

## 상태 추적

| Phase | 상태 | 해소하는 항목 |
|---|---|---|
| 0 — 로컬 MVP | ✅ 완료 | — |
| 1 — Provider 연결(CLI) | 미착수 | CFL-01, CFL-03, DEC-02/03 적용 |
| 2 — 자동 폴더 생성 | 미착수 | (SCR-05 구현) |
| 3 — 멀티 프로젝트 히스토리 | 미착수 | CFL-10 |
| 4 — API 키 모드 | 미착수 | CFL-12 |
| 5 — Gemini + provider 확장성 | 미착수 | CFL-13 |
| 6 — 자동 업데이트 확인 | 미착수 | CFL-11 |
| 7 — 프레임워크 전환 | 미착수 | CFL-06(실행), CFL-09 |

이 표가 정본은 아니다 — 각 phase가 끝나면 여기 상태만 갱신하고, 실제
해소 근거는 [flutter-mapping.html Conflict List](flutter-mapping.html#s2)
쪽에서 그 항목을 지우거나 갱신한다.
