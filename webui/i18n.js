// Agent Handoff Bridge -- UI language switch (한국어/English).
//
// Loaded before app.js (plain <script> tag, no build step/bundler in this
// project -- see webui/app.js's own header comment for the same
// no-tooling constraint) so app.js's IIFE can call the global
// AHB_I18N.t()/applyI18n() directly.
//
// Scope, deliberately narrow: this translates the app's own UI *chrome*
// only (buttons, labels, toasts, placeholders) -- never actual chat
// message content, which is data the user/provider wrote, not UI text.
// Provider/model/file/folder names are also never translated (they are
// literal identifiers, not language-dependent).
//
// No FOUC-prevention pre-paint script the way app.css's theme has one:
// a brief flash of the wrong *text* on load is far less jarring than a
// flash of the wrong *color scheme*, so this simply applies once app.js
// boots, same as every other dynamic render in that file.
var AHB_I18N = (function () {
  "use strict";

  var LANG_KEY = "ahb-lang";

  var STRINGS = {
    ko: {
      "titlebar.openFolder.title": "다른 폴더 열기",
      "titlebar.modelOverride.placeholder": "모델 (기본값)",
      "titlebar.update": "업데이트",
      "titlebar.update.title": "업데이트 확인",
      "titlebar.history": "History",
      "titlebar.history.title": "다른 프로젝트의 최근 대화",
      "titlebar.settings": "설정",
      "titlebar.settings.title": "설정 · provider 연결 상태 · 지침",
      "sidebar.footer": "파일을 클릭하면 첨부됩니다",

      "chat.empty.line1": "아직 메시지가 없습니다.",
      "chat.empty.line2": "왼쪽에서 파일을 클릭하거나, 이 영역에 파일을 드래그해서 놓아보세요.",
      "dropzone.text": "여기에 파일을 놓아 첨부",

      "composer.attach.title": "파일 첨부",
      "composer.placeholder.hasWorkspace": "메시지를 입력하세요…",
      "composer.placeholder.noWorkspace": "메시지를 입력하면 자동으로 폴더가 만들어집니다…",
      "composer.note": "전송은 선택한 provider를 실제로 호출합니다 — 토큰이 소비될 수 있습니다. 이 세션의 첫 전송만 확인을 거치고, 이후는 바로 실행됩니다.",

      "history.title": "이전 대화 기록",
      "history.close.title": "닫기",
      "history.empty": "아직 대화 기록이 없습니다.",
      "history.current": "현재",
      "history.loadError": "불러올 수 없음: {msg}",
      "history.emptyMessage": "(빈 메시지)",
      "time.justNow": "방금",
      "time.minutesAgo": "{n}분 전",
      "time.hoursAgo": "{n}시간 전",
      "time.yesterday": "어제",
      "time.daysAgo": "{n}일 전",

      "status.success": "완료",
      "status.handoff": "핸드오프 필요",
      "status.fail": "실패",

      "settings.title": "설정",
      "settings.general": "일반",
      "settings.autoFallback.label": "Auto-fallback",
      "settings.autoFallback.note": "선택한 provider가 quota/rate/컨텍스트 한도로 실패하면 다른 provider로 자동 전환",
      "settings.theme.label": "테마",
      "settings.theme.note": "기본값은 시스템 설정을 따라갑니다",
      "settings.theme.system": "시스템 설정",
      "settings.theme.light": "라이트",
      "settings.theme.dark": "다크",
      "settings.language.label": "언어",
      "settings.language.note": "앱 화면 표시 언어입니다",
      "settings.version.label": "버전",
      "settings.version.note": "현재 실행 중인 버전입니다",
      "settings.providers.title": "연결된 AI 모델",
      "settings.providers.intro_html": "<b>커스텀 provider</b>가 아닌 고정 provider는 CLI가 없으면 API 키로 직접 연결할 수 있습니다. 파일 읽기/쓰기/수정, 명령 실행 등 CLI 모드와 동일한 도구 사용을 지원합니다.",
      "settings.customProviders.intro_html": "<b>커스텀 provider</b> — CLI 없이 토큰을 직접 구매해 쓰는 OpenAI/Anthropic 호환 endpoint (OpenRouter, Groq, 로컬 Ollama/LM Studio 등)를 원하는 이름으로 여러 개 등록할 수 있습니다.",
      "settings.instructions.title": "지침",
      "settings.instructions.intro_html": "이 프로젝트에서 어떤 provider를 쓰든(CLI든 커스텀 API 키든) 항상 함께 전달되는 지침입니다. 프로젝트별로 저장되며, <code>.handoff/shared-context.md</code>로 워크스페이스에 남습니다.",
      "settings.instructions.placeholder": "예: legacy/ 폴더는 절대 수정하지 마세요. 이 프로젝트는 4-space indent를 씁니다…",
      "settings.close": "닫기",
      "settings.instructions.save": "지침 저장",
      "settings.instructions.saved": "지침이 저장되었습니다.",
      "settings.instructions.loading": "불러오는 중…",

      "provider.cliDetected": "CLI 감지됨",
      "provider.cliNotFound": "CLI 없음",
      "provider.cliModelNote": "기본 모델을 지정하면 매 전송마다 --model로 전달됩니다. 비워두면 CLI의 기본값을 사용합니다. Titlebar의 모델 입력창에서 메시지별로 덮어쓸 수도 있습니다.",
      "provider.cliModelSaved": "{provider}의 기본 모델이 \"{model}\"(으)로 저장되었습니다.",
      "provider.cliModelCleared": "{provider}의 기본 모델 설정이 삭제되었습니다.",
      "provider.noCliNoApiKeySupport": "로컬에 CLI가 설치되어 있지 않습니다. 이 provider는 아직 API 키 모드를 지원하지 않습니다.",
      "provider.connectedViaApiKey": "API 키로 연결됨 (model: {model})",
      "provider.modelRequired": "설정 필요",
      "provider.noCliConnectWithKey": "로컬에 CLI가 설치되어 있지 않습니다. API 키로 연결할까요?",
      "provider.apiKeyPlaceholder": "API 키",
      "provider.modelPlaceholder": "model",
      "provider.save": "저장",
      "provider.saveRequiresKey": "API 키를 입력해야 저장됩니다. 연결을 해제하려면 \"연결 해제\"를 사용하세요.",
      "provider.verifiedConfirmation": " (확인 응답: \"{text}\")",
      "provider.savedAndVerified": "{provider} API 키가 확인되어 저장되었습니다.{suffix}",
      "provider.saveFailed": "저장 실패: {msg}",
      "provider.disconnect": "연결 해제",
      "provider.disconnected": "{provider} API 키가 삭제되었습니다.",
      "provider.deleteFailed": "삭제 실패: {msg}",

      "customProvider.openaiCompat": "OpenAI 호환",
      "customProvider.anthropicCompat": "Anthropic 호환",
      "customProvider.delete": "삭제",
      "customProvider.deleted": "{name}가 삭제되었습니다.",
      "customProvider.namePlaceholder": "이름 (예: openrouter)",
      "customProvider.baseUrlPlaceholder": "base URL (예: https://openrouter.ai/api/v1)",
      "customProvider.add": "추가",
      "customProvider.fillAllFields": "이름/API 키/model/base URL을 모두 입력하세요.",
      "customProvider.addedAndVerified": "{name}가 확인되어 추가되었습니다.{suffix}",
      "customProvider.addFailed": "추가 실패: {msg}",

      "workspace.none": "워크스페이스 없음",
      "workspace.cannotSwitchWhileRunning": "응답을 기다리는 중에는 워크스페이스를 전환할 수 없습니다.",
      "workspace.openFailed": "폴더를 열 수 없음: {msg}",
      "workspace.pickFailed": "폴더 선택 실패: {err}",

      "session.new.title": "새 탭",
      "session.close.title": "탭 닫기",
      "session.createFailed": "새 탭을 만들지 못했습니다: {msg}",
      "session.closeFailed": "탭을 닫지 못했습니다: {msg}",
      "session.closeRunInFlight": "응답을 기다리는 중인 탭은 닫을 수 없습니다.",
      "session.switchFailed": "탭 전환 실패: {msg}",

      "noWorkspace.title": "작업할 폴더가 아직 없습니다",
      "noWorkspace.note": "기존 폴더를 고르거나, 새 프로젝트 폴더를 자동으로 만들 수 있습니다.",
      "noWorkspace.autoCreate": "새 폴더 자동 생성",
      "noWorkspace.pickFolder": "폴더 직접 선택…",
      "noWorkspace.autoCreateLocation": "자동 생성 위치: ",

      "tree.loadError": "불러올 수 없음: {msg}",
      "tree.empty": "(비어 있음)",
      "tree.clickToAttach": "클릭하면 첨부됩니다",
      "tree.attachedWithoutPreview": "미리보기 없이 첨부됨: {msg}",

      "msg.you": "나",
      "msg.system": "시스템",
      "msg.running": " 실행 중…",
      "msg.historyLoadFailed": "대화 기록을 불러오지 못함: {msg}",

      "send.confirm": "Codex/Claude를 실행합니다. 토큰이 소비될 수 있습니다. 계속할까요?",
      "send.chatSaveFailed": "대화 기록 저장 실패(화면에는 남아있음): {msg}",
      "send.workspaceRefreshFailed": "워크스페이스 정보를 새로고침하지 못함: {msg}",
      "send.runFailedSystemMsg": "실행 실패: {msg}",
      "send.runFailedToast": "provider 실행 실패: {msg}",

      "update.checking": "업데이트 확인 중입니다…",
      "update.cannotCheck": "업데이트를 확인할 수 없습니다.",
      "update.upToDate": "최신 버전을 사용 중입니다.",
      "update.availableVersion": "v{version} 사용 가능",
      "update.currentVersionNote": "현재 v{version} 사용 중. 릴리즈 노트를 확인하고 업데이트하세요.",
      "update.later": "나중에",
      "update.viewReleaseNotes": "릴리즈 노트 보기",

      "folderPrompt.title": "폴더 열기",
      "folderPrompt.note_html": "네이티브 폴더 선택 창을 쓰려면 <code>pip install pywebview</code> 후 다시 실행하세요. 지금은 절대 경로를 직접 입력해주세요.",
      "folderPrompt.cancel": "취소",
      "folderPrompt.open": "열기"
    },
    en: {
      "titlebar.openFolder.title": "Open a different folder",
      "titlebar.modelOverride.placeholder": "Model (default)",
      "titlebar.update": "Update",
      "titlebar.update.title": "Check for updates",
      "titlebar.history": "History",
      "titlebar.history.title": "Recent conversations from other projects",
      "titlebar.settings": "Settings",
      "titlebar.settings.title": "Settings · provider connections · instructions",
      "sidebar.footer": "Click a file to attach it",

      "chat.empty.line1": "No messages yet.",
      "chat.empty.line2": "Click a file on the left, or drag files into this area.",
      "dropzone.text": "Drop files here to attach",

      "composer.attach.title": "Attach files",
      "composer.placeholder.hasWorkspace": "Type a message…",
      "composer.placeholder.noWorkspace": "Type a message and a folder will be created automatically…",
      "composer.note": "Sending actually calls the selected provider — tokens may be spent. Only the first send this session asks for confirmation; every send after that runs immediately.",

      "history.title": "Conversation history",
      "history.close.title": "Close",
      "history.empty": "No conversation history yet.",
      "history.current": "Current",
      "history.loadError": "Couldn't load: {msg}",
      "history.emptyMessage": "(empty message)",
      "time.justNow": "just now",
      "time.minutesAgo": "{n}m ago",
      "time.hoursAgo": "{n}h ago",
      "time.yesterday": "Yesterday",
      "time.daysAgo": "{n}d ago",

      "status.success": "Done",
      "status.handoff": "Handoff needed",
      "status.fail": "Failed",

      "settings.title": "Settings",
      "settings.general": "General",
      "settings.autoFallback.label": "Auto-fallback",
      "settings.autoFallback.note": "Automatically switch to another provider when the selected one fails on a quota/rate/context limit",
      "settings.theme.label": "Theme",
      "settings.theme.note": "Follows the system setting by default",
      "settings.theme.system": "System",
      "settings.theme.light": "Light",
      "settings.theme.dark": "Dark",
      "settings.language.label": "Language",
      "settings.language.note": "The display language for this app",
      "settings.version.label": "Version",
      "settings.version.note": "The version currently running",
      "settings.providers.title": "Connected AI Models",
      "settings.providers.intro_html": "A fixed provider (not a <b>custom provider</b>) with no CLI can be connected directly with an API key. It supports the same tool access as CLI mode -- reading/writing/editing files, running commands.",
      "settings.customProviders.intro_html": "<b>Custom providers</b> -- register any number of OpenAI/Anthropic-compatible endpoints (OpenRouter, Groq, a local Ollama/LM Studio, etc.) under a name you choose, for buying tokens directly without a CLI.",
      "settings.instructions.title": "Instructions",
      "settings.instructions.intro_html": "Instructions sent along with every provider call in this project, whether CLI or a custom API key. Saved per project as <code>.handoff/shared-context.md</code> in the workspace.",
      "settings.instructions.placeholder": "e.g. Never modify the legacy/ folder. This project uses 4-space indentation…",
      "settings.close": "Close",
      "settings.instructions.save": "Save Instructions",
      "settings.instructions.saved": "Instructions saved.",
      "settings.instructions.loading": "Loading…",

      "provider.cliDetected": "CLI detected",
      "provider.cliNotFound": "No CLI",
      "provider.cliModelNote": "Set a default model to have it sent as --model on every send. Leave blank to use the CLI's own default. You can also override it per message from the model field in the titlebar.",
      "provider.cliModelSaved": "{provider}'s default model saved as \"{model}\".",
      "provider.cliModelCleared": "{provider}'s default model was cleared.",
      "provider.noCliNoApiKeySupport": "No local CLI installed. This provider doesn't support API-key mode yet.",
      "provider.connectedViaApiKey": "Connected via API key (model: {model})",
      "provider.modelRequired": "not set",
      "provider.noCliConnectWithKey": "No local CLI installed. Connect with an API key instead?",
      "provider.apiKeyPlaceholder": "API key",
      "provider.modelPlaceholder": "model",
      "provider.save": "Save",
      "provider.saveRequiresKey": "An API key is required to save. Use \"Disconnect\" to remove the connection.",
      "provider.verifiedConfirmation": " (verification reply: \"{text}\")",
      "provider.savedAndVerified": "{provider} API key verified and saved.{suffix}",
      "provider.saveFailed": "Save failed: {msg}",
      "provider.disconnect": "Disconnect",
      "provider.disconnected": "{provider} API key removed.",
      "provider.deleteFailed": "Delete failed: {msg}",

      "customProvider.openaiCompat": "OpenAI-compatible",
      "customProvider.anthropicCompat": "Anthropic-compatible",
      "customProvider.delete": "Delete",
      "customProvider.deleted": "{name} removed.",
      "customProvider.namePlaceholder": "Name (e.g. openrouter)",
      "customProvider.baseUrlPlaceholder": "base URL (e.g. https://openrouter.ai/api/v1)",
      "customProvider.add": "Add",
      "customProvider.fillAllFields": "Fill in name/API key/model/base URL.",
      "customProvider.addedAndVerified": "{name} verified and added.{suffix}",
      "customProvider.addFailed": "Add failed: {msg}",

      "workspace.none": "No workspace",
      "workspace.cannotSwitchWhileRunning": "Can't switch workspace while waiting for a response.",
      "workspace.openFailed": "Couldn't open folder: {msg}",
      "workspace.pickFailed": "Folder selection failed: {err}",

      "session.new.title": "New tab",
      "session.close.title": "Close tab",
      "session.createFailed": "Couldn't create a new tab: {msg}",
      "session.closeFailed": "Couldn't close the tab: {msg}",
      "session.closeRunInFlight": "Can't close a tab that's still waiting for a response.",
      "session.switchFailed": "Couldn't switch tabs: {msg}",

      "noWorkspace.title": "No folder to work in yet",
      "noWorkspace.note": "Choose an existing folder, or create a new project folder automatically.",
      "noWorkspace.autoCreate": "Create a new folder automatically",
      "noWorkspace.pickFolder": "Choose a folder…",
      "noWorkspace.autoCreateLocation": "Auto-created at: ",

      "tree.loadError": "Couldn't load: {msg}",
      "tree.empty": "(empty)",
      "tree.clickToAttach": "Click to attach",
      "tree.attachedWithoutPreview": "Attached without a preview: {msg}",

      "msg.you": "You",
      "msg.system": "System",
      "msg.running": " Running…",
      "msg.historyLoadFailed": "Couldn't load conversation history: {msg}",

      "send.confirm": "This will run Codex/Claude. Tokens may be spent. Continue?",
      "send.chatSaveFailed": "Failed to save the conversation (still shown on screen): {msg}",
      "send.workspaceRefreshFailed": "Couldn't refresh workspace info: {msg}",
      "send.runFailedSystemMsg": "Run failed: {msg}",
      "send.runFailedToast": "Provider run failed: {msg}",

      "update.checking": "Checking for updates…",
      "update.cannotCheck": "Couldn't check for updates.",
      "update.upToDate": "You're up to date.",
      "update.availableVersion": "v{version} available",
      "update.currentVersionNote": "Currently on v{version}. Check the release notes and update.",
      "update.later": "Later",
      "update.viewReleaseNotes": "View release notes",

      "folderPrompt.title": "Open Folder",
      "folderPrompt.note_html": "To use a native folder picker, run <code>pip install pywebview</code> and restart. For now, type an absolute path directly.",
      "folderPrompt.cancel": "Cancel",
      "folderPrompt.open": "Open"
    }
  };

  function getLanguage() {
    var saved;
    try {
      saved = localStorage.getItem(LANG_KEY);
    } catch (err) {
      saved = null;
    }
    // Default "ko", not navigator.language-sniffed: this app's UI has
    // always been Korean-only text, so an unset preference must keep
    // rendering exactly what every existing user already sees -- the
    // same "unset means unchanged" default policy webui/app.js's own
    // theme preference already uses ("system" leaves rendering exactly
    // as before this feature existed).
    return saved === "en" ? "en" : "ko";
  }

  function setLanguage(lang) {
    try {
      if (lang === "en") {
        localStorage.setItem(LANG_KEY, "en");
      } else {
        localStorage.removeItem(LANG_KEY);
      }
    } catch (err) {
      // localStorage unavailable -- the choice still applies for this
      // page load, it just won't be remembered next launch.
    }
  }

  function t(key, params) {
    var table = STRINGS[getLanguage()] || STRINGS.ko;
    var text = Object.prototype.hasOwnProperty.call(table, key) ? table[key] : STRINGS.ko[key];
    if (text === undefined) return key; // missing key: surface it in the UI, don't silently blank the element
    if (params) {
      Object.keys(params).forEach(function (name) {
        text = text.split("{" + name + "}").join(String(params[name]));
      });
    }
    return text;
  }

  // Translates already-in-the-DOM static markup (index.html's own tags).
  // Dynamically generated content (provider rows, history items, chat
  // messages, toasts -- built in app.js via el()/document.createTextNode())
  // is NOT covered here; those call t() directly at render time instead,
  // since there is no persistent DOM node to walk before they exist.
  function applyI18n(root) {
    var scope = root || document;
    var textNodes = scope.querySelectorAll("[data-i18n]");
    for (var i = 0; i < textNodes.length; i++) {
      textNodes[i].textContent = t(textNodes[i].getAttribute("data-i18n"));
    }
    // _html variants: only ever used for a handful of trusted, developer-
    // authored strings in the dictionary above that embed a <code>/<b> tag
    // mid-sentence -- never user/provider data, so innerHTML here carries
    // no injection risk the way it would for chat message content
    // (which DEC-03/webui/app.js's renderTextWithCodeBlocks() deliberately
    // never uses innerHTML for).
    var htmlNodes = scope.querySelectorAll("[data-i18n-html]");
    for (var j = 0; j < htmlNodes.length; j++) {
      htmlNodes[j].innerHTML = t(htmlNodes[j].getAttribute("data-i18n-html"));
    }
    var titleNodes = scope.querySelectorAll("[data-i18n-title]");
    for (var k = 0; k < titleNodes.length; k++) {
      titleNodes[k].title = t(titleNodes[k].getAttribute("data-i18n-title"));
    }
    var placeholderNodes = scope.querySelectorAll("[data-i18n-placeholder]");
    for (var m = 0; m < placeholderNodes.length; m++) {
      placeholderNodes[m].placeholder = t(placeholderNodes[m].getAttribute("data-i18n-placeholder"));
    }
  }

  return { t: t, applyI18n: applyI18n, getLanguage: getLanguage, setLanguage: setLanguage };
})();
