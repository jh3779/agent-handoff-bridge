// Phase 7a (DEC-22): the actual application logic stays in the Python
// backend (handoff_webui.py, PyInstaller-built as the
// "agent-handoff-bridge-server" sidecar) -- this Rust side only spawns it
// and opens a window once it's actually ready. No API/business logic lives
// here by design; see docs/research-phase7-framework.md and
// docs/design-system/flutter-mapping.html's DEC-22 for why (keep the tested
// Python backend, don't rewrite it).
//
// How the page reaches that backend: no network port at all. The window loads
// the page bundled in the app (tauri.conf.json `frontendDist`), and every
// request the page makes goes through the `api_request` command below to the
// sidecar's stdin/stdout (`handoff_webui.py --stdio`, ipc.rs, webui_stdio.py).
// (Released versions up to v0.5.1 had the sidecar listen on 127.0.0.1:8787 with
// the window pointed at it. That mode and its port-conflict handling were
// removed once the portless path was confirmed on an installed build; a switch
// to fall back to it existed only briefly on the development branch.)
mod ipc;

use ipc::{IpcState, LineKind};
use serde_json::Value;
use std::sync::{Arc, Mutex};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

// tauri.conf.json's app.windows is deliberately empty -- the window is created
// programmatically below, only after the sidecar's `ready` event (ipc.rs), not
// merely after it was spawned. A PyInstaller onefile binary has a real,
// non-trivial startup cost (self-extracting to a temp dir, then a full Python
// import); a window created at once would load the page and fire its first
// requests before there is anything to answer them.

// Phase 7b M6: the CommandChild returned by sidecar.spawn() below used to
// be dropped immediately (bound as `_child`) -- verified empirically
// (built the real .app, quit it normally, checked `ps`) that dropping it
// does NOT kill the sidecar: tauri-plugin-shell's CommandChild has no
// Drop-triggered cleanup, so the process just gets reparented to launchd
// and keeps running forever -- confirmed as a real, already-hit bug (a
// leftover orphaned sidecar from earlier local testing was found squatting on
// the old localhost port during this same investigation). Stored here as
// managed state so the RunEvent::Exit handler in run() below can reach it and
// kill it before the app actually exits.
type SidecarChildState = Arc<Mutex<Option<CommandChild>>>;

/// The page's way of talking to the bundled server. `request` is
/// `{method, path, headers, body}`; the answer is the server's response frame
/// `{id, status, headers, body, encoding}`. A transport failure (server gone,
/// malformed call) is an `Err`; an HTTP-level error such as a 404 is still an
/// `Ok` carrying that status, exactly as it would be over HTTP.
#[tauri::command]
async fn api_request(
  ipc_state: tauri::State<'_, IpcState>,
  sidecar: tauri::State<'_, SidecarChildState>,
  request: Value,
) -> Result<Value, String> {
  let (id, reply) = ipc_state.register();
  let line = match ipc::request_frame(id, &request) {
    Ok(line) => line,
    Err(message) => {
      ipc_state.cancel(id);
      return Err(message);
    }
  };
  {
    // Not held across the await below.
    let mut guard = sidecar.lock().unwrap();
    let written = match guard.as_mut() {
      Some(child) => child.write(line.as_bytes()).map_err(|err| format!("could not reach the app's local server: {err}")),
      None => Err("the app's local server is not running".to_string()),
    };
    if let Err(message) = written {
      ipc_state.cancel(id);
      return Err(message);
    }
  }
  match reply.await {
    Ok(answer) => answer,
    Err(_) => Err("the app's local server stopped before answering".to_string()),
  }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  let app = tauri::Builder::default()
    // Must be the first plugin registered (Tauri's own docs) so it can
    // intercept a second launch before anything else in this chain runs.
    // A second instance's whole process exits right after this callback
    // fires -- it never reaches the sidecar-spawn code below, so there is
    // no second agent-handoff-bridge-server sharing the first one's
    // workspace registry and chat files.
    .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
      if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.set_focus();
      }
      // If "main" doesn't exist yet, the first instance's sidecar hasn't
      // signalled ready -- it'll appear on its own shortly; nothing to
      // focus yet.
    }))
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_dialog::init())
    .plugin(tauri_plugin_updater::Builder::new().build())
    .manage(SidecarChildState::default())
    .manage(IpcState::default())
    .invoke_handler(tauri::generate_handler![api_request])
    .setup(|app| {
      // Always on, not just debug_assertions -- a review round pointed
      // out that gating this to debug builds left a release build with
      // *zero* diagnostic trail if the sidecar ever fails before the
      // window is created (see the CommandEvent::Error/Terminated arms
      // below): no window, no log, nothing a user could report. This
      // writes to tauri-plugin-log's default per-platform log file
      // location in addition to stdout, so a silent-looking failure
      // still leaves something to look at.
      app.handle().plugin(
        tauri_plugin_log::Builder::default()
          .level(log::LevelFilter::Info)
          .build(),
      )?;

      // Matches handoff_webui.py's own `--no-browser` flag: start the
      // HTTP server and block, without opening a browser tab or its own
      // pywebview window -- this Tauri window is the window now.
      // PYTHONUNBUFFERED=1: an extra safety net, not the actual fix.
      // CPython switches stdout to fully-buffered (not line-buffered)
      // the moment it's a pipe instead of a tty, so the readiness print
      // below could sit in Python's own userspace buffer indefinitely
      // instead of ever reaching this process's CommandEvent::Stdout --
      // found by testing the actual built .app, where window creation
      // just hung. This env var alone turned out NOT to reliably fix it
      // when tested against the real PyInstaller onefile binary (its
      // bootloader's own environment/re-exec handling apparently
      // doesn't guarantee it reaches the embedded interpreter) -- the
      // real fix is handoff_webui.py's own
      // `sys.stdout.reconfigure(line_buffering=True)` at the top of
      // main(), which is unaffected by that. Left here anyway since
      // it's a harmless, standard signal plenty of other Python tooling
      // respects.
      // `--stdio` serves the Web UI's routes over stdin/stdout with no
      // listening socket (webui_stdio.py).
      log::info!("[server] starting the bundled server over stdin/stdout");
      let sidecar = app
        .shell()
        .sidecar("agent-handoff-bridge-server")
        .expect("failed to create agent-handoff-bridge-server sidecar command")
        .env("PYTHONUNBUFFERED", "1")
        .args(["--stdio"]);
      let (mut rx, child) = sidecar.spawn().expect("failed to spawn agent-handoff-bridge-server sidecar");
      *app.state::<SidecarChildState>().lock().unwrap() = Some(child);

      let app_handle = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        let mut window_created = false;
        while let Some(event) = rx.recv().await {
          match event {
            CommandEvent::Stdout(line) => {
              let text = String::from_utf8_lossy(&line);
              // Response frames carry the user's chat text and file contents,
              // so a frame is never logged; only lines that are not frames
              // (stray prints) are.
              let kind = app_handle.state::<IpcState>().handle_line(&text);
              if kind == LineKind::Ignored {
                log::info!("[server] {text}");
              }
              if !window_created && kind == LineKind::Ready {
                window_created = true;
                let handle = app_handle.clone();
                let handle_for_closure = handle.clone();
                // WebviewWindowBuilder must run on the main thread.
                let _ = handle.run_on_main_thread(move || {
                  // The page bundled into the app (tauri.conf.json frontendDist).
                  // webui/app.js sends its requests through `api_request` only
                  // when __AHB_IPC__ is set; in a plain browser it keeps using
                  // fetch().
                  log::info!("[window] opening the main window");
                  if let Err(err) = WebviewWindowBuilder::new(&handle_for_closure, "main", WebviewUrl::App("index.html".into()))
                    .title("Agent Handoff Bridge")
                    .inner_size(1100.0, 760.0)
                    .min_inner_size(720.0, 480.0)
                    .resizable(true)
                    .initialization_script("window.__AHB_IPC__ = true;")
                    .build()
                  {
                    log::error!("failed to create main window: {err}");
                    fatal_startup_error(&handle_for_closure, &format!("Failed to open the app window: {err}"));
                  }
                });
                // DEC-28: only start looking for a real update once the app
                // is actually up and usable -- this must never compete with
                // or delay sidecar startup/window creation above, which is
                // why it's spawned as its own independent task rather than
                // awaited inline here.
                spawn_update_check(app_handle.clone());
              }
            }
            CommandEvent::Stderr(line) => {
              let text = String::from_utf8_lossy(&line);
              log::warn!("[server] {text}");
            }
            CommandEvent::Error(err) => {
              log::error!("[server] sidecar error: {err}");
              app_handle.state::<IpcState>().fail_all("the app's local server failed");
              // A review round found that an error here before the
              // readiness marker ever printed (a Python import error, a
              // broken PyInstaller build, etc.) used to leave the app
              // running with no window, no dialog, and -- before the
              // logging fix above -- no diagnostic trail at all in a
              // release build. Report it and quit rather than
              // sitting invisibly forever.
              if !window_created {
                fatal_startup_error(&app_handle, &format!("The app's local server failed to start: {err}"));
              }
            }
            CommandEvent::Terminated(payload) => {
              log::warn!("[server] sidecar exited: {:?}", payload);
              app_handle.state::<IpcState>().fail_all("the app's local server exited");
              if !window_created {
                fatal_startup_error(
                  &app_handle,
                  &format!("The app's local server exited before it was ready (code: {:?}).", payload.code),
                );
              }
            }
            _ => {}
          }
        }
      });

      Ok(())
    })
    .build(tauri::generate_context!())
    .expect("error while building tauri application");

  app.run(|app_handle, event| {
    // Phase 7b M6: fires on every exit path this app has (window closed,
    // Cmd+Q, or fatal_startup_error's own app.exit(1)) -- one hook here
    // covers all of them instead of duplicating a kill call at each exit
    // site. If the sidecar already died on its own, the state is already None
    // and this is a no-op.
    //
    // RunEvent::Exit, not RunEvent::ExitRequested -- verified empirically
    // (built the real .app, quit it via a real Apple Event `tell
    // application ... to quit`, logged every RunEvent variant that
    // actually fired) that on macOS, ExitRequested does not fire at all
    // on a normal quit; the sequence observed was ...,
    // MainEventsCleared, Exit. ExitRequested is documented as
    // preventable (its `api.prevent_exit()`) and apparently tied to a
    // different trigger (e.g. the last window's close button) than the
    // whole-app Quit path this app actually uses -- Exit is the one
    // event confirmed to fire on every real exit this app has.
    if let RunEvent::Exit = event {
      if let Some(child) = app_handle.state::<SidecarChildState>().lock().unwrap().take() {
        kill_sidecar_tree(child);
      }
    }
  });
}

// How long to wait after a graceful terminate request before force-killing
// whatever's still alive. Not empirically tuned (no live testing this
// round -- see the commit/PR this was added in) -- chosen as a bounded,
// short delay: long enough to plausibly let a provider CLI flush a file
// write in progress, short enough that quitting the app doesn't feel like
// it hung.
const GRACEFUL_KILL_GRACE_PERIOD: std::time::Duration = std::time::Duration::from_millis(1500);

/// Kills the sidecar and its whole descendant process tree.
/// CommandChild::kill() (Rust stdlib's Child::kill(), i.e. SIGKILL on Unix
/// / TerminateProcess on Windows) only reaches the single PID Tauri
/// directly spawned -- verified empirically (built the real .app, quit it,
/// checked `ps`) that this leaves the PyInstaller onefile bootloader's
/// re-exec'd inner process orphaned and still running.
///
/// A single `pkill -P <pid>` (killing only direct children) is NOT
/// sufficient either: an in-flight provider run makes the real tree
/// deeper than "bootloader -> its re-exec'd interpreter" --
/// handoff_webui.py's bridge_command_prefix() shells out to a *second*
/// PyInstaller sidecar (agent-handoff-bridge-cli) for init/run, which
/// itself re-execs and then spawns the real codex/claude/agy
/// subprocess. That's 3-4 generations, none of which are a *direct* child
/// of the outer tracked PID, so a one-hop `pkill -P` misses all of them --
/// quitting the app mid-run would still orphan a live provider CLI. Fixed
/// by walking the full descendant tree via `pgrep -P` before killing
/// anything.
///
/// A review round pointed out that hard-killing unconditionally is itself
/// a real risk: if a provider CLI is mid-write when the app quits,
/// SIGKILL/a forced TerminateProcess gives it no chance to flush or clean
/// up. Graceful-terminate-then-force instead: signal every descendant to
/// exit cleanly, wait a short grace period, then force-kill only whatever
/// is still alive. Blocking here (this runs inside the RunEvent::Exit
/// callback, which is already on the way out) is an accepted, bounded
/// delay to app shutdown in exchange for not always hard-killing in-flight
/// work.
fn kill_sidecar_tree(child: CommandChild) {
  let pid = child.pid();
  #[cfg(unix)]
  {
    // Discover the whole tree first: once a node is dead, `pgrep -P`
    // can no longer find its children by ppid (they get reparented to
    // launchd/init), so discovery has to happen before any killing at
    // all. Signal from the deepest generation up (reverse discovery
    // order) so no intermediate node's children survive it -- a node is
    // always discovered before its own children get explored, so
    // reversing the discovery order guarantees children are signaled
    // before their parents regardless of traversal order.
    let mut descendants = descendant_pids_unix(pid);
    descendants.reverse();
    for descendant_pid in &descendants {
      let _ = std::process::Command::new("kill").args(["-TERM", &descendant_pid.to_string()]).status();
    }
    std::thread::sleep(GRACEFUL_KILL_GRACE_PERIOD);
    for descendant_pid in &descendants {
      // `kill -0`: no signal sent, exit status alone reports whether the
      // PID still exists -- only escalate to SIGKILL for survivors, not
      // everything unconditionally.
      let still_alive = std::process::Command::new("kill")
        .args(["-0", &descendant_pid.to_string()])
        .status()
        .map(|status| status.success())
        .unwrap_or(false);
      if still_alive {
        let _ = std::process::Command::new("kill").args(["-9", &descendant_pid.to_string()]).status();
      }
    }
  }
  #[cfg(windows)]
  {
    // Graceful attempt first (no /F -- taskkill sends a close request
    // rather than forcibly terminating), /T for the whole tree in one
    // call. Windows console apps (which codex/claude/agy are) may not
    // respond to a non-forced taskkill the same way a windowed GUI app
    // does -- unverified on real Windows, no Windows machine available to
    // test this fix on -- so this still escalates to a forced /F kill
    // after the same grace period rather than assuming the graceful
    // attempt worked.
    let _ = std::process::Command::new("taskkill").args(["/T", "/PID", &pid.to_string()]).status();
    std::thread::sleep(GRACEFUL_KILL_GRACE_PERIOD);
    let _ = std::process::Command::new("taskkill").args(["/T", "/F", "/PID", &pid.to_string()]).status();
  }
  let _ = child.kill();
}

/// Direct-and-indirect children of `root`, discovered via repeated
/// `pgrep -P` calls (a stdlib/libc-free way to walk the process tree on
/// Unix -- avoids adding a new Cargo dependency for something a system
/// tool already does correctly). Order is not guaranteed to be strict
/// breadth/depth-first, but every PID is guaranteed to appear after its
/// own parent, which is what kill_sidecar_tree's reversed-order kill
/// relies on.
#[cfg(unix)]
fn descendant_pids_unix(root: u32) -> Vec<u32> {
  let mut discovered = Vec::new();
  let mut frontier = vec![root];
  while let Some(parent) = frontier.pop() {
    let Ok(output) = std::process::Command::new("pgrep").args(["-P", &parent.to_string()]).output() else {
      continue;
    };
    for line in String::from_utf8_lossy(&output.stdout).lines() {
      if let Ok(child_pid) = line.trim().parse::<u32>() {
        discovered.push(child_pid);
        frontier.push(child_pid);
      }
    }
  }
  discovered
}

/// Shows a blocking native error dialog and quits -- the only reasonable
/// thing to do if the sidecar dies before a window ever exists: there's
/// no UI to show an in-app error in, and sitting invisibly forever
/// (this project's actual first failure mode, found by testing the real
/// build) is worse than a hard, visible exit.
fn fatal_startup_error(app: &tauri::AppHandle, message: &str) {
  app
    .dialog()
    .message(message)
    .title("Agent Handoff Bridge")
    .kind(MessageDialogKind::Error)
    .blocking_show();
  app.exit(1);
}

/// DEC-28: real, in-app self-update via Tauri's official updater plugin,
/// superseding DEC-22's "keep the gh-CLI-based check-only path, don't
/// adopt Tauri's updater" call -- that decision was made while this repo
/// was private, and rested specifically on "Tauri's updater has no
/// documented private-repo support path." The repo has been public since
/// v0.2.0 (docs/security-model.md), which is Tauri's actual blessed use
/// case for a GitHub-Releases-hosted update manifest -- the premise DEC-22
/// rested on no longer holds. This is additive, not a replacement:
/// handoff_bridge.py's own check_for_update()/webui/app.js's titlebar
/// badge (DEC-19) keep working exactly as before for source-zip/browser-
/// tab users, who have no installed native binary for this plugin to
/// replace in the first place.
///
/// Runs once per app launch (matches DEC-19's existing "check once at
/// startup, not periodic" cadence for the same feature's other half) --
/// silently does nothing on a failed check or "already current" (same
/// fail-quiet posture check_for_update() already has: this is a
/// background convenience, not something that should interrupt someone
/// who didn't ask). Only becomes visible once a real update exists, via a
/// native Yes/No confirmation dialog -- DEC-02's "don't act without
/// confirmation" principle applied to a new kind of action (installing
/// and restarting), not just CLI-mode's own tool-call gate.
fn spawn_update_check(app_handle: tauri::AppHandle) {
  tauri::async_runtime::spawn(async move {
    let updater = match app_handle.updater() {
      Ok(updater) => updater,
      Err(err) => {
        log::warn!("[updater] could not construct updater client: {err}");
        return;
      }
    };
    let update = match updater.check().await {
      Ok(Some(update)) => update,
      Ok(None) => {
        log::info!("[updater] already on the latest version");
        return;
      }
      Err(err) => {
        // Never a fatal_startup_error -- by this point the real window is
        // already up and usable; a network hiccup or a temporarily
        // missing latest.json (e.g. between release-process.md steps)
        // must not interrupt someone who is already using the app.
        log::warn!("[updater] update check failed: {err}");
        return;
      }
    };
    log::info!("[updater] update available: {}", update.version);
    let version = update.version.clone();
    let dialog_handle = app_handle.clone();
    // blocking_show() must not run on the async runtime's own thread (see
    // its own docs) -- spawn_blocking hands it a dedicated thread that can
    // actually block, then this task resumes once the user answers.
    let confirmed = tauri::async_runtime::spawn_blocking(move || {
      dialog_handle
        .dialog()
        .message(format!(
          "A new version ({version}) of Agent Handoff Bridge is available. \
           Download and install it now? The app will restart once it's \
           done."
        ))
        .title("Update Available")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::YesNo)
        .blocking_show()
    })
    .await
    .unwrap_or(false);
    if !confirmed {
      log::info!("[updater] user declined the available update");
      return;
    }
    if let Err(err) = update
      .download_and_install(
        |_chunk_length, _content_length| {
          // No progress UI today -- DEC-28 kept this to the officially
          // documented minimum flow rather than building a progress bar
          // this project has no existing UI pattern for; the confirm
          // dialog already sets the expectation that this may take a
          // moment.
        },
        || {
          log::info!("[updater] download finished, installing");
        },
      )
      .await
    {
      log::error!("[updater] download/install failed: {err}");
      app_handle
        .dialog()
        .message(format!(
          "The update could not be installed: {err}\n\nYou can still update manually from the GitHub Releases page."
        ))
        .title("Agent Handoff Bridge")
        .kind(MessageDialogKind::Error)
        .blocking_show();
      return;
    }
    log::info!("[updater] update installed, restarting");
    app_handle.restart();
  });
}
