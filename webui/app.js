// Agent Handoff Bridge -- MVP web UI client.
// File browsing + attaching + a VS Code-style "Open Folder" switch, with a
// local per-workspace chat draft history. "Send" persists the message to
// <workspace>/.handoff/webui/chat/ *and* actually calls Codex/Claude via
// POST /api/run (Phase 1) -- attachments are folded into that prompt, not
// just the chat log. See webui/index.html's composer-note and
// docs/provider-extensibility.md for what's intentionally not wired up yet.
(function () {
  "use strict";

  // AHB_I18N is defined in webui/i18n.js, loaded before this file --
  // see that file's own header for why translation is a separate module
  // (and its narrow scope: UI chrome only, never chat message content).
  const t = AHB_I18N.t;

  const MAX_PREVIEW_CHARS = 20000;

  // M2 (multi-session, docs/research-session-splitting.md): every request
  // names which open session (tab) it means via this header -- must match
  // handoff_webui.SESSION_HEADER exactly. DEFAULT_SESSION_ID must match
  // webui_chat_storage.DEFAULT_SESSION_ID -- both sides hardcode the same
  // literal rather than one fetching it from the other over the wire for
  // a single constant string.
  const SESSION_HEADER = "X-AHB-Session";
  const DEFAULT_SESSION_ID = "default";

  const tabBarEl = document.getElementById("tab-bar");
  const treeEl = document.getElementById("tree");
  const workspaceLabel = document.getElementById("workspace-label");
  const openFolderBtn = document.getElementById("open-folder-btn");
  const providerSelect = document.getElementById("provider-select");
  const modelOverrideInput = document.getElementById("model-override-input");
  const modelOverrideDatalist = document.getElementById("model-override-datalist");
  const chatThread = document.getElementById("chat-thread");
  const dropzone = document.getElementById("dropzone");
  const composerInput = document.getElementById("composer-input");
  const composerAttachments = document.getElementById("composer-attachments");
  const sendBtn = document.getElementById("send-btn");
  const attachBtn = document.getElementById("attach-btn");
  const fileInput = document.getElementById("file-input");
  const toast = document.getElementById("toast");
  const folderPromptOverlay = document.getElementById("folder-prompt-overlay");
  const folderPromptInput = document.getElementById("folder-prompt-input");
  const folderPromptCancel = document.getElementById("folder-prompt-cancel");
  const folderPromptConfirm = document.getElementById("folder-prompt-confirm");
  const historyBtn = document.getElementById("history-btn");
  const historyDrawer = document.getElementById("history-drawer");
  const historyScrim = document.getElementById("history-scrim");
  const historyCloseBtn = document.getElementById("history-close-btn");
  const historyList = document.getElementById("history-list");
  const settingsBtn = document.getElementById("settings-btn");
  const settingsPanelOverlay = document.getElementById("settings-panel-overlay");
  const settingsPanelClose = document.getElementById("settings-panel-close");
  const filePreviewOverlay = document.getElementById("file-preview-overlay");
  const filePreviewName = document.getElementById("file-preview-name");
  const filePreviewNote = document.getElementById("file-preview-note");
  const filePreviewContent = document.getElementById("file-preview-content");
  const filePreviewClose = document.getElementById("file-preview-close");
  const providerPanelList = document.getElementById("provider-panel-list");
  const contextTextarea = document.getElementById("context-textarea");
  const contextPanelSave = document.getElementById("context-panel-save");
  const autoFallbackToggle = document.getElementById("auto-fallback-toggle");
  const themeSelect = document.getElementById("theme-select");
  const languageSelect = document.getElementById("language-select");
  const settingsVersionValue = document.getElementById("settings-version-value");
  const updateBtn = document.getElementById("update-btn");
  const updateDot = document.getElementById("update-dot");
  const updatePopover = document.getElementById("update-popover");
  const updatePopoverVersion = document.getElementById("update-popover-version");
  const updatePopoverNote = document.getElementById("update-popover-note");
  const updateLaterBtn = document.getElementById("update-later-btn");
  const updateReleaseNotesBtn = document.getElementById("update-release-notes-btn");

  let dragDepth = 0;
  // DEC-02: only the first send *in this page load* confirms that tokens
  // may be spent; every send after that runs immediately. Resets on page
  // reload -- intentionally not persisted. Deliberately page-load-scoped,
  // not per-tab (M2): a fresh app-level tab is not "a fresh session" in
  // DEC-02's sense, just another view into the same running app -- asking
  // again per tab would be a repeated nag with no real safety benefit.
  let sessionRunConfirmed = false;

  // ---------- multi-session (M2, docs/research-session-splitting.md) ----------
  //
  // One shared set of DOM elements (chat thread, tree, composer, etc.) is
  // repainted for whichever session is "active" -- switching tabs re-fetches
  // that session's workspace/tree/chat from the server (the same round trip
  // "Open Folder"/a History-drawer item already pays today) rather than
  // maintaining N independent cached DOM subtrees, which would be a much
  // larger and riskier change for comparable user-visible benefit. What
  // *is* kept in memory per session, cheaply, so it's never lost across a
  // switch: attachments-in-progress, the composer draft, and the
  // provider/model selection -- everything else (chat messages, file tree)
  // is cheap enough to just re-fetch.
  function freshSessionMeta() {
    return {
      hasWorkspace: false,
      /** @type {{name: string, path: string|null, content: string|null, truncated: boolean}[]} */
      attachments: [],
      composerDraft: "",
      provider: "auto",
      model: "",
      // true while this tab's model input just mirrors the provider's saved
      // default (so clearing/changing that default must clear/change it here
      // too); false once the user typed their own override in the titlebar.
      modelInherited: true,
      // Guards against a second concurrent /api/run for *this* session --
      // sendBtn.disabled alone doesn't stop the Enter-key send path, and
      // updateSendState() (run on every keystroke) would otherwise
      // re-enable it while a run is in flight -- a real race that could
      // duplicate an already-persisted agent message (server-side
      // backstop: handoff_webui.RunAlreadyInProgressError).
      runInFlight: false,
      // Set when a send finishes while a *different* tab was active, so
      // that tab's reply couldn't be rendered live -- cleared the next
      // time this session becomes active (its chat history is re-fetched
      // then anyway, which already contains the reply).
      hasUnseenReply: false,
      // Mirrors GET /api/info's `name`/`workspace === null` for this
      // session -- kept even while a different tab is active so the tab
      // bar has something to show without re-fetching for every tab on
      // every render.
      workspaceName: null,
    };
  }

  let activeSessionId = DEFAULT_SESSION_ID;
  /** @type {Map<string, ReturnType<typeof freshSessionMeta>>} */
  const sessionMetaById = new Map([[DEFAULT_SESSION_ID, freshSessionMeta()]]);

  function activeMeta() {
    return sessionMetaById.get(activeSessionId);
  }

  // Functions, not static objects: STATUS_LABEL's text must reflect
  // whichever language is active *at render time*, not whichever was
  // active when this script first evaluated -- a static object baked in
  // here would never update after a language switch.
  const STATUS_LABEL_KEY = { success: "status.success", handoff: "status.handoff", fail: "status.fail" };
  function statusLabel(status) {
    return STATUS_LABEL_KEY[status] ? t(STATUS_LABEL_KEY[status]) : status;
  }
  const STATUS_ICON = { success: "✅", handoff: "🔀", fail: "⚠️" };

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast._t);
    showToast._t = window.setTimeout(() => toast.classList.remove("show"), 3200);
  }

  // ---------- transport ----------
  // The desktop app has no network port: its shell sets window.__AHB_IPC__ and
  // sends every request through the shell's `api_request` command, which hands
  // it to the bundled server over stdin/stdout (see src-tauri/src/lib.rs and
  // webui_stdio.py). In a plain browser (python3 handoff_webui.py) the same
  // calls are ordinary fetches. Both return the little bit of the Response API
  // this file uses: ok, status, json(), text().

  // Base64 -> UTF-8 text, for the rare non-text body the server sends.
  // Pure; tested from tests/test_webui_frontend_logic.py.
  function decodeBase64Utf8(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder("utf-8").decode(bytes);
  }

  // `invoke` is the Tauri command invoker when the shell provided one, else null.
  // Pure given its arguments; tested.
  function createApiFetch(invoke, plainFetch) {
    if (!invoke) return plainFetch;
    return async function apiFetch(url, init) {
      const options = init || {};
      const response = await invoke("api_request", {
        request: {
          method: options.method || "GET",
          path: url,
          headers: options.headers || {},
          body: options.body == null ? null : String(options.body),
        },
      });
      const text = response.encoding === "base64" ? decodeBase64Utf8(response.body) : response.body;
      return {
        ok: response.status >= 200 && response.status < 300,
        status: response.status,
        json: async () => JSON.parse(text),
        text: async () => text,
      };
    };
  }

  const apiFetch = createApiFetch(
    window.__AHB_IPC__ && window.__TAURI__ && window.__TAURI__.core ? window.__TAURI__.core.invoke : null,
    (url, init) => fetch(url, init)
  );

  // Opens an http(s) link in the user's own browser. In the desktop app that goes
  // through the shell plugin (window.open does not leave the app window); in a
  // browser it is a normal new tab.
  function openExternal(url) {
    const shell = window.__AHB_IPC__ && window.__TAURI__ && window.__TAURI__.shell;
    if (shell && typeof shell.open === "function") {
      Promise.resolve(shell.open(url)).catch(() => window.open(url, "_blank", "noopener"));
      return;
    }
    window.open(url, "_blank", "noopener");
  }

  // `sessionId`: which open tab this request is scoped to. Omit to use
  // whichever tab is active *at call time* (fine for most call sites);
  // pass it explicitly wherever a long-running async operation needs to
  // keep targeting the tab it started in even if the user switches away
  // before it resolves (sendMessage() is the one place this actually
  // matters -- see its own comment).
  async function fetchJSON(url, sessionId) {
    const res = await apiFetch(url, { headers: { [SESSION_HEADER]: sessionId || activeSessionId } });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `request failed: ${res.status}`);
    }
    return data;
  }

  async function postJSON(url, payload, sessionId) {
    const res = await apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", [SESSION_HEADER]: sessionId || activeSessionId },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `request failed: ${res.status}`);
    }
    return data;
  }

  async function deleteJSON(url, sessionId) {
    const res = await apiFetch(url, { method: "DELETE", headers: { [SESSION_HEADER]: sessionId || activeSessionId } });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `request failed: ${res.status}`);
    }
    return data;
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children || []) {
      node.appendChild(child);
    }
    return node;
  }

  // ---------- workspace label + open folder ----------

  function updateComposerPlaceholder() {
    composerInput.placeholder = activeMeta().hasWorkspace
      ? t("composer.placeholder.hasWorkspace")
      : t("composer.placeholder.noWorkspace");
  }

  // `sessionId` defaults to whichever tab is active *right now* -- fine
  // for every call site except boot()'s tab-restore loop, which refreshes
  // a tab that may not be the active one yet.
  function refreshWorkspaceLabel(sessionId) {
    sessionId = sessionId || activeSessionId;
    return fetchJSON("/api/info", sessionId).then((info) => {
      const meta = sessionMetaById.get(sessionId);
      if (!meta) return info; // the tab was closed while this request was in flight
      meta.hasWorkspace = info.workspace !== null;
      meta.workspaceName = meta.hasWorkspace ? info.name : null;
      if (sessionId === activeSessionId) {
        workspaceLabel.textContent = meta.hasWorkspace ? info.name : t("workspace.none");
        openFolderBtn.title = meta.hasWorkspace ? info.workspace : t("workspace.none");
        updateComposerPlaceholder();
      }
      if (info.version) settingsVersionValue.textContent = "v" + info.version;
      renderTabBar();
      return info;
    });
  }

  async function switchWorkspaceTo(rawPath) {
    // Captured up front, same reasoning as sendMessage(): this always
    // targets whichever tab was active when Open Folder/a History-drawer
    // item was clicked, even in the (rare -- requires clicking a
    // different tab in the brief window before this resolves) case where
    // the user switches tabs before this finishes.
    const sessionId = activeSessionId;
    const meta = sessionMetaById.get(sessionId);
    // A provider run in flight writes into whatever workspace was active
    // when it started, and its reply/status render into chatThread
    // whenever it resolves -- switching workspaces mid-run (Open Folder,
    // a History drawer item) would tear down and rebuild chatThread out
    // from under it via loadChatHistory() below, so the stale run's reply
    // ends up appended to the *new* project's thread once it finally
    // settles. sendMessage() already refuses a second concurrent /api/run
    // this way; workspace switches need the same guard.
    if (meta.runInFlight) {
      showToast(t("workspace.cannotSwitchWhileRunning"));
      return;
    }
    try {
      await postJSON("/api/open-folder", { path: rawPath }, sessionId);
    } catch (err) {
      if (sessionId === activeSessionId) showToast(t("workspace.openFailed", { msg: err.message }));
      return;
    }
    meta.attachments = [];
    if (sessionId === activeSessionId) renderAttachments();
    // Unlike boot(), nothing here depends on another's result -- none of
    // the three reads shared mutable state the others set first -- so they
    // can run concurrently instead of round-tripping to the server one at
    // a time. The latter two are skipped (not just guarded after the fact)
    // when this session is no longer active, since they'd only repaint
    // shared DOM that now belongs to a different tab.
    await Promise.all([
      refreshWorkspaceLabel(sessionId),
      sessionId === activeSessionId ? renderTree(treeEl, "") : Promise.resolve(),
      sessionId === activeSessionId ? loadChatHistory() : Promise.resolve(),
    ]);
  }

  function openFolderPrompt() {
    folderPromptInput.value = "";
    folderPromptOverlay.classList.add("show");
    folderPromptInput.focus();
  }

  function closeFolderPrompt() {
    folderPromptOverlay.classList.remove("show");
  }

  // Shared by the titlebar Open Folder button and the "폴더 직접 선택…"
  // button in the no-workspace card (SCR-05) -- exactly one code path
  // picks a folder, same invariant AppState/Api's docstring already
  // establishes for switching one.
  async function pickFolder() {
    if (window.__TAURI__ && window.__TAURI__.dialog) {
      // Tauri desktop shell: real OS folder picker via tauri-plugin-dialog
      // (window.__TAURI__ only exists because tauri.conf.json sets
      // app.withGlobalTauri -- this project has no frontend build step/
      // npm bundler, so the @tauri-apps/plugin-dialog JS package isn't an
      // option; the global-Tauri + capabilities/default.json's
      // "dialog:allow-open" grant is the no-build-tooling equivalent).
      let chosen;
      try {
        chosen = await window.__TAURI__.dialog.open({ directory: true, multiple: false });
      } catch (err) {
        showToast(t("workspace.pickFailed", { err }));
        return;
      }
      if (chosen) await switchWorkspaceTo(chosen);
      return;
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
      // Native app window (non-Tauri, pywebview fallback): real OS folder
      // picker via the pywebview JS bridge.
      let chosen;
      try {
        chosen = await window.pywebview.api.pick_folder();
      } catch (err) {
        showToast(t("workspace.pickFailed", { err }));
        return;
      }
      if (chosen) await switchWorkspaceTo(chosen);
      return;
    }
    // Plain browser tab: no native folder dialog available -- ask for a path.
    openFolderPrompt();
  }
  openFolderBtn.addEventListener("click", pickFolder);

  folderPromptCancel.addEventListener("click", closeFolderPrompt);
  folderPromptOverlay.addEventListener("click", (e) => {
    if (e.target === folderPromptOverlay) closeFolderPrompt();
  });
  folderPromptConfirm.addEventListener("click", async () => {
    const path = folderPromptInput.value.trim();
    if (!path) return;
    closeFolderPrompt();
    await switchWorkspaceTo(path);
  });
  folderPromptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") folderPromptConfirm.click();
    if (e.key === "Escape") closeFolderPrompt();
  });

  // ---------- history drawer (Phase 3, SCR-03, DEC-08~12) ----------

  function formatRelativeTime(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const diffMs = Date.now() - then;
    const minutes = Math.floor(diffMs / 60000);
    if (minutes < 1) return t("time.justNow");
    if (minutes < 60) return t("time.minutesAgo", { n: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t("time.hoursAgo", { n: hours });
    const days = Math.floor(hours / 24);
    if (days === 1) return t("time.yesterday");
    if (days < 7) return t("time.daysAgo", { n: days });
    return new Date(iso).toISOString().slice(0, 10);
  }

  function renderHistoryItem(group, turn) {
    const row = el("div", { class: "history-item" }, []);
    const top = el("div", { class: "hi-top" }, [el("span", { text: turn.provider || "-" }, [])]);
    if (turn.status) {
      top.appendChild(
        el("span", { class: `status-badge status-${turn.status}`, text: statusLabel(turn.status) }, [])
      );
    }
    top.appendChild(el("span", { class: "hi-time", text: formatRelativeTime(turn.ts) }, []));
    row.appendChild(top);
    row.appendChild(el("div", { class: "hi-task", text: turn.text || t("history.emptyMessage") }, []));
    row.addEventListener("click", () => {
      closeHistoryDrawer();
      if (!group.current) switchWorkspaceTo(group.path);
    });
    return row;
  }

  function renderHistoryGroups(groups) {
    historyList.innerHTML = "";
    if (!groups || groups.length === 0) {
      historyList.appendChild(el("div", { class: "hd-empty", text: t("history.empty") }, []));
      return;
    }
    for (const group of groups) {
      const header = el("div", { class: "hd-group" }, [
        el("span", { text: "📁" }, []),
        el("span", { text: group.name }, []),
      ]);
      if (group.current) header.appendChild(el("span", { class: "current-tag", text: t("history.current") }, []));
      header.appendChild(el("span", { class: "cnt", text: String(group.turns.length) }, []));
      historyList.appendChild(header);
      for (const turn of group.turns) {
        historyList.appendChild(renderHistoryItem(group, turn));
      }
    }
  }

  async function openHistoryDrawer() {
    historyScrim.classList.add("show");
    historyDrawer.classList.add("show");
    try {
      const data = await fetchJSON("/api/history");
      renderHistoryGroups(data.groups);
    } catch (err) {
      historyList.innerHTML = "";
      historyList.appendChild(el("div", { class: "hd-empty", text: t("history.loadError", { msg: err.message }) }, []));
    }
  }

  function closeHistoryDrawer() {
    historyScrim.classList.remove("show");
    historyDrawer.classList.remove("show");
  }

  historyBtn.addEventListener("click", openHistoryDrawer);
  historyCloseBtn.addEventListener("click", closeHistoryDrawer);
  historyScrim.addEventListener("click", closeHistoryDrawer);

  // ---------- provider connection panel (Phase 4, SCR-06/components.html §14) ----------

  const PROVIDER_LABEL = { codex: "Codex", claude: "Claude Code", agy: "Antigravity" };

  // Fills a <datalist> with a provider's known models (see
  // known_cli_models() server-side) so a model input can offer a pickable
  // list while still accepting free text for anything not listed --
  // replaces what used to be a plain text field with no protection against
  // a typo'd/invalid model id being saved and silently sent on every call.
  function renderModelDatalist(datalistEl, knownModels) {
    datalistEl.innerHTML = "";
    for (const m of knownModels || []) {
      datalistEl.appendChild(el("option", { value: m.id, label: m.label || m.id }, []));
    }
  }

  function renderProviderRow(info) {
    const row = el("div", { class: "pp-row" }, []);
    const badge = info.cli_detected
      ? el("span", { class: "status-badge status-success", text: t("provider.cliDetected") }, [])
      : el("span", { class: "status-badge status-fail", text: t("provider.cliNotFound") }, []);
    row.appendChild(
      el("div", { class: "pp-top" }, [
        el("span", { class: "pp-name", text: PROVIDER_LABEL[info.provider] || info.provider }, []),
        badge,
      ])
    );
    if (info.cli_detected) {
      // CLI가 감지된 provider는 기본 모델(--model 오버라이드)을 지정할
      // 수 있다 -- 키가 필요 없으므로(CLI가 자체적으로 인증을 처리) 검증
      // 호출 없이 바로 저장한다. 여기 저장한 값이 titlebar의
      // model-override-input 프리필 기본값이 되고, 매 전송 시 그 입력을
      // 비워두면 이 기본값이, 채워두면 그 값이 우선 적용된다.
      row.appendChild(el("div", { class: "pp-note", text: t("provider.cliModelNote") }, []));
      // "비움 = 자동"을 눈에 보이게: 고정된 값이 있으면 그 값을, 없으면 자동임을
      // 알려준다. 서버가 CLI 자신의 카탈로그에서 사라진 고정 모델을 찾으면
      // (model_stale) 경고와 함께 자동으로 되돌리는 버튼을 보여준다.
      row.appendChild(
        el("div", {
          class: "pp-note",
          text: info.cli_model ? t("provider.modelPinned", { model: info.cli_model }) : t("provider.modelAuto"),
        }, [])
      );
      const staleWarning = el("div", { class: "pp-note pp-warn", id: `cli-model-warn-${info.provider}` }, []);
      staleWarning.textContent = staleWarningText(info, PROVIDER_LABEL[info.provider], t);
      staleWarning.hidden = !staleWarning.textContent;
      row.appendChild(staleWarning);
      const cliModelDatalistId = `cli-model-list-${info.provider}`;
      const cliModelDatalist = el("datalist", { id: cliModelDatalistId }, []);
      renderModelDatalist(cliModelDatalist, info.known_models);
      const cliModelInput = el("input", {
        type: "text",
        placeholder: t("provider.modelPlaceholder"),
        "data-field": "model",
        list: cliModelDatalistId,
        value: info.cli_model || "",
      }, []);
      const cliModelSaveBtn = el("button", { type: "button", class: "primary", text: t("provider.save") }, []);
      cliModelSaveBtn.addEventListener("click", async () => {
        const model = cliModelInput.value.trim();
        try {
          await postJSON("/api/cli-model", { provider: info.provider, model });
          showToast(
            model
              ? t("provider.cliModelSaved", { provider: PROVIDER_LABEL[info.provider], model })
              : t("provider.cliModelCleared", { provider: PROVIDER_LABEL[info.provider] })
          );
          await refreshProviderPanel();
        } catch (err) {
          showToast(t("provider.saveFailed", { msg: err.message }));
        }
      });
      const keyRowChildren = [cliModelInput, cliModelSaveBtn];
      if (info.cli_model) {
        const autoBtn = el("button", { type: "button", text: t("provider.useAuto") }, []);
        autoBtn.addEventListener("click", async () => {
          try {
            await postJSON("/api/cli-model", { provider: info.provider, model: "" });
            showToast(t("provider.cliModelAutoSet", { provider: PROVIDER_LABEL[info.provider] }));
            await refreshProviderPanel();
          } catch (err) {
            showToast(t("provider.saveFailed", { msg: err.message }));
          }
        });
        keyRowChildren.push(autoBtn);
      }
      keyRowChildren.push(cliModelDatalist);
      row.appendChild(el("div", { class: "pp-key-row" }, keyRowChildren));
      return row;
    }
    row.appendChild(el("div", { class: "pp-note", text: t("provider.noCliInstallNote") }, []));
    return row;
  }

  // provider-select(작성 상자 상단)를 서버가 실제로 아는 provider 목록
  // (고정 3개 + auto)으로 다시 채운다 -- index.html은 "auto"만
  // 하드코딩해두고 나머지는 항상 이 함수가 채운다.
  //
  // provider별 cli_detected/cli_model을 여기 보관해두고, 옆의
  // model-override-input을 채우거나 숨기는 데 쓴다 -- "auto"는 provider
  // 하나로 확정되지 않으므로 이 입력창은 CLI로 감지된 고정 provider를
  // 선택했을 때만 노출한다.
  let providerCliInfoByName = {};

  // Repaints providerSelect/modelOverrideInput from the *active* session's
  // remembered choice (meta.provider/meta.model) -- called on tab switch
  // and after refreshProviderSelect() rebuilds the option list, so a
  // provider-list refresh (e.g. reopening Settings) never silently resets
  // what this tab had selected.
  // What the titlebar model input shows for a tab (pure; tested from
  // tests/test_webui_frontend_logic.py). An inherited value follows the
  // provider's *current* saved default, so "자동으로" or clearing the default
  // really stops sending the old model from every tab that never overrode it;
  // only a value the user typed in that tab is kept as it was.
  function resolveTitlebarModel(meta, defaultModel, show) {
    if (!show) return "";
    return meta.modelInherited === false ? meta.model || "" : defaultModel || "";
  }

  // The running bubble's text: what the CLI says it is doing right now (the
  // {kind, detail} from /api/run-progress; "working" until it says anything) and
  // how long the run has taken. Pure; tested.
  function runProgressText(progress, elapsedSeconds, translate) {
    const kinds = ["working", "starting", "thinking", "command", "read", "edit", "write", "search", "web", "tool", "writing"];
    const kind = progress && kinds.includes(progress.kind) ? progress.kind : "working";
    const detail = progress && typeof progress.detail === "string" ? progress.detail : "";
    const seconds = Math.max(0, Math.floor(elapsedSeconds) || 0);
    const elapsed =
      seconds >= 60
        ? translate("progress.elapsedMinutes", { m: Math.floor(seconds / 60), s: seconds % 60 })
        : translate("progress.elapsedSeconds", { s: seconds });
    return `${translate("progress." + kind)}${detail ? `: ${detail}` : ""} · ${elapsed}`;
  }

  // The warning shown when a pinned model is missing from the CLI's own
  // current list ("" = nothing to warn about). Pure; tested.
  function staleWarningText(info, providerLabel, translate) {
    if (!info || !info.model_stale || !info.cli_model) return "";
    return translate("provider.modelStale", { model: info.cli_model, provider: providerLabel });
  }

  // Updates only the warning elements of rows that are already on screen, so
  // a re-poll can surface a newly stale model without rebuilding the rows --
  // which would wipe what the user is typing in a model field. Touches
  // nothing but the warnings (getById is only asked for their ids). Pure
  // given its arguments; tested.
  function applyStaleWarnings(providers, getById, translate, labels) {
    for (const info of providers) {
      const warning = getById(`cli-model-warn-${info.provider}`);
      if (!warning) continue;
      const text = staleWarningText(info, labels[info.provider] || info.provider, translate);
      warning.textContent = text;
      warning.hidden = !text;
    }
  }

  // Whether /api/providers should be asked again because agy's model list is
  // still being fetched in the background (pure; tested). Bounded.
  function needsModelRepoll(providers, attempt, maxPolls) {
    return attempt < maxPolls && providers.some((info) => info.models_refreshing);
  }

  function restoreProviderSelectionForActiveSession() {
    const meta = activeMeta();
    if ([...providerSelect.options].some((opt) => opt.value === meta.provider)) {
      providerSelect.value = meta.provider;
    } else {
      // The remembered choice no longer exists -- fall back to whatever
      // the <select> defaulted to.
      meta.provider = providerSelect.value;
    }
    const info = providerCliInfoByName[providerSelect.value];
    const show = Boolean(info && info.cli_detected);
    modelOverrideInput.hidden = !show;
    renderModelDatalist(modelOverrideDatalist, show ? info.known_models : []);
    modelOverrideInput.value = resolveTitlebarModel(meta, info && info.cli_model, show);
    meta.model = modelOverrideInput.value;
  }

  function refreshProviderSelect(data) {
    providerSelect.innerHTML = "";
    providerSelect.appendChild(el("option", { value: "auto", text: "auto" }, []));
    providerCliInfoByName = {};
    for (const info of data.providers) {
      providerSelect.appendChild(el("option", { value: info.provider, text: info.provider }, []));
      providerCliInfoByName[info.provider] = info;
    }
    restoreProviderSelectionForActiveSession();
  }
  providerSelect.addEventListener("change", () => {
    const meta = activeMeta();
    meta.provider = providerSelect.value;
    const info = providerCliInfoByName[providerSelect.value];
    const show = Boolean(info && info.cli_detected);
    modelOverrideInput.hidden = !show;
    renderModelDatalist(modelOverrideDatalist, show ? info.known_models : []);
    // A provider *change* refills with the new provider's own saved
    // default, unlike restoring a tab (which keeps whatever was typed).
    modelOverrideInput.value = show ? info.cli_model || "" : "";
    meta.model = modelOverrideInput.value;
    meta.modelInherited = true;
  });
  modelOverrideInput.addEventListener("input", () => {
    const meta = activeMeta();
    meta.model = modelOverrideInput.value;
    meta.modelInherited = false;
  });

  // Monotonic token so an overlapping refresh (e.g. a "저장" click's own
  // await refreshProviderPanel() racing a fresh panel reopen) can't have
  // an earlier, slower response render its rows on top of a later one's
  // without re-clearing first -- only the response matching the most
  // recently issued request is ever allowed to touch the DOM.
  let providerPanelRequestId = 0;

  // agy's model list is fetched on a background thread server-side
  // (/api/providers answers at once with `models_refreshing: true`), so ask
  // again a few times until it settles: bounded, and it never rebuilds the
  // rows while the user is typing in one.
  const MODEL_REFRESH_POLL_MS = 2500;
  const MODEL_REFRESH_MAX_POLLS = 6;

  function updateModelDatalists(data) {
    for (const info of data.providers) {
      const list = document.getElementById(`cli-model-list-${info.provider}`);
      if (list) renderModelDatalist(list, info.known_models);
    }
    applyStaleWarnings(data.providers, (id) => document.getElementById(id), t, PROVIDER_LABEL);
  }

  async function refreshProviderPanel(attempt = 0) {
    const requestId = ++providerPanelRequestId;
    let data;
    let error;
    try {
      data = await fetchJSON("/api/providers");
    } catch (err) {
      error = err;
    }
    if (requestId !== providerPanelRequestId) return; // superseded by a newer refresh
    if (error) {
      providerPanelList.innerHTML = "";
      providerPanelList.appendChild(el("div", { class: "hd-empty", text: t("history.loadError", { msg: error.message }) }, []));
      return;
    }
    if (attempt > 0 && providerPanelList.contains(document.activeElement)) {
      // A re-poll must not wipe what the user is typing: refresh only the
      // suggestion lists, the stale warnings and the titlebar, not the rows.
      updateModelDatalists(data);
    } else {
      providerPanelList.innerHTML = "";
      for (const info of data.providers) {
        providerPanelList.appendChild(renderProviderRow(info));
      }
    }
    refreshProviderSelect(data);
    if (needsModelRepoll(data.providers, attempt, MODEL_REFRESH_MAX_POLLS)) {
      setTimeout(() => {
        // A newer refresh (a click, a reopened panel) owns the polling now.
        if (requestId === providerPanelRequestId) refreshProviderPanel(attempt + 1);
      }, MODEL_REFRESH_POLL_MS);
    }
  }

  // ---------- shared context (DEC-27) ----------
  // .handoff/shared-context.md -- free-form, per-workspace text folded
  // into every provider call (via handoff_bridge.py's build_prompt()).
  // No workspace yet: GET returns "" (not
  // an error), and the panel is still openable/editable, but saving
  // requires a workspace (same "no workspace selected" 400 every other
  // workspace-scoped POST already returns).

  async function loadSharedContext() {
    contextTextarea.value = t("settings.instructions.loading");
    contextTextarea.disabled = true;
    try {
      const data = await fetchJSON("/api/shared-context");
      contextTextarea.value = data.text;
    } catch (err) {
      contextTextarea.value = "";
      showToast(t("history.loadError", { msg: err.message }));
    } finally {
      contextTextarea.disabled = false;
    }
  }

  // ---------- settings panel ----------
  // Single entry point (titlebar "설정" button) for everything that used to
  // be two separate buttons (Diagnose/Context) plus two new items
  // (auto-fallback toggle, theme) -- consolidated so the titlebar itself
  // stays short.
  function openSettingsPanel() {
    settingsPanelOverlay.classList.add("show");
    refreshProviderPanel();
    loadSharedContext();
  }

  function closeSettingsPanel() {
    settingsPanelOverlay.classList.remove("show");
  }

  settingsBtn.addEventListener("click", openSettingsPanel);
  settingsPanelClose.addEventListener("click", closeSettingsPanel);
  settingsPanelOverlay.addEventListener("click", (event) => {
    if (event.target === settingsPanelOverlay) closeSettingsPanel();
  });
  contextPanelSave.addEventListener("click", async () => {
    try {
      await postJSON("/api/shared-context", { text: contextTextarea.value });
      showToast(t("settings.instructions.saved"));
    } catch (err) {
      showToast(t("provider.saveFailed", { msg: err.message }));
    }
  });

  // ---------- auto-fallback toggle ----------
  // Frontend-only preference (localStorage), threaded into POST /api/run's
  // body as `auto_fallback` -- webui_bridge_run.py only appends CLI mode's
  // `--auto-fallback` flag when it's true (default), so turning this off
  // makes a run try only the selected provider, matching what
  // remote_handoff_submit.py's own --no-auto-fallback already allows on
  // the CLI side.
  const AUTO_FALLBACK_KEY = "ahb-auto-fallback";

  function loadAutoFallbackPreference() {
    let saved;
    try {
      saved = localStorage.getItem(AUTO_FALLBACK_KEY);
    } catch (err) {
      saved = null;
    }
    autoFallbackToggle.checked = saved !== "off"; // default: on
  }

  function isAutoFallbackEnabled() {
    return autoFallbackToggle.checked;
  }

  autoFallbackToggle.addEventListener("change", () => {
    try {
      localStorage.setItem(AUTO_FALLBACK_KEY, autoFallbackToggle.checked ? "on" : "off");
    } catch (err) {
      // localStorage unavailable -- the toggle still works for this page
      // load, it just won't be remembered next launch.
    }
  });

  loadAutoFallbackPreference();

  // ---------- theme ----------
  // "system" (default) clears the override and lets app.css's
  // @media (prefers-color-scheme) block decide, matching the inline
  // pre-paint script in index.html's <head> (kept in sync deliberately:
  // that script only *applies* a saved choice, this is the only place
  // that *writes* one).
  const THEME_KEY = "ahb-theme";

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function loadThemePreference() {
    let saved;
    try {
      saved = localStorage.getItem(THEME_KEY);
    } catch (err) {
      saved = null;
    }
    const theme = saved === "light" || saved === "dark" ? saved : "system";
    themeSelect.value = theme;
    applyTheme(theme); // index.html's inline script already did this before first paint; re-applying here is a cheap no-op, not a fix for anything
  }

  themeSelect.addEventListener("change", () => {
    const theme = themeSelect.value;
    applyTheme(theme);
    try {
      if (theme === "system") {
        localStorage.removeItem(THEME_KEY);
      } else {
        localStorage.setItem(THEME_KEY, theme);
      }
    } catch (err) {
      // localStorage unavailable -- the choice still applies for this page
      // load, it just won't be remembered next launch.
    }
  });

  loadThemePreference();

  // ---------- language ----------
  // Unlike theme, there is no pre-paint script for this (see i18n.js's own
  // header for why a brief text flash is an acceptable tradeoff a color
  // flash isn't). AHB_I18N.getLanguage()/setLanguage() own the actual
  // localStorage read/write; this only owns the <select> and re-rendering
  // whatever's currently on screen after a change.
  function applyLanguageChangeToVisibleContent() {
    AHB_I18N.applyI18n();
    updateComposerPlaceholder();
    renderTabBar();
    if (!activeMeta().hasWorkspace) {
      // The only thing chatThread can be showing while hasWorkspace is
      // false is showNoWorkspaceState()'s own card -- safe to
      // unconditionally re-render it in the new language.
      showNoWorkspaceState();
    }
    if (settingsPanelOverlay.classList.contains("show")) {
      // Re-fetches and re-renders the provider rows with
      // the new language's t() calls -- cheap, and the panel being open
      // means the user is looking at exactly this content right now.
      refreshProviderPanel();
    }
    // Deliberately not handled: an already-rendered chat-empty placeholder
    // (no messages yet, but a real workspace open) and an already-open
    // history drawer both keep their old-language text until the next
    // time they're actually re-rendered (switching workspace, reopening
    // history, sending the first message) -- a narrow, low-stakes staleness
    // window rather than engineering a full re-render for every possible
    // visible state.
  }

  languageSelect.value = AHB_I18N.getLanguage();
  languageSelect.addEventListener("change", () => {
    AHB_I18N.setLanguage(languageSelect.value);
    applyLanguageChangeToVisibleContent();
  });

  // ---------- file tree ----------

  function iconFor(entry) {
    return entry.type === "dir" ? "📁" : "📄";
  }

  async function renderTree(container, relPath) {
    container.innerHTML = "";
    let entries;
    try {
      const data = await fetchJSON(`/api/tree?path=${encodeURIComponent(relPath)}`);
      entries = data.entries;
    } catch (err) {
      container.appendChild(el("div", { class: "tree-error", text: t("tree.loadError", { msg: err.message }) }, []));
      return;
    }
    if (entries.length === 0) {
      container.appendChild(el("div", { class: "tree-empty", text: t("tree.empty") }, []));
      return;
    }
    for (const entry of entries) {
      container.appendChild(renderEntry(entry));
    }
  }

  function renderEntry(entry) {
    const row = el(
      "div",
      { class: "tree-row" },
      [
        el("span", { class: "icon", text: iconFor(entry) }, []),
        el("span", { class: "name", text: entry.name }, []),
      ]
    );

    if (entry.type === "dir") {
      let expanded = false;
      let childContainer = null;
      row.addEventListener("click", async () => {
        expanded = !expanded;
        row.querySelector(".icon").textContent = expanded ? "📂" : "📁";
        if (expanded) {
          childContainer = el("div", { class: "tree-children" }, []);
          row.after(childContainer);
          await renderTree(childContainer, entry.path);
        } else if (childContainer) {
          childContainer.remove();
          childContainer = null;
        }
      });
    } else {
      row.addEventListener("click", () => attachWorkspaceFile(entry));
      row.title = t("tree.clickToAttach");
      // Separate from the row's own click-to-attach (kept as-is, already
      // relied on) -- a dedicated button so a user can read a file without
      // also adding it to the next message's context.
      const viewBtn = el("button", { type: "button", class: "tree-view-btn", text: "👁", "data-i18n-title": "tree.view.title", title: t("tree.view.title") }, []);
      viewBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        openFilePreview(entry);
      });
      row.appendChild(viewBtn);
    }
    return row;
  }

  async function attachWorkspaceFile(entry) {
    try {
      const preview = await fetchJSON(`/api/file?path=${encodeURIComponent(entry.path)}`);
      addAttachment({
        name: preview.name,
        path: preview.path,
        content: preview.content.slice(0, MAX_PREVIEW_CHARS),
        truncated: preview.truncated || preview.content.length > MAX_PREVIEW_CHARS,
      });
    } catch (err) {
      // Binary or unreadable: still attach as a reference-only chip.
      addAttachment({ name: entry.name, path: entry.path, content: null, truncated: false });
      showToast(t("tree.attachedWithoutPreview", { msg: err.message }));
    }
  }

  // ---------- file preview (read-only, no attach) ----------

  async function openFilePreview(entry) {
    filePreviewName.textContent = entry.name;
    filePreviewNote.hidden = true;
    filePreviewContent.textContent = t("filePreview.loading");
    filePreviewOverlay.classList.add("show");
    try {
      const preview = await fetchJSON(`/api/file?path=${encodeURIComponent(entry.path)}`);
      filePreviewContent.textContent = preview.content;
      if (preview.truncated) {
        filePreviewNote.hidden = false;
        filePreviewNote.textContent = t("filePreview.truncatedNote");
      }
    } catch (err) {
      filePreviewContent.textContent = "";
      filePreviewNote.hidden = false;
      filePreviewNote.textContent = t("filePreview.loadError", { msg: err.message });
    }
  }

  function closeFilePreview() {
    filePreviewOverlay.classList.remove("show");
  }

  filePreviewClose.addEventListener("click", closeFilePreview);
  filePreviewOverlay.addEventListener("click", (event) => {
    if (event.target === filePreviewOverlay) closeFilePreview();
  });

  // ---------- attachments ----------

  function addAttachment(attachment) {
    const attachments = activeMeta().attachments;
    if (attachments.some((a) => a.path && a.path === attachment.path)) return;
    attachments.push(attachment);
    renderAttachments();
  }

  function removeAttachment(index) {
    activeMeta().attachments.splice(index, 1);
    renderAttachments();
  }

  function renderAttachments() {
    composerAttachments.innerHTML = "";
    activeMeta().attachments.forEach((a, index) => {
      const chip = el("span", { class: "chip" }, [
        el("span", { text: a.path ? "📄" : "📎" }, []),
        el("span", { text: a.name }, []),
      ]);
      const closeBtn = el("button", { type: "button", text: "✕" }, []);
      closeBtn.addEventListener("click", () => removeAttachment(index));
      chip.appendChild(closeBtn);
      composerAttachments.appendChild(chip);
    });
    updateSendState();
  }

  function updateSendState() {
    const hasContent = composerInput.value.trim().length > 0 || activeMeta().attachments.length > 0;
    sendBtn.disabled = activeMeta().runInFlight || !hasContent;
  }

  // ---------- drag & drop (files dragged in from the OS) ----------

  const dropTarget = document.querySelector(".chat-col");

  dropTarget.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth += 1;
    dropzone.classList.add("active");
  });
  dropTarget.addEventListener("dragover", (e) => e.preventDefault());
  dropTarget.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropzone.classList.remove("active");
  });
  dropTarget.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove("active");
    handleDroppedFiles(e.dataTransfer.files);
  });

  function handleDroppedFiles(fileList) {
    for (const file of Array.from(fileList)) {
      readDroppedFile(file);
    }
  }

  function readDroppedFile(file) {
    const looksTextual = !file.type || file.type.startsWith("text/") || /\.(md|txt|py|js|json|ya?ml|css|html?)$/i.test(file.name);
    if (!looksTextual || file.size > MAX_PREVIEW_CHARS) {
      addAttachment({ name: file.name, path: null, content: null, truncated: false });
      return;
    }
    const reader = new FileReader();
    reader.onload = () => addAttachment({ name: file.name, path: null, content: String(reader.result), truncated: false });
    reader.onerror = () => addAttachment({ name: file.name, path: null, content: null, truncated: false });
    reader.readAsText(file);
  }

  // ---------- attach button / hidden file input (dialog fallback for drag&drop) ----------

  attachBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    handleDroppedFiles(fileInput.files);
    fileInput.value = "";
  });

  // ---------- chat thread rendering (shared by history load + live send) ----------

  function clearChatEmptyState() {
    // Also strips the no-workspace card (SCR-05): once a real message
    // renders, that placeholder is stale regardless of which one was
    // showing.
    const placeholder = chatThread.querySelector(".chat-empty, .no-workspace-card");
    if (placeholder) placeholder.remove();
  }

  function showChatEmptyState() {
    chatThread.innerHTML = "";
    chatThread.appendChild(
      el("div", { class: "chat-empty" }, [
        document.createTextNode(t("chat.empty.line1")),
        el("br", {}, []),
        document.createTextNode(t("chat.empty.line2")),
      ])
    );
  }

  // SCR-05 (docs/design-system/wireframes.html#s7): AppState.workspace is
  // None. DEC-05 -- "새 폴더 자동 생성" doesn't create anything itself, it
  // just nudges focus to the composer, since the actual creation is
  // deferred to whichever message gets sent first regardless of which
  // button (if either) was clicked.
  function showNoWorkspaceState() {
    chatThread.innerHTML = "";
    const autoBtn = el("button", { type: "button", class: "primary", text: t("noWorkspace.autoCreate") }, []);
    autoBtn.addEventListener("click", () => composerInput.focus());
    const pickBtn = el("button", { type: "button", text: t("noWorkspace.pickFolder") }, []);
    pickBtn.addEventListener("click", pickFolder);
    chatThread.appendChild(
      el("div", { class: "no-workspace-card" }, [
        el("div", { class: "nw-icon", text: "📂" }, []),
        el("div", { class: "nw-title", text: t("noWorkspace.title") }, []),
        el("div", { class: "nw-note", text: t("noWorkspace.note") }, []),
        el("div", { class: "nw-actions" }, [autoBtn, pickBtn]),
        el("div", { class: "nw-path" }, [
          document.createTextNode(t("noWorkspace.autoCreateLocation")),
          el("span", { class: "mono", text: "~/Documents/Agent Handoff Bridge/" }, []),
        ]),
      ])
    );
  }

  // DEC-03: fenced ```code``` blocks render as monospace blocks; everything
  // else is plain text. Both paths use textContent/createTextNode only --
  // never innerHTML -- because message text can come from a provider's
  // response, which this app doesn't fully control or trust.
  function renderTextWithCodeBlocks(container, text) {
    const parts = String(text).split(/```[^\n]*\n?([\s\S]*?)```/g);
    parts.forEach((part, i) => {
      if (!part) return;
      if (i % 2 === 0) {
        container.appendChild(document.createTextNode(part));
      } else {
        const pre = el("pre", { class: "code-block" }, []);
        const code = el("code", { text: part.replace(/\n$/, "") }, []);
        pre.appendChild(code);
        container.appendChild(pre);
      }
    });
  }

  function renderMessage(message) {
    clearChatEmptyState();
    const bubble = el("div", { class: "bubble" }, []);
    if (message.text) renderTextWithCodeBlocks(bubble, message.text);

    if (message.attachments && message.attachments.length > 0) {
      const attachRow = el("div", { class: "attachments" }, []);
      for (const a of message.attachments) {
        attachRow.appendChild(el("span", { class: "chip", text: `${a.path ? "📄" : "📎"} ${a.name}` }, []));
      }
      bubble.appendChild(attachRow);
    }

    let roleClass = "system";
    let avatar = "🗂️";
    const metaParts = [];
    if (message.role === "user") {
      roleClass = "user";
      avatar = "🧑";
      metaParts.push(document.createTextNode(t("msg.you")));
    } else if (message.role === "agent") {
      roleClass = "agent";
      avatar = "🤖";
      metaParts.push(document.createTextNode(message.provider || "agent"));
      if (message.status) {
        metaParts.push(
          el("span", { class: `status-badge status-${message.status}` }, [
            document.createTextNode(`${STATUS_ICON[message.status] || ""} ${statusLabel(message.status)}`),
          ])
        );
      }
    } else {
      metaParts.push(document.createTextNode(t("msg.system")));
    }

    const msg = el("div", { class: `msg ${roleClass}` }, [
      el("div", { class: "avatar", text: avatar }, []),
      el("div", {}, [el("div", { class: "meta" }, metaParts), bubble]),
    ]);
    chatThread.appendChild(msg);
    chatThread.scrollTop = chatThread.scrollHeight;
    return msg;
  }

  function renderBusyMessage(providerLabel) {
    clearChatEmptyState();
    const msg = el("div", { class: "msg agent busy" }, [
      el("div", { class: "avatar", text: "🤖" }, []),
      el("div", {}, [
        el("div", { class: "meta" }, [document.createTextNode(providerLabel)]),
        el("div", { class: "bubble" }, [
          el("span", { class: "spinner" }, []),
          el("span", { class: "progress-text", text: t("msg.running") }, []),
        ]),
      ]),
    ]);
    chatThread.appendChild(msg);
    chatThread.scrollTop = chatThread.scrollHeight;
    return msg;
  }

  // While a run is in flight, ask the server once a second what the CLI is doing
  // and show it in the busy bubble (the terminal's "Running command ..." line).
  // A failed or slow poll just leaves the last text; returns the stop function.
  function startRunProgress(busyMsg, sessionId) {
    const textEl = busyMsg.querySelector(".progress-text");
    if (!textEl) return () => {};
    const startedAt = Date.now();
    let latest = null;
    let polling = false;
    let stopped = false;
    const render = () => {
      if (!stopped) textEl.textContent = runProgressText(latest, (Date.now() - startedAt) / 1000, t);
    };
    const poll = async () => {
      if (polling || stopped) return;
      polling = true;
      try {
        const data = await fetchJSON("/api/run-progress", sessionId);
        if (data && data.running) latest = data;
      } catch (err) {
        // Progress is a nicety; the run itself reports its own failures.
      } finally {
        polling = false;
      }
    };
    render();
    const timer = setInterval(() => {
      poll();
      render();
    }, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }

  async function loadChatHistory() {
    // Guards against a rapid tab switch landing while this fetch is still
    // in flight -- without this, a slow response for a tab the user
    // already switched away from could still overwrite the *new* active
    // tab's chatThread with the wrong session's messages.
    const sessionId = activeSessionId;
    let data;
    try {
      data = await fetchJSON("/api/chat", sessionId);
    } catch (err) {
      if (sessionId !== activeSessionId) return;
      showToast(t("msg.historyLoadFailed", { msg: err.message }));
      showChatEmptyState();
      return;
    }
    if (sessionId !== activeSessionId) return;
    if (!data.messages || data.messages.length === 0) {
      showChatEmptyState();
      return;
    }
    chatThread.innerHTML = "";
    for (const message of data.messages) renderMessage(message);
  }

  // ---------- composer ----------

  composerInput.addEventListener("input", () => {
    composerInput.style.height = "auto";
    composerInput.style.height = `${Math.min(composerInput.scrollHeight, 120)}px`;
    updateSendState();
  });
  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  sendBtn.addEventListener("click", sendMessage);

  async function sendMessage() {
    // Captured once, up front: this send belongs to whichever tab is
    // active *right now*, for its entire lifetime, even if the user
    // switches to a different tab before it resolves (a run can take
    // minutes). Every DOM touch below is guarded with
    // `sessionId === activeSessionId` for exactly that reason -- without
    // it, switching away mid-run would make this function's eventual
    // result (or busy indicator) render into whatever tab happens to be
    // active *later*, not the one that actually started it.
    const sessionId = activeSessionId;
    const meta = sessionMetaById.get(sessionId);

    // Re-entry guard: sendBtn.disabled alone doesn't stop the Enter-key
    // path, which never checks it. Without this, typing a follow-up and
    // hitting Enter while the first reply is still pending fires a second
    // concurrent /api/run for this session.
    if (meta.runInFlight) return;

    const text = composerInput.value.trim();
    if (!text && meta.attachments.length === 0) return;

    // DEC-02: confirm once per page load before the first provider call,
    // then trust the user from then on -- deliberately not per-tab, see
    // sessionRunConfirmed's own comment. window.confirm() is blocking/
    // synchronous by design here -- we want the user's answer before any
    // token-spending call goes out, not a fire-and-forget toast.
    if (!sessionRunConfirmed) {
      const ok = window.confirm(t("send.confirm"));
      if (!ok) return;
      sessionRunConfirmed = true;
    }

    const provider = providerSelect.value;
    // Only meaningful while visible (a CLI-detected fixed provider is
    // selected, see restoreProviderSelectionForActiveSession()) -- hidden
    // for "auto", so this is deliberately null in every other case rather than sending a stale leftover value.
    const model = (!modelOverrideInput.hidden && modelOverrideInput.value.trim()) || null;
    const userMessage = { role: "user", text, attachments: meta.attachments };
    // Still synchronously the active session at this point (nothing has
    // awaited yet) -- safe to touch the DOM unconditionally here.
    renderMessage(userMessage);

    composerInput.value = "";
    composerInput.style.height = "auto";
    meta.attachments = [];
    renderAttachments();
    updateSendState();

    const workspaceWasMissing = !meta.hasWorkspace;
    try {
      await postJSON("/api/chat", userMessage, sessionId);
    } catch (err) {
      // Stop here unconditionally -- not just for the auto-create case.
      // Calling /api/run right after a failed/rejected /api/chat means
      // asking the provider to answer a message that was never actually
      // recorded: at best it's a wasted round trip (a 409 here means a
      // run is already in progress, so /api/run would immediately 409
      // too -- a second, more confusing error on top of this one); at
      // worst the agent's reply renders and persists with no
      // corresponding user turn backing it, which pair_messages_into_turns()
      // (Phase 3) can't attribute to anything in the history drawer.
      if (sessionId === activeSessionId) showToast(t("send.chatSaveFailed", { msg: err.message }));
      return;
    }
    if (workspaceWasMissing) {
      // SCR-05: bring the titlebar and file tree up to date with the
      // workspace /api/chat just auto-created, before the provider call
      // that's about to follow.
      try {
        await refreshWorkspaceLabel(sessionId);
        if (sessionId === activeSessionId) await renderTree(treeEl, "");
      } catch (err) {
        if (sessionId === activeSessionId) showToast(t("send.workspaceRefreshFailed", { msg: err.message }));
      }
    }

    const busyMsg = sessionId === activeSessionId ? renderBusyMessage(provider) : null;
    meta.runInFlight = true;
    if (sessionId === activeSessionId) {
      composerInput.disabled = true;
      updateSendState();
    }
    renderTabBar();
    const stopRunProgress = busyMsg ? startRunProgress(busyMsg, sessionId) : () => {};
    try {
      const result = await postJSON(
        "/api/run",
        { provider, model, text, attachments: userMessage.attachments, auto_fallback: isAutoFallbackEnabled() },
        sessionId
      );
      if (sessionId === activeSessionId) {
        if (busyMsg) busyMsg.remove();
        for (const agentMessage of result.messages) renderMessage(agentMessage);
      } else {
        // The reply is already persisted server-side -- this tab's next
        // loadChatHistory() (whenever the user switches back to it) will
        // show it. The badge is the only thing needed here.
        meta.hasUnseenReply = true;
      }
    } catch (err) {
      if (sessionId === activeSessionId) {
        if (busyMsg) busyMsg.remove();
        renderMessage({ role: "system", text: t("send.runFailedSystemMsg", { msg: err.message }), attachments: [] });
        showToast(t("send.runFailedToast", { msg: err.message }));
      } else {
        meta.hasUnseenReply = true;
      }
    } finally {
      stopRunProgress();
      meta.runInFlight = false;
      if (sessionId === activeSessionId) {
        composerInput.disabled = false;
        updateSendState();
      }
      renderTabBar();
    }
  }

  // ---------- update check (Phase 6, SCR-07/components.html §15) ----------

  // "pending" | "available" | "current" | "unavailable" -- "pending" is
  // local to this file (GET /api/update-check hasn't reported checked:
  // true yet); the other three mirror check_for_update()'s `status`
  // field (handoff_bridge.py, CFL-18) once it has. Starts "pending", set
  // from a real GET /api/update-check response only once one actually
  // reports checked: true (see checkForUpdate() below). CFL-18:
  // "current" (genuinely checked, nothing newer) and
  // "unavailable" (couldn't check at all -- gh missing/unauthenticated/
  // offline, real DEC-19-documented failure paths) used to be
  // indistinguishable and both showed "최신 버전을 사용 중입니다," which
  // is simply false for the "unavailable" case.
  let latestUpdateStatus = "pending";
  let latestUpdateInfo = null; // only meaningful when status === "available"

  function openUpdatePopover() {
    if (latestUpdateStatus === "pending") {
      showToast(t("update.checking"));
      return;
    }
    if (latestUpdateStatus === "unavailable") {
      showToast(t("update.cannotCheck"));
      return;
    }
    if (latestUpdateStatus === "current") {
      // components.html §15: the button/icon is always visible ("평소엔
      // 아이콘만"), only the dot is conditional -- reuse the existing
      // toast mechanism for the "you're already current" case instead of
      // inventing a second popover layout the wireframe never mocked.
      showToast(t("update.upToDate"));
      return;
    }
    // "available"
    updatePopoverVersion.textContent = t("update.availableVersion", { version: latestUpdateInfo.latest_version });
    updatePopoverNote.textContent = t("update.currentVersionNote", { version: latestUpdateInfo.current_version });
    updatePopover.classList.add("show");
  }

  function closeUpdatePopover() {
    updatePopover.classList.remove("show");
  }

  // The real `gh` call server-side is network I/O (handoff_bridge.py's
  // short_run() gives it up to 10s before giving up) -- the page's first
  // GET /api/update-check can easily arrive before that background check
  // finishes, especially right after server startup. Poll while
  // `checked` is false instead of asking exactly once (review fix: a
  // real race, not a hypothetical -- a single-shot check would silently
  // and permanently miss the badge whenever the page loaded faster than
  // the network call). Bounded so a hung/never-finishing check (gh
  // installed but stuck, unusual network conditions) doesn't poll
  // forever -- comfortably past the 10s worst case, then give up
  // quietly like every other failure mode this feature already treats
  // as "can't tell right now."
  const UPDATE_CHECK_POLL_INTERVAL_MS = 1500;
  const UPDATE_CHECK_MAX_POLLS = 10;

  function scheduleUpdateCheckRetry(attempt) {
    // attempt is 0-indexed (the first fetch already happened before this
    // is ever called), so this caps the total fetch count at
    // UPDATE_CHECK_MAX_POLLS, not UPDATE_CHECK_MAX_POLLS + 1 -- a review
    // found the original `attempt < UPDATE_CHECK_MAX_POLLS` check let one
    // extra fetch through (attempts 0..10 = 11 calls for a "10" bound).
    if (attempt + 1 < UPDATE_CHECK_MAX_POLLS) {
      window.setTimeout(() => checkForUpdate(attempt + 1), UPDATE_CHECK_POLL_INTERVAL_MS);
    }
    // Otherwise: polling exhausted with no confirmed answer --
    // latestUpdateStatus deliberately stays "pending" rather than
    // falling through to "current" or "unavailable," since neither was
    // actually confirmed.
  }

  async function checkForUpdate(attempt = 0) {
    let data;
    try {
      data = await fetchJSON("/api/update-check");
    } catch {
      // A transient fetch failure (e.g. the server briefly not accepting
      // connections in the instant right after startup) must not
      // permanently give up on the whole check -- that would undermine
      // the entire point of polling (review fix: the original version
      // only retried on "not checked yet" and treated any fetch
      // exception as final, even a one-off blip on the very first
      // attempt). Retried the same bounded way as an unfinished check.
      scheduleUpdateCheckRetry(attempt);
      return;
    }
    if (!data.checked) {
      scheduleUpdateCheckRetry(attempt);
      return;
    }
    latestUpdateStatus = data.status;
    if (data.status === "available") {
      latestUpdateInfo = data;
      updateDot.classList.add("show");
    }
  }

  updateBtn.addEventListener("click", openUpdatePopover);
  updateLaterBtn.addEventListener("click", closeUpdatePopover);
  updateReleaseNotesBtn.addEventListener("click", () => {
    if (latestUpdateInfo && latestUpdateInfo.url) openExternal(latestUpdateInfo.url);
    closeUpdatePopover();
  });
  document.addEventListener("click", (event) => {
    if (updatePopover.classList.contains("show") && !updatePopover.contains(event.target) && event.target !== updateBtn && !updateBtn.contains(event.target)) {
      closeUpdatePopover();
    }
  });

  // ---------- multi-session tab bar (M2, docs/research-session-splitting.md) ----------

  function renderTabBar() {
    tabBarEl.innerHTML = "";
    for (const [sessionId, meta] of sessionMetaById) {
      const nameText = meta.hasWorkspace ? meta.workspaceName : t("workspace.none");
      const tab = el("div", { class: "tab" + (sessionId === activeSessionId ? " active" : ""), title: nameText }, [
        el("span", { class: "tab-name", text: nameText }, []),
      ]);
      if (meta.runInFlight) {
        tab.appendChild(el("span", { class: "tab-busy", text: "⏳" }, []));
      } else if (meta.hasUnseenReply) {
        tab.appendChild(el("span", { class: "tab-badge" }, []));
      }
      if (sessionId !== DEFAULT_SESSION_ID) {
        // The default session can never be closed (server-enforced too,
        // see handoff_webui.py's do_DELETE) -- no close button for it.
        const closeBtn = el("button", { type: "button", class: "tab-close", text: "✕" }, []);
        closeBtn.title = t("session.close.title");
        closeBtn.addEventListener("click", (event) => {
          event.stopPropagation(); // don't also trigger the tab's own click-to-switch
          closeSession(sessionId);
        });
        tab.appendChild(closeBtn);
      }
      tab.addEventListener("click", () => switchToSession(sessionId));
      tabBarEl.appendChild(tab);
    }
    const newTabBtn = el("button", { type: "button", class: "tab-new", text: "+" }, []);
    newTabBtn.title = t("session.new.title");
    newTabBtn.addEventListener("click", createNewSession);
    tabBarEl.appendChild(newTabBtn);
  }

  async function switchToSession(sessionId) {
    if (sessionId === activeSessionId) return;
    const meta = sessionMetaById.get(sessionId);
    if (!meta) return; // stale click on a tab that got closed in the meantime

    // Save the outgoing tab's live draft state into its own meta before
    // switching away -- nothing typed or attached there is lost.
    activeMeta().composerDraft = composerInput.value;

    activeSessionId = sessionId;
    meta.hasUnseenReply = false;

    composerInput.value = meta.composerDraft || "";
    composerInput.style.height = "auto";
    renderAttachments();
    updateComposerPlaceholder();
    updateSendState();
    restoreProviderSelectionForActiveSession();
    renderTabBar();

    try {
      const info = await refreshWorkspaceLabel(sessionId);
      if (sessionId !== activeSessionId) return; // switched again before this resolved
      if (info.workspace === null) {
        showNoWorkspaceState();
        return;
      }
      await renderTree(treeEl, "");
      await loadChatHistory();
    } catch (err) {
      if (sessionId === activeSessionId) showToast(t("session.switchFailed", { msg: err.message }));
    }
  }

  async function createNewSession() {
    let created;
    try {
      created = await postJSON("/api/sessions", {});
    } catch (err) {
      showToast(t("session.createFailed", { msg: err.message }));
      return;
    }
    sessionMetaById.set(created.session_id, freshSessionMeta());
    renderTabBar();
    await switchToSession(created.session_id);
  }

  async function closeSession(sessionId) {
    if (sessionId === DEFAULT_SESSION_ID) return; // no close button renders for it; a defensive no-op anyway
    const meta = sessionMetaById.get(sessionId);
    if (meta && meta.runInFlight) {
      // The server would reject this close anyway (a run in flight there
      // is still writing into that session's workspace/chat log) -- check
      // client-side first so closing a busy tab doesn't first navigate
      // away from it only to then report the failure.
      showToast(t("session.closeRunInFlight"));
      return;
    }
    if (sessionId === activeSessionId) {
      // Never leave the UI pointed at a session that's about to stop
      // existing -- switch away first. DEFAULT_SESSION_ID is always
      // present (never itself closeable), so this always has somewhere
      // to go.
      await switchToSession(DEFAULT_SESSION_ID);
    }
    try {
      await deleteJSON(`/api/sessions/${sessionId}`);
    } catch (err) {
      showToast(t("session.closeFailed", { msg: err.message }));
      return;
    }
    sessionMetaById.delete(sessionId);
    renderTabBar();
  }

  // ---------- boot ----------

  async function boot() {
    // Translates all of index.html's static markup (data-i18n/-title/
    // -placeholder/-html attributes) as the very first thing boot() does --
    // before any network round trip, so the chrome renders in the right
    // language even if /api/info or the others are slow.
    AHB_I18N.applyI18n();
    // Fire-and-forget, not awaited: the update check is independent of
    // workspace state (SCR-07 runs it "앱 시작 시" regardless of whether a
    // workspace is even open yet) and must never delay the rest of boot
    // on a slow/offline `gh` call -- though in practice the server-side
    // check already ran in main()'s background thread before this page
    // even loaded, so this fetch is normally near-instant either way.
    checkForUpdate();
    // Same reasoning, also independent of workspace state (provider
    // detection is app-level, not per-workspace): populates the
    // composer's provider-select dropdown (index.html only hardcodes
    // "auto") with the real provider list.
    // Through refreshProviderPanel (not a bare fetch) so the boot load also
    // re-asks while agy's list is still being fetched in the background.
    refreshProviderPanel();

    // M2: restore any extra tabs left open across a restart -- the default
    // session/tab already exists (sessionMetaById's own initialization),
    // this only adds the others. Each restored tab's real name is filled
    // in via refreshWorkspaceLabel() (matching GET /api/info's own name
    // computation, not reimplemented client-side from the raw path) --
    // fire-and-forget per tab so this never blocks the active tab's own
    // boot sequence below on N extra round trips.
    try {
      const data = await fetchJSON("/api/sessions");
      for (const entry of data.sessions) {
        if (entry.session_id === DEFAULT_SESSION_ID) continue;
        sessionMetaById.set(entry.session_id, freshSessionMeta());
        refreshWorkspaceLabel(entry.session_id).catch(() => {});
      }
    } catch {
      // Couldn't list sessions (server not ready yet, etc.) -- proceed
      // with just the default tab, same as every version of this file
      // before M2 existed.
    }
    renderTabBar();

    let info;
    try {
      info = await refreshWorkspaceLabel();
    } catch {
      workspaceLabel.textContent = "workspace";
      return;
    }
    if (info.workspace === null) {
      // SCR-05: nothing to browse or load yet -- the tree/chat GET
      // endpoints would just return empty results anyway, so skip them.
      showNoWorkspaceState();
      return;
    }
    await renderTree(treeEl, "");
    await loadChatHistory();
  }
  boot();
})();
