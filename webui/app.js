// Agent Handoff Bridge -- MVP web UI client.
// File browsing + attaching + a VS Code-style "Open Folder" switch, with a
// local per-workspace chat draft history. "Send" persists the message to
// <workspace>/.handoff/webui/chat/ *and* actually calls Codex/Claude via
// POST /api/run (Phase 1) -- attachments are folded into that prompt, not
// just the chat log. See webui/index.html's composer-note and
// docs/provider-extensibility.md for what's intentionally not wired up yet.
(function () {
  "use strict";

  const MAX_PREVIEW_CHARS = 20000;

  const treeEl = document.getElementById("tree");
  const workspaceLabel = document.getElementById("workspace-label");
  const openFolderBtn = document.getElementById("open-folder-btn");
  const providerSelect = document.getElementById("provider-select");
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
  const diagnoseBtn = document.getElementById("diagnose-btn");
  const providerPanelOverlay = document.getElementById("provider-panel-overlay");
  const providerPanelList = document.getElementById("provider-panel-list");
  const providerPanelClose = document.getElementById("provider-panel-close");

  /** @type {{name: string, path: string|null, content: string|null, truncated: boolean}[]} */
  let attachments = [];
  let dragDepth = 0;
  // DEC-02: only the first send *in this browser session* confirms that
  // tokens may be spent; every send after that in the same session runs
  // immediately. Resets on page reload -- intentionally not persisted.
  let sessionRunConfirmed = false;
  // Guards against a second concurrent /api/run: sendBtn.disabled alone
  // doesn't stop the Enter-key send path below, and updateSendState() (run
  // on every keystroke) would otherwise re-enable sendBtn if the user
  // types while a run is still in flight -- a real race that could
  // duplicate an already-persisted agent message (server-side backstop:
  // handoff_webui.RunAlreadyInProgressError).
  let runInFlight = false;
  // Phase 2 (SCR-05, DEC-04~07): AppState.workspace can be null server-side
  // until the first message auto-creates one. Mirrors that so the UI knows
  // whether to show the "no workspace" card and which composer placeholder
  // to use, without re-fetching /api/info on every keystroke.
  let hasWorkspace = true;

  const STATUS_LABEL = { success: "완료", handoff: "핸드오프 필요", fail: "실패" };
  const STATUS_ICON = { success: "✅", handoff: "🔀", fail: "⚠️" };

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast._t);
    showToast._t = window.setTimeout(() => toast.classList.remove("show"), 3200);
  }

  async function fetchJSON(url) {
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `request failed: ${res.status}`);
    }
    return data;
  }

  async function postJSON(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
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
    composerInput.placeholder = hasWorkspace
      ? "메시지를 입력하세요…"
      : "메시지를 입력하면 자동으로 폴더가 만들어집니다…";
  }

  function refreshWorkspaceLabel() {
    return fetchJSON("/api/info").then((info) => {
      hasWorkspace = info.workspace !== null;
      workspaceLabel.textContent = hasWorkspace ? info.name : "워크스페이스 없음";
      openFolderBtn.title = hasWorkspace ? info.workspace : "워크스페이스 없음";
      updateComposerPlaceholder();
      return info;
    });
  }

  async function switchWorkspaceTo(rawPath) {
    // A provider run in flight writes into whatever workspace was active
    // when it started, and its reply/status render into chatThread
    // whenever it resolves -- switching workspaces mid-run (Open Folder,
    // a History drawer item) would tear down and rebuild chatThread out
    // from under it via loadChatHistory() below, so the stale run's reply
    // ends up appended to the *new* project's thread once it finally
    // settles. sendMessage() already refuses a second concurrent /api/run
    // this way; workspace switches need the same guard.
    if (runInFlight) {
      showToast("응답을 기다리는 중에는 워크스페이스를 전환할 수 없습니다.");
      return;
    }
    try {
      await postJSON("/api/open-folder", { path: rawPath });
    } catch (err) {
      showToast(`폴더를 열 수 없음: ${err.message}`);
      return;
    }
    attachments = [];
    renderAttachments();
    await refreshWorkspaceLabel();
    await renderTree(treeEl, "");
    await loadChatHistory();
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
    if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
      // Native app window: real OS folder picker via the pywebview JS bridge.
      let chosen;
      try {
        chosen = await window.pywebview.api.pick_folder();
      } catch (err) {
        showToast(`폴더 선택 실패: ${err}`);
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
    if (minutes < 1) return "방금";
    if (minutes < 60) return `${minutes}분 전`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}시간 전`;
    const days = Math.floor(hours / 24);
    if (days === 1) return "어제";
    if (days < 7) return `${days}일 전`;
    return new Date(iso).toISOString().slice(0, 10);
  }

  function renderHistoryItem(group, turn) {
    const row = el("div", { class: "history-item" }, []);
    const top = el("div", { class: "hi-top" }, [el("span", { text: turn.provider || "-" }, [])]);
    if (turn.status) {
      top.appendChild(
        el("span", { class: `status-badge status-${turn.status}`, text: STATUS_LABEL[turn.status] || turn.status }, [])
      );
    }
    top.appendChild(el("span", { class: "hi-time", text: formatRelativeTime(turn.ts) }, []));
    row.appendChild(top);
    row.appendChild(el("div", { class: "hi-task", text: turn.text || "(빈 메시지)" }, []));
    row.addEventListener("click", () => {
      closeHistoryDrawer();
      if (!group.current) switchWorkspaceTo(group.path);
    });
    return row;
  }

  function renderHistoryGroups(groups) {
    historyList.innerHTML = "";
    if (!groups || groups.length === 0) {
      historyList.appendChild(el("div", { class: "hd-empty", text: "아직 대화 기록이 없습니다." }, []));
      return;
    }
    for (const group of groups) {
      const header = el("div", { class: "hd-group" }, [
        el("span", { text: "📁" }, []),
        el("span", { text: group.name }, []),
      ]);
      if (group.current) header.appendChild(el("span", { class: "current-tag", text: "현재" }, []));
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
      historyList.appendChild(el("div", { class: "hd-empty", text: `불러올 수 없음: ${err.message}` }, []));
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

  const PROVIDER_LABEL = { codex: "Codex", claude: "Claude Code" };

  function renderProviderRow(info) {
    const row = el("div", { class: "pp-row" }, []);
    const badge = info.cli_detected
      ? el("span", { class: "status-badge status-success", text: "CLI 감지됨" }, [])
      : el("span", { class: "status-badge status-fail", text: "CLI 없음" }, []);
    row.appendChild(
      el("div", { class: "pp-top" }, [
        el("span", { class: "pp-name", text: PROVIDER_LABEL[info.provider] || info.provider }, []),
        badge,
      ])
    );
    if (info.cli_detected) {
      return row; // SCR-06: API 키 입력은 "CLI 없음" 상태에서만 노출
    }
    if (info.api_key_configured) {
      row.appendChild(
        el(
          "div",
          { class: "pp-note", text: `API 키로 연결됨 (model: ${info.model || "설정 필요"})` },
          []
        )
      );
    } else {
      row.appendChild(
        el("div", { class: "pp-note", text: "로컬에 CLI가 설치되어 있지 않습니다. API 키로 연결할까요?" }, [])
      );
    }
    const keyInput = el("input", { type: "password", placeholder: "API 키", "data-field": "key" }, []);
    const modelInput = el("input", {
      type: "text",
      placeholder: "model",
      "data-field": "model",
      value: info.model || "",
    }, []);
    const saveBtn = el("button", { type: "button", class: "primary", text: "저장" }, []);
    saveBtn.addEventListener("click", async () => {
      const key = keyInput.value.trim();
      const model = modelInput.value.trim();
      try {
        await postJSON("/api/provider-key", { provider: info.provider, key, model });
        showToast(key ? `${PROVIDER_LABEL[info.provider]} API 키가 저장되었습니다.` : `${PROVIDER_LABEL[info.provider]} API 키가 삭제되었습니다.`);
        await refreshProviderPanel();
      } catch (err) {
        showToast(`저장 실패: ${err.message}`);
      }
    });
    row.appendChild(el("div", { class: "pp-key-row" }, [keyInput, modelInput, saveBtn]));
    return row;
  }

  async function refreshProviderPanel() {
    providerPanelList.innerHTML = "";
    try {
      const data = await fetchJSON("/api/providers");
      for (const info of data.providers) {
        providerPanelList.appendChild(renderProviderRow(info));
      }
    } catch (err) {
      providerPanelList.appendChild(el("div", { class: "hd-empty", text: `불러올 수 없음: ${err.message}` }, []));
    }
  }

  function openProviderPanel() {
    providerPanelOverlay.classList.add("show");
    refreshProviderPanel();
  }

  function closeProviderPanel() {
    providerPanelOverlay.classList.remove("show");
  }

  diagnoseBtn.addEventListener("click", openProviderPanel);
  providerPanelClose.addEventListener("click", closeProviderPanel);
  providerPanelOverlay.addEventListener("click", (event) => {
    if (event.target === providerPanelOverlay) closeProviderPanel();
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
      container.appendChild(el("div", { class: "tree-error", text: `불러올 수 없음: ${err.message}` }, []));
      return;
    }
    if (entries.length === 0) {
      container.appendChild(el("div", { class: "tree-empty", text: "(비어 있음)" }, []));
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
      row.title = "클릭하면 첨부됩니다";
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
      showToast(`미리보기 없이 첨부됨: ${err.message}`);
    }
  }

  // ---------- attachments ----------

  function addAttachment(attachment) {
    if (attachments.some((a) => a.path && a.path === attachment.path)) return;
    attachments.push(attachment);
    renderAttachments();
  }

  function removeAttachment(index) {
    attachments.splice(index, 1);
    renderAttachments();
  }

  function renderAttachments() {
    composerAttachments.innerHTML = "";
    attachments.forEach((a, index) => {
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
    const hasContent = composerInput.value.trim().length > 0 || attachments.length > 0;
    sendBtn.disabled = runInFlight || !hasContent;
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
        document.createTextNode("아직 메시지가 없습니다."),
        el("br", {}, []),
        document.createTextNode("왼쪽에서 파일을 클릭하거나, 이 영역에 파일을 드래그해서 놓아보세요."),
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
    const autoBtn = el("button", { type: "button", class: "primary", text: "새 폴더 자동 생성" }, []);
    autoBtn.addEventListener("click", () => composerInput.focus());
    const pickBtn = el("button", { type: "button", text: "폴더 직접 선택…" }, []);
    pickBtn.addEventListener("click", pickFolder);
    chatThread.appendChild(
      el("div", { class: "no-workspace-card" }, [
        el("div", { class: "nw-icon", text: "📂" }, []),
        el("div", { class: "nw-title", text: "작업할 폴더가 아직 없습니다" }, []),
        el("div", { class: "nw-note", text: "기존 폴더를 고르거나, 새 프로젝트 폴더를 자동으로 만들 수 있습니다." }, []),
        el("div", { class: "nw-actions" }, [autoBtn, pickBtn]),
        el("div", { class: "nw-path" }, [
          document.createTextNode("자동 생성 위치: "),
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
      metaParts.push(document.createTextNode("나"));
    } else if (message.role === "agent") {
      roleClass = "agent";
      avatar = "🤖";
      metaParts.push(document.createTextNode(message.provider || "agent"));
      if (message.status) {
        metaParts.push(
          el("span", { class: `status-badge status-${message.status}` }, [
            document.createTextNode(`${STATUS_ICON[message.status] || ""} ${STATUS_LABEL[message.status] || message.status}`),
          ])
        );
      }
    } else {
      metaParts.push(document.createTextNode("시스템"));
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
        el("div", { class: "bubble" }, [el("span", { class: "spinner" }, []), document.createTextNode(" 실행 중…")]),
      ]),
    ]);
    chatThread.appendChild(msg);
    chatThread.scrollTop = chatThread.scrollHeight;
    return msg;
  }

  async function loadChatHistory() {
    try {
      const data = await fetchJSON("/api/chat");
      if (!data.messages || data.messages.length === 0) {
        showChatEmptyState();
        return;
      }
      chatThread.innerHTML = "";
      for (const message of data.messages) renderMessage(message);
    } catch (err) {
      showToast(`대화 기록을 불러오지 못함: ${err.message}`);
      showChatEmptyState();
    }
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
    // Re-entry guard: sendBtn.disabled alone doesn't stop the Enter-key
    // path, which never checks it. Without this, typing a follow-up and
    // hitting Enter while the first reply is still pending (runs can take
    // minutes) fires a second concurrent /api/run.
    if (runInFlight) return;

    const text = composerInput.value.trim();
    if (!text && attachments.length === 0) return;

    // DEC-02: confirm once per session before the first provider call,
    // then trust the user for the rest of the session. window.confirm()
    // is blocking/synchronous by design here -- we want the user's answer
    // before any token-spending call goes out, not a fire-and-forget toast.
    if (!sessionRunConfirmed) {
      const ok = window.confirm("Codex/Claude를 실행합니다. 토큰이 소비될 수 있습니다. 계속할까요?");
      if (!ok) return;
      sessionRunConfirmed = true;
    }

    const provider = providerSelect.value;
    const userMessage = { role: "user", text, attachments };
    renderMessage(userMessage);

    composerInput.value = "";
    composerInput.style.height = "auto";
    attachments = [];
    renderAttachments();
    updateSendState();

    const workspaceWasMissing = !hasWorkspace;
    try {
      await postJSON("/api/chat", userMessage);
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
      showToast(`대화 기록 저장 실패(화면에는 남아있음): ${err.message}`);
      return;
    }
    if (workspaceWasMissing) {
      // SCR-05: bring the titlebar and file tree up to date with the
      // workspace /api/chat just auto-created, before the provider call
      // that's about to follow.
      try {
        await refreshWorkspaceLabel();
        await renderTree(treeEl, "");
      } catch (err) {
        showToast(`워크스페이스 정보를 새로고침하지 못함: ${err.message}`);
      }
    }

    const busyMsg = renderBusyMessage(provider);
    runInFlight = true;
    composerInput.disabled = true;
    updateSendState();
    try {
      const result = await postJSON("/api/run", { provider, text, attachments: userMessage.attachments });
      busyMsg.remove();
      for (const agentMessage of result.messages) renderMessage(agentMessage);
    } catch (err) {
      busyMsg.remove();
      renderMessage({ role: "system", text: `실행 실패: ${err.message}`, attachments: [] });
      showToast(`provider 실행 실패: ${err.message}`);
    } finally {
      runInFlight = false;
      composerInput.disabled = false;
      updateSendState();
    }
  }

  // ---------- boot ----------

  async function boot() {
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
