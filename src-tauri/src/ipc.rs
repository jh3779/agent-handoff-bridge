// The portless transport between the page and the bundled Python server.
//
// The page calls the `api_request` command (lib.rs); this module turns that
// call into one JSON line for the sidecar's stdin and matches the JSON line the
// sidecar writes back to the call that is waiting for it. The wire format is
// documented in webui_stdio.py (the other end):
//
//   request   {"id": 7, "method": "POST", "path": "/api/run",
//              "headers": {...}, "body": "...json text..."}
//   response  {"id": 7, "status": 200, "headers": {...}, "body": "...", "encoding": "utf-8"}
//   event     {"event": "ready", "version": "..."}
//
// Nothing here touches Tauri, so it can be unit tested without a window.
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use tokio::sync::oneshot;

/// What the shell does with one line the sidecar printed on stdout.
#[derive(Debug, PartialEq, Eq)]
pub enum LineKind {
  /// The server finished starting; the window can be created.
  Ready,
  /// A response frame; the waiting `api_request` call was answered.
  Response,
  /// Not a frame (a stray print from a library, for example).
  Ignored,
}

type Reply = Result<Value, String>;

#[derive(Default)]
pub struct IpcState {
  next_id: AtomicU64,
  waiting: Mutex<HashMap<u64, oneshot::Sender<Reply>>>,
}

impl IpcState {
  /// Reserves an id and the channel its response will arrive on.
  pub fn register(&self) -> (u64, oneshot::Receiver<Reply>) {
    let id = self.next_id.fetch_add(1, Ordering::Relaxed) + 1;
    let (sender, receiver) = oneshot::channel();
    self.waiting.lock().unwrap().insert(id, sender);
    (id, receiver)
  }

  /// Forgets a request that never made it to the sidecar.
  pub fn cancel(&self, id: u64) {
    self.waiting.lock().unwrap().remove(&id);
  }

  /// Handles one stdout line. Frame contents are never logged here or by the
  /// caller: they carry the user's chat text and file contents.
  pub fn handle_line(&self, line: &str) -> LineKind {
    let Ok(frame) = serde_json::from_str::<Value>(line.trim()) else {
      return LineKind::Ignored;
    };
    if frame.get("event").and_then(Value::as_str) == Some("ready") {
      return LineKind::Ready;
    }
    let Some(id) = frame.get("id").and_then(Value::as_u64) else {
      return LineKind::Ignored;
    };
    if frame.get("status").is_none() {
      return LineKind::Ignored;
    }
    if let Some(sender) = self.waiting.lock().unwrap().remove(&id) {
      // The page may have gone away while waiting; nobody to tell then.
      let _ = sender.send(Ok(frame));
    }
    LineKind::Response
  }

  /// The sidecar is gone: answer everyone still waiting with an error instead of
  /// leaving their calls hanging forever.
  pub fn fail_all(&self, message: &str) {
    let pending: Vec<_> = self.waiting.lock().unwrap().drain().collect();
    for (_, sender) in pending {
      let _ = sender.send(Err(message.to_string()));
    }
  }

  #[cfg(test)]
  pub fn waiting_count(&self) -> usize {
    self.waiting.lock().unwrap().len()
  }
}

/// The request line for the sidecar's stdin, from the object the page passed to
/// `api_request`. Validates the shape so a malformed call fails here, with a
/// clear message, rather than as a 400 from the other side.
pub fn request_frame(id: u64, request: &Value) -> Result<String, String> {
  let object = request.as_object().ok_or("request must be an object")?;
  let path = object
    .get("path")
    .and_then(Value::as_str)
    .filter(|path| path.starts_with('/'))
    .ok_or("request.path must be a string starting with /")?;
  let method = object.get("method").and_then(Value::as_str).unwrap_or("GET");
  let headers = match object.get("headers") {
    None | Some(Value::Null) => json!({}),
    Some(Value::Object(map)) => Value::Object(map.clone()),
    Some(_) => return Err("request.headers must be an object".to_string()),
  };
  let mut frame = json!({ "id": id, "method": method, "path": path, "headers": headers });
  match object.get("body") {
    None | Some(Value::Null) => {}
    Some(Value::String(body)) => frame["body"] = Value::String(body.clone()),
    Some(_) => return Err("request.body must be a string".to_string()),
  }
  let mut line = serde_json::to_string(&frame).map_err(|err| err.to_string())?;
  line.push('\n');
  Ok(line)
}

#[cfg(test)]
mod tests {
  use super::*;

  #[test]
  fn ids_are_unique_and_start_at_one() {
    let state = IpcState::default();
    let (a, _ra) = state.register();
    let (b, _rb) = state.register();
    assert_eq!((a, b), (1, 2));
    assert_eq!(state.waiting_count(), 2);
  }

  #[test]
  fn a_response_frame_reaches_the_call_waiting_for_it() {
    let state = IpcState::default();
    let (first, mut first_rx) = state.register();
    let (second, mut second_rx) = state.register();
    // Answered out of order, as the server is free to do.
    let line = format!(r#"{{"id": {second}, "status": 200, "headers": {{}}, "body": "two", "encoding": "utf-8"}}"#);
    assert_eq!(state.handle_line(&line), LineKind::Response);
    assert!(first_rx.try_recv().is_err(), "the first call must still be waiting");
    let answer = second_rx.try_recv().unwrap().unwrap();
    assert_eq!(answer["body"], "two");
    let line = format!(r#"{{"id": {first}, "status": 404, "body": "one", "encoding": "utf-8"}}"#);
    state.handle_line(&line);
    assert_eq!(first_rx.try_recv().unwrap().unwrap()["status"], 404);
    assert_eq!(state.waiting_count(), 0);
  }

  #[test]
  fn the_ready_event_and_noise_are_told_apart() {
    let state = IpcState::default();
    assert_eq!(state.handle_line(r#"{"event": "ready", "version": "0.5.1"}"#), LineKind::Ready);
    assert_eq!(state.handle_line("some library printed this"), LineKind::Ignored);
    assert_eq!(state.handle_line(r#"{"hello": "world"}"#), LineKind::Ignored);
    assert_eq!(state.handle_line(r#"{"id": 5}"#), LineKind::Ignored); // no status: not a response
    assert_eq!(state.handle_line(""), LineKind::Ignored);
  }

  #[test]
  fn a_response_nobody_waits_for_is_harmless() {
    let state = IpcState::default();
    assert_eq!(state.handle_line(r#"{"id": 99, "status": 200, "body": ""}"#), LineKind::Response);
  }

  #[test]
  fn fail_all_answers_everyone_with_an_error() {
    let state = IpcState::default();
    let (_a, mut a_rx) = state.register();
    let (_b, mut b_rx) = state.register();
    state.fail_all("server exited");
    assert_eq!(a_rx.try_recv().unwrap().unwrap_err(), "server exited");
    assert_eq!(b_rx.try_recv().unwrap().unwrap_err(), "server exited");
    assert_eq!(state.waiting_count(), 0);
  }

  #[test]
  fn a_cancelled_request_is_forgotten() {
    let state = IpcState::default();
    let (id, _rx) = state.register();
    state.cancel(id);
    assert_eq!(state.waiting_count(), 0);
  }

  #[test]
  fn the_request_frame_is_one_json_line() {
    let request = json!({
      "method": "POST",
      "path": "/api/chat?x=1",
      "headers": {"X-AHB-Session": "default"},
      "body": "{\"text\": \"한글\\n줄바꿈\"}",
    });
    let line = request_frame(7, &request).unwrap();
    assert!(line.ends_with('\n'));
    assert_eq!(line.matches('\n').count(), 1, "a body with newlines must stay on one line");
    let frame: Value = serde_json::from_str(line.trim_end()).unwrap();
    assert_eq!(frame["id"], 7);
    assert_eq!(frame["method"], "POST");
    assert_eq!(frame["path"], "/api/chat?x=1");
    assert_eq!(frame["headers"]["X-AHB-Session"], "default");
    assert_eq!(frame["body"], request["body"]);
  }

  #[test]
  fn defaults_and_validation() {
    let get = request_frame(1, &json!({"path": "/api/info"})).unwrap();
    let frame: Value = serde_json::from_str(get.trim_end()).unwrap();
    assert_eq!(frame["method"], "GET");
    assert_eq!(frame["headers"], json!({}));
    assert!(frame.get("body").is_none());
    assert!(request_frame(1, &json!("nope")).is_err());
    assert!(request_frame(1, &json!({"path": "api/info"})).is_err());
    assert!(request_frame(1, &json!({})).is_err());
    assert!(request_frame(1, &json!({"path": "/a", "headers": []})).is_err());
    assert!(request_frame(1, &json!({"path": "/a", "body": 5})).is_err());
    assert!(request_frame(1, &json!({"path": "/a", "body": null})).is_ok());
  }
}

/// Talks to the real Python end. Needs Python and the repo checkout, so it is
/// opt-in: `cargo test --manifest-path src-tauri/Cargo.toml -- --include-ignored`.
#[cfg(test)]
mod live {
  use super::*;
  use std::io::{BufRead, BufReader, Write};
  use std::process::{Command, Stdio};

  #[test]
  #[ignore = "spawns python handoff_webui.py --stdio"]
  fn rust_and_python_agree_on_the_wire_format() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
    let home = std::env::temp_dir().join(format!("ahb-ipc-live-{}", std::process::id()));
    std::fs::create_dir_all(&home).unwrap();
    // `python` is the usual name on Windows, `python3` elsewhere; set
    // AHB_TEST_PYTHON to use a specific interpreter.
    let default_python = if cfg!(windows) { "python" } else { "python3" };
    let mut child = Command::new(std::env::var("AHB_TEST_PYTHON").unwrap_or_else(|_| default_python.to_string()))
      .arg(root.join("handoff_webui.py"))
      .args(["--stdio", "--workspace"])
      .arg(&home)
      .env("HOME", &home)
      .env("USERPROFILE", &home)
      // Do not start the background `agy models` lookup on the developer's machine.
      .env("AHB_NO_MODEL_PREWARM", "1")
      .current_dir(&root)
      .stdin(Stdio::piped())
      .stdout(Stdio::piped())
      .stderr(Stdio::null())
      .spawn()
      .expect("could not start Python (set AHB_TEST_PYTHON to its path)");
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let state = IpcState::default();

    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert_eq!(state.handle_line(&line), LineKind::Ready, "first line was: {line}");

    let mut call = |request: Value| -> Value {
      let (id, mut reply) = state.register();
      stdin.write_all(request_frame(id, &request).unwrap().as_bytes()).unwrap();
      stdin.flush().unwrap();
      let mut line = String::new();
      stdout.read_line(&mut line).unwrap();
      assert_eq!(state.handle_line(&line), LineKind::Response, "line was: {line}");
      reply.try_recv().expect("the call was not answered").expect("transport error")
    };

    let info = call(json!({"method": "GET", "path": "/api/info"}));
    assert_eq!(info["status"], 200);
    let info_body: Value = serde_json::from_str(info["body"].as_str().unwrap()).unwrap();
    assert!(info_body["workspace"].is_string());

    let posted = call(json!({
      "method": "POST",
      "path": "/api/chat",
      "headers": {"Content-Type": "application/json", "X-AHB-Session": "default"},
      "body": json!({"role": "user", "text": "러스트에서 보낸 한글 메시지\n두 번째 줄", "attachments": []}).to_string(),
    }));
    assert_eq!(posted["status"], 200);

    let chat = call(json!({"method": "GET", "path": "/api/chat", "headers": {"X-AHB-Session": "default"}}));
    assert_eq!(chat["status"], 200);
    assert!(chat["body"].as_str().unwrap().contains("러스트에서 보낸 한글 메시지"));

    let missing = call(json!({"method": "GET", "path": "/api/nope"}));
    assert_eq!(missing["status"], 404);

    let page = call(json!({"method": "GET", "path": "/"}));
    assert_eq!(page["status"], 200);
    assert!(page["body"].as_str().unwrap().contains("<title>"));

    drop(stdin); // EOF: the server must exit by itself
    let status = child.wait().unwrap();
    assert!(status.success(), "python exited with {status:?}");
    let _ = std::fs::remove_dir_all(&home);
  }
}
