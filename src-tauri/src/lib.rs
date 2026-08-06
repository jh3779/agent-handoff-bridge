// Phase 7a (DEC-22): the actual application logic stays in the Python
// backend (handoff_webui.py, PyInstaller-built as the
// "agent-handoff-bridge-server" sidecar) -- this Rust side only spawns it
// and points a window at its fixed local port once it's actually ready.
// No API/business logic lives here by design; see
// docs/research-phase7-framework.md and docs/design-system/
// flutter-mapping.html's DEC-22 for why (keep the tested Python backend,
// don't rewrite it).
use tauri::{WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_shell::process::CommandEvent;
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_dialog::init())
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
      let (mut rx, _child) = sidecar.spawn().expect("failed to spawn agent-handoff-bridge-server sidecar");

      let app_handle = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        let mut window_created = false;
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
              log::warn!("[server] {}", String::from_utf8_lossy(&line));
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
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
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
