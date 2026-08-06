// Phase 7a (DEC-22): the actual application logic stays in the Python
// backend (handoff_webui.py, PyInstaller-built as the
// "agent-handoff-bridge-server" sidecar) -- this Rust side only spawns it
// and points a window at its fixed local port once it's actually ready.
// No API/business logic lives here by design; see
// docs/research-phase7-framework.md and docs/design-system/
// flutter-mapping.html's DEC-22 for why (keep the tested Python backend,
// don't rewrite it).
use std::sync::{Arc, Mutex};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

// Matches handoff_webui.py's --port default. tauri.conf.json's
// app.windows is deliberately empty -- the window is created
// programmatically below, only after the sidecar's own stdout confirms
// it's actually listening, not just spawned. A statically-declared
// window (the first thing tried) navigates the instant it's created,
// which races a PyInstaller onefile binary's real, non-trivial startup
// cost (self-extracting to a temp dir, then a full Python import) --
// found by actually launching the built .app and seeing a permanently
// blank window (the webview's one-shot initial navigation had already
// failed by the time the server was really up; nothing ever retried
// it). A later sub-phase can make the port dynamic if 8787 being taken
// ever becomes a real problem (out of scope for 7a's "does the sidecar
// architecture work at all" goal).
const SERVER_URL: &str = "http://127.0.0.1:8787/";
// Printed by handoff_webui.py's main() -- after ThreadingHTTPServer(...)
// has already bound and is listening -- as either "... serving
// <workspace>" or "... -- no workspace yet". Matching the common prefix
// covers both without caring which.
const SERVER_READY_MARKER: &str = "Agent Handoff Bridge web UI";

// Phase 7b M6: the CommandChild returned by sidecar.spawn() below used to
// be dropped immediately (bound as `_child`) -- verified empirically
// (built the real .app, quit it normally, checked `ps`) that dropping it
// does NOT kill the sidecar: tauri-plugin-shell's CommandChild has no
// Drop-triggered cleanup, so the process just gets reparented to launchd
// and keeps running forever, still holding port 8787 -- confirmed as a
// real, already-hit bug (a leftover orphaned sidecar from earlier local
// testing was found squatting on the port during this same
// investigation). Stored here as managed state so the RunEvent::Exit
// handler in run() below can reach it and kill it before the app actually
// exits.
type SidecarChildState = Arc<Mutex<Option<CommandChild>>>;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  let app = tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_dialog::init())
    .manage(SidecarChildState::default())
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
      let sidecar = app
        .shell()
        .sidecar("agent-handoff-bridge-server")
        .expect("failed to create agent-handoff-bridge-server sidecar command")
        .env("PYTHONUNBUFFERED", "1")
        .args(["--no-browser"]);
      let (mut rx, child) = sidecar.spawn().expect("failed to spawn agent-handoff-bridge-server sidecar");
      *app.state::<SidecarChildState>().lock().unwrap() = Some(child);

      let app_handle = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        let mut window_created = false;
        // Phase 7b M6: handoff_webui.py's ThreadingHTTPServer(...) has no
        // try/except around the bind call, so "port 8787 already taken"
        // surfaces as a raw Python traceback on stderr followed by a
        // non-zero exit -- verified empirically (pre-bound the port, then
        // launched a real second instance, read the actual log). Without
        // this, that case fell into Terminated's generic "exited before
        // it was ready" message below, which doesn't tell the user why or
        // what to do about it.
        let mut port_conflict_detected = false;
        while let Some(event) = rx.recv().await {
          match event {
            CommandEvent::Stdout(line) => {
              let text = String::from_utf8_lossy(&line);
              log::info!("[server] {text}");
              if !window_created && text.contains(SERVER_READY_MARKER) {
                window_created = true;
                let handle = app_handle.clone();
                let handle_for_closure = handle.clone();
                // WebviewWindowBuilder must run on the main thread.
                let _ = handle.run_on_main_thread(move || {
                  if let Err(err) = WebviewWindowBuilder::new(&handle_for_closure, "main", WebviewUrl::External(SERVER_URL.parse().unwrap()))
                    .title("Agent Handoff Bridge")
                    .inner_size(1100.0, 760.0)
                    .min_inner_size(720.0, 480.0)
                    .resizable(true)
                    .build()
                  {
                    log::error!("failed to create main window: {err}");
                    fatal_startup_error(&handle_for_closure, &format!("Failed to open the app window: {err}"));
                  }
                });
              }
            }
            CommandEvent::Stderr(line) => {
              let text = String::from_utf8_lossy(&line);
              log::warn!("[server] {text}");
              if text.contains("Address already in use") {
                port_conflict_detected = true;
              }
            }
            CommandEvent::Error(err) => {
              log::error!("[server] sidecar error: {err}");
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
              if !window_created {
                let message = if port_conflict_detected {
                  "Port 8787 is already in use, so the app's local server \
                   could not start. Another instance of Agent Handoff \
                   Bridge (or something else) may already be running -- \
                   quit it and try again."
                    .to_string()
                } else {
                  format!("The app's local server exited before it was ready (code: {:?}).", payload.code)
                };
                fatal_startup_error(&app_handle, &message);
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
    // site. If the sidecar already died on its own (e.g. the port-conflict
    // crash above), the state is already None and this is a no-op.
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

/// Kills the sidecar and its PyInstaller onefile bootloader's re-exec'd
/// inner process. CommandChild::kill() (Rust stdlib's Child::kill(), i.e.
/// SIGKILL on Unix / TerminateProcess on Windows) only reaches the single
/// PID Tauri directly spawned -- verified empirically (built the real
/// .app, quit it, checked `ps`) that this leaves the bootloader's inner
/// process, which shows up as a separate PID with the outer one as its
/// ppid, running as a newly-orphaned process still holding port 8787. A
/// plain SIGTERM sent manually to the outer PID happened to cascade
/// correctly in manual testing, but relying on the bootloader's own
/// signal-forwarding behavior isn't something this project controls --
/// explicitly killing the process tree instead. Descendants have to be
/// killed *before* the parent: once the parent's dead, the child's ppid
/// changes (reparented to launchd/init), and `pkill -P <pid>` matching
/// stops working.
fn kill_sidecar_tree(child: CommandChild) {
  let pid = child.pid();
  #[cfg(unix)]
  {
    let _ = std::process::Command::new("pkill").args(["-P", &pid.to_string()]).status();
  }
  #[cfg(windows)]
  {
    // /T: kill the whole tree, not just this PID -- covers the
    // descendant-before-parent ordering concern above in one call.
    let _ = std::process::Command::new("taskkill").args(["/T", "/F", "/PID", &pid.to_string()]).status();
  }
  let _ = child.kill();
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
