# 릴리스 프로세스 (한글)

이 문서는 [`docs/release-process.md`](release-process.md)의 한글
번역본입니다. 영어 원문이 정본(source of truth)이며, 이 문서는
`ko-operator-guide.md`와 같은 방식으로 별도 파일로 병기됩니다.

이 저장소의 태그된 릴리스를 만드는 방법. Phase 7b(DEC-23, CFL-09 해소)
이후로, 릴리스는 같은 버전 태그와 같은 GitHub Release에 **두 개의 병행
패키징 트랙**을 실어 배포합니다:

1. **소스 zip** (`scripts/package_platforms.py`) — git 불필요, 다만
   사용자 자신의 Python 3는 필요. 터미널/CLI 전용 사용을 위함:
   `handoff_bridge.py`를 직접 실행, 스크립팅, 헤드리스 환경. 원래의
   배포 모델이고 그대로 유지됩니다.
2. **데스크톱 인스톨러** (Tauri, `cargo tauri build`) — `.dmg`/`.app`
   (macOS, **Apple Silicon 전용** — CI 매트릭스에 `x86_64-apple-darwin`
   다리가 없어서 Intel Mac은 오늘 네이티브 인스톨러가 없음; 7b M1
   계획 당시부터 열려 있던 채로 남아있음, `docs/design-system/
   roadmap.md`의 7b 계획 항목 2 참고), `.msi`+nsis `.exe`(Windows),
   `.deb`/`.AppImage`/`.rpm`(Linux). PyInstaller sidecar를 통해 Python을
   번들하므로 최종 사용자는 Python 설치가 전혀 필요 없음. 데스크톱
   GUI 사용을 위함. **현재 미서명**(코드 서명은 Phase 7c, DEC-22/DEC-23에
   따른 별도 결정 게이트) — 그때까지는 Gatekeeper("확인할 수
   없음")/SmartScreen("알 수 없는 게시자") 경고를 예상하세요; [Security
   Model](security-model.md) 참고.

두 트랙 중 어느 쪽도 다른 쪽을 대체하지 않습니다 — 둘 다 유지하는
이유는 DEC-23(`docs/design-system/flutter-mapping.html#s1c`) 참고.

## 1. 버전 범프

`handoff_bridge.py`의 `BRIDGE_VERSION`을 수정(단일 소스 오브 트루스;
`--version`, `diagnose`, `scripts/package_platforms.py`의
`START_HERE_*.txt`가 읽음). `src-tauri/tauri.conf.json`의 `"version"`도
맞춰서 갱신 — Tauri는 `BRIDGE_VERSION`을 자동으로 읽지 않으므로, 둘을
손으로 동기화하지 않으면 데스크톱 앱과 CLI zip이 같은 릴리스에 대해
다른 버전 번호를 보고하게 됩니다.

## 2. 릴리스 노트 갱신

`docs/release-notes.md`의 `## Unreleased` 항목들을 새
`## vX.Y.Z — YYYY-MM-DD` 헤딩 아래로 옮기고, 그 위의 `## Unreleased`는
다음에 올 것을 위해 비워둡니다.

## 3. 전체 검증 스위트 실행

```bash
python3 handoff_bridge.py check
```

이게 실패하면 진행하지 마세요 — CI가 모든 pull request마다 돌리는 것과
같은 체크입니다. [Quality Gates](quality-gates.md) 참고.

## 4. 소스 zip 빌드

```bash
python3 scripts/package_platforms.py
```

`dist/agent-handoff-bridge-macos.zip`과
`dist/agent-handoff-bridge-windows.zip`을 만듭니다. 발행 전에 최소
하나는 정합성 확인하세요 — 저장소 밖 어딘가에 압축을 풀고 독립적으로
실행되는지 확인(git 저장소 없음, zip 밖 파일에 의존 없음):

```bash
cd /tmp && unzip -q /path/to/repo/dist/agent-handoff-bridge-macos.zip
cd agent-handoff-bridge-macos
python3 handoff_bridge.py --version
python3 handoff_bridge.py check
```

두 명령 다 provider 토큰을 쓰지 않고 git 저장소 없이 통과해야 합니다.
여기서 `check`가 실패했는데 3단계에서는 통과했다면,
`scripts/package_platforms.py`의 `COMMON_FILES`에서 `check`가 쓰는
파일이 빠진 것입니다 — 추가하고 다시 빌드하세요.

## 5. 커밋, 태그, 푸시

```bash
git add -A
git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

데스크톱 인스톨러를 빌드하기(6단계) **전에** 이걸 하세요 —
`installer-build` job은 자신이 가리키는 커밋 그대로 빌드하므로, 아직
푸시 안 된 작업 트리를 대상으로 트리거하면 조용히 *직전* 버전의
인스톨러가 만들어집니다.

## 6. 데스크톱 인스톨러 빌드

`installer-build` CI job(`.github/workflows/ci.yml`)이 진짜 OS별
인스톨러를 만들지만, 나머지 CI처럼 모든 PR/push마다가 아니라
의도적으로 수동 트리거(`workflow_dispatch`)로만 제한돼 있습니다.
원래는 GitHub의 비공개 저장소 Actions 과금(macOS 러너 10배, Windows
2배)을 매 push마다 피하기 위한 제한이었습니다 — 저장소가 이제
공개 전환되어 GitHub 호스팅 러너 분은 무료지만, 실제 번들 빌드는
여전히 실행 시간이 상대적으로 오래 걸리기 때문에(WiX/NSIS 다운로드,
DMG 생성) 당분간은 수동 트리거로만 유지합니다. 5단계에서 방금 푸시한
태그를 대상으로 트리거해서, 인스톨러가 정확히 그 태그된 커밋으로부터
빌드되게 하세요:

```bash
gh workflow run ci.yml --ref vX.Y.Z
```

`gh workflow run`은 새 run의 ID를 출력하지 않고, 나타나기까지 몇 초
걸릴 수 있습니다 — `gh run list`가 즉시 반환하는 걸 그냥 잡는 대신
폴링하세요. `--limit 1`만으로는 여기서 안전하지 않습니다: 이번
릴리스와 무관한 다른 수동 `workflow_dispatch` run이 더 최근에
착지했다면, 1줄짜리 창에 그것만 있어서 태그 매칭이 매번 아무것도 못
찾고 영원히 계속됩니다. 뭔가가 앞서 경합했더라도 이번 릴리스의 run이
여전히 안에 있도록 창을 넓히고, 진짜로 못 찾으면 멈추는 대신 크게
실패하도록 재시도를 제한하세요:

```bash
run_id=""
for attempt in $(seq 1 30); do
  run_id=$(gh run list --workflow=ci.yml --event=workflow_dispatch --limit 20 \
    --json databaseId,headBranch,createdAt \
    -q '[.[] | select(.headBranch == "vX.Y.Z")] | sort_by(.createdAt) | last | .databaseId // empty')
  [ -n "$run_id" ] && break
  sleep 3
done
if [ -z "$run_id" ]; then
  echo "could not find the workflow_dispatch run for tag vX.Y.Z after 30 attempts -- check manually:"
  gh run list --workflow=ci.yml --event=workflow_dispatch --limit 20
  exit 1
fi
gh run watch "$run_id"
```

초록불이 뜨면 산출물 다운로드:

```bash
gh run download "$run_id" --dir /tmp/agent-handoff-bridge-installers
```

`actions/upload-artifact@v4`는 매칭된 파일들의 공통 조상 아래 각
포맷의 하위 디렉터리를 보존하므로, 이건
`installers-<target-triple>/<포맷>/<파일>`을 만듭니다(예:
`installers-aarch64-apple-darwin/dmg/agent-handoff-bridge_X.Y.Z_aarch64.dmg`) —
평평한 디렉터리가 아닙니다 — 포맷 하위 디렉터리(`dmg`, `macos`,
`msi`, `nsis`, `deb`, `appimage`, `rpm`)는 `.github/workflows/ci.yml`
자체의 `src-tauri/target/release/bundle/<포맷>/` 업로드 경로와
일치합니다. 최소 하나의 인스톨러는 실제로 실행해서(앱 설치 +
실행, 타이틀바 업데이트 확인 배지와 기본 채팅 왕복이 되는지 확인)
정합성 확인하세요 — 4단계의 zip 확인처럼 스크립트로 할 수 없으니
발행 전에 수동으로 하세요.

매트릭스 다리 하나라도 실패하면, 추측하지 말고 run의 로그를 직접
확인하세요(`gh run view "$run_id" --log-failed`) — Windows/Linux
번들링에는 실제로 이전에 겪은 플랫폼별 실패 사례가 있습니다
(`docs/design-system/roadmap.md`의 "7b M3 실제로 한 것"에 이미 우회한
것들 참고, 예: `ubuntu-latest`의 알려진 업스트림
`linuxdeploy`/AppImage 문제, tauri-apps/tauri#14796).

## 7. GitHub Release 발행

```bash
gh release create vX.Y.Z \
  dist/agent-handoff-bridge-macos.zip \
  dist/agent-handoff-bridge-windows.zip \
  /tmp/agent-handoff-bridge-installers/installers-aarch64-apple-darwin/dmg/*.dmg \
  /tmp/agent-handoff-bridge-installers/installers-x86_64-pc-windows-msvc/nsis/*.exe \
  /tmp/agent-handoff-bridge-installers/installers-x86_64-unknown-linux-gnu/appimage/*.AppImage \
  --title "vX.Y.Z" \
  --notes-file <(sed -n "/## vX.Y.Z/,/## /p" docs/release-notes.md | sed '$d')
```

OS당 인스톨러 하나씩만 첨부(`.dmg`, nsis `.exe`, `.AppImage`)하면
릴리스 페이지가 거의 중복인 포맷들로 어지러워지지 않습니다 — `.msi`,
`.app`, `.deb`, `.rpm`도 만들어지고, 사용자가 특정 포맷을 요청하면
따로 첨부할 수 있습니다(`gh release upload vX.Y.Z <파일>`). 릴리스
노트에 인스톨러가 미서명이라는 것과 각 OS가 어떤 경고를 보여주는지
적어두세요(위 6단계 도입부 참고).

`--notes-file` 명령은 `docs/release-notes.md`에서 새 버전 섹션만
추출하므로 릴리스 본문과 체인지로그가 절대 어긋나지 않습니다. `gh
release view vX.Y.Z --web`으로 렌더링된 노트를 확인하고, 추출이
잘못돼 보이면 `gh release edit vX.Y.Z --notes-file <파일>`로
수정하세요(`sed` 범위 매칭은 최선 노력이지 완벽하지 않습니다 —
특이한 헤딩 텍스트가 있는 릴리스 노트에서는 믿기 전에 확인하세요).

## 8. 검증

```bash
gh release view vX.Y.Z
```

zip 자산과 OS당 최소 하나의 인스톨러가 첨부됐는지, 다운로드 링크가
동작하는지 확인하세요 — 저장소가 공개 전환되어 이제 저장소 접근 권한이
없어도 됩니다([Security Model](security-model.md) 참고).

## 참고 사항

- `dist/`와 `src-tauri/target/` 둘 다 gitignore 대상입니다; 빌드된
  zip과 인스톨러는 GitHub Release만 갖고 있습니다.
- 기존 태그 아래 자산을 다시 빌드해서 재업로드하지 마세요 — 대신 새
  패치 버전을 만드세요, 그래야 버전 번호가 항상 정확히 한 세트의
  파일을 의미합니다.
- 두 패키징 트랙 모두 의도적으로 계속 지원됩니다(DEC-23) — 터미널/
  스크립트 사용을 위한 소스 zip, 데스크톱 GUI 사용을 위한
  인스톨러. 같은 방식으로 새로 기록된 결정 없이는 어느 쪽도 빼지
  마세요.
- **이 런북의 인스톨러 트랙은 v0.2.0을 자를 때 처음으로 실제
  실행됐습니다** — 다음 릴리스 전에 알아두면 좋은 것들:
  - 사용된 `gh` 버전에서는 `gh workflow run`이 실제로 새 run의 URL을
    바로 출력해서, 그땐 6단계의 제한된 폴링 루프가 필요 없었습니다 —
    하지만 이게 모든 `gh` 버전에서 보장되지 않으므로 문서에는 그대로
    남겨둡니다; URL이 그냥 돌아오면 폴링하는 대신 거기서 run ID만
    추출하세요.
  - Windows `installer-build` 다리가 실제 빌드·검증·산출물 업로드가
    이미 성공적으로 끝난 *뒤에* 도는 post-job 단계
    (`Swatinem/rust-cache`의 캐시 저장 정리)에서 30분 job 타임아웃에
    걸렸습니다 — 그래서 job이 보고한 `conclusion`이 `success`가 아니라
    `cancelled`였지만, 실제 인스톨러는 이미 완전히 업로드돼서 다운로드
    가능한 상태였습니다. `cancelled` conclusion을 자동으로 실패로
    취급하지 말고 먼저 산출물이 실제로 존재하는지 확인하세요
    (`gh api repos/<owner>/<repo>/actions/runs/<run_id>/artifacts`) —
    빌드 자체가 실패한 게 아니라 정리 단계에서 타임아웃에 걸린
    것일 수 있습니다. 이게 계속 반복되면 그 job의 `timeout-minutes`를
    늘려야 할 수도 있습니다.
  - `gh release create`/`gh release upload` 둘 다 실제로 다운로드한
    산출물을 대상으로 문서화된 그대로 동작했습니다.
