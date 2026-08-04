// Agent Handoff Bridge -- MVP web UI client.
// File browsing + attaching + a VS Code-style "Open Folder" switch, with a
// local per-workspace chat draft history. No provider is ever called from
// here -- "send" persists to <workspace>/.handoff/webui/chat/ and renders
// locally. See webui/index.html's composer-note and
// docs/provider-extensibility.md for what's intentionally not wired up yet.
(function () {
  "use strict";

  const MAX_PREVIEW_CHARS = 20000;

  const treeEl = document.getElementById("tree");
  const workspaceLabel = document.getElementById("workspace-label");
  const openFolderBtn = document.getElementById("open-folder-btn");
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

  /** @type {{name: string, path: string|null, content: string|null, truncated: boolean}[]} */
  let attachments = [];
  let dragDepth = 0;

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

  function refreshWorkspaceLabel() {
    return fetchJSON("/api/info").then((info) => {
      workspaceLabel.textContent = info.name;
      openFolderBtn.title = info.workspace;
      return info;
    });
  }

  async function switchWorkspaceTo(rawPath) {
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

  openFolderBtn.addEventListener("click", async () => {
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
  });

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
    sendBtn.disabled = !hasContent;
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
    const emptyState = chatThread.querySelector(".chat-empty");
    if (emptyState) emptyState.remove();
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

  function renderMessage(message) {
    clearChatEmptyState();
    const bubbleChildren = [];
    if (message.text) bubbleChildren.push(document.createTextNode(message.text));
    const bubble = el("div", { class: "bubble" }, bubbleChildren);

    if (message.attachments && message.attachments.length > 0) {
      const attachRow = el("div", { class: "attachments" }, []);
      for (const a of message.attachments) {
        attachRow.appendChild(el("span", { class: "chip", text: `${a.path ? "📄" : "📎"} ${a.name}` }, []));
      }
      bubble.appendChild(attachRow);
    }

    const isUser = message.role === "user";
    const msg = el("div", { class: `msg ${isUser ? "user" : "system"}` }, [
      el("div", { class: "avatar", text: isUser ? "🧑" : "🗂️" }, []),
      el("div", {}, [el("div", { class: "meta", text: isUser ? "나" : "시스템" }, []), bubble]),
    ]);
    chatThread.appendChild(msg);
    chatThread.scrollTop = chatThread.scrollHeight;
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
    const text = composerInput.value.trim();
    if (!text && attachments.length === 0) return;

    const message = { role: "user", text, attachments };
    renderMessage(message);

    composerInput.value = "";
    composerInput.style.height = "auto";
    attachments = [];
    renderAttachments();
    updateSendState();

    try {
      await postJSON("/api/chat", message);
    } catch (err) {
      showToast(`대화 기록 저장 실패(화면에는 남아있음): ${err.message}`);
    }
  }

  // ---------- boot ----------

  refreshWorkspaceLabel().catch(() => { workspaceLabel.textContent = "workspace"; });
  renderTree(treeEl, "");
  loadChatHistory();
})();
