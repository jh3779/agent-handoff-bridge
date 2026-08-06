// Phase 7a (DEC-22): the actual application logic stays in the Python
// backend (handoff_webui.py, PyInstaller-built as the
// "agent-handoff-bridge-server" sidecar) -- this Rust side only spawns it
// and points a window at its fixed local port once it's actually ready.
// No API/business logic lives here by design; see
// docs/research-phase7-framework.md and docs/design-system/
// flutter-mapping.html's DEC-22 for why (keep the tested Python backend,
// don't rewrite it).
use tauri::{WebviewUrl, WebviewWindowBuilder};
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
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }

      // Matches handoff_webui.py's own `--no-browser` flag: start the
      // HTTP server and block, without opening a browser tab or its own
      // pywebview window -- this Tauri window is the window now.
      // PYTHONUNBUFFERED=1: found by testing the actual built .app --
      // without it, CPython's stdio switches to fully-buffered (not
      // line-buffered) the moment stdout is a pipe instead of a tty, so
      // the readiness print below could sit in Python's own userspace
      // buffer indefinitely (until it fills or the process exits)
      // instead of ever reaching this process's CommandEvent::Stdout,
      // silently hanging window creation forever. No Python source
      // change needed -- this is purely how the sidecar is invoked.
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
                  }
                });
              }
            }
            CommandEvent::Stderr(line) => {
              log::warn!("[server] {}", String::from_utf8_lossy(&line));
            }
            CommandEvent::Error(err) => {
              log::error!("[server] sidecar error: {err}");
            }
            CommandEvent::Terminated(payload) => {
              log::warn!("[server] sidecar exited: {:?}", payload);
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
