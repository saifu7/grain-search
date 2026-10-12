//! Grain Search shell: global hotkey, hidden-but-preloaded window, tray icon, autostart,
//! and starting/stopping the Python search service (`uv run grain serve`).

use std::fs;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{
    AppHandle, Emitter, LogicalSize, Manager, PhysicalPosition, WebviewWindow, WindowEvent,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

const WIDTH_ROUNDED: f64 = 756.0; // 720 panel + 2 x 18 px transparent margin for the shadow
const WIDTH_GLASS: f64 = 720.0;
const SERVICE_START_TIMEOUT: Duration = Duration::from_secs(180);

// -- settings ------------------------------------------------------------------------------
/// Optional `settings.json`, looked up in %APPDATA%\app.grainsearch.desktop\ and then
/// in the `app` folder of the repository (development builds). Every field is optional.
#[derive(Clone, Deserialize)]
#[serde(default)]
struct Settings {
    hotkey: String,
    glass: bool,
    project_dir: Option<String>,
    uv: Option<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            hotkey: "Alt+Space".into(),
            glass: false,
            project_dir: None,
            uv: None,
        }
    }
}

fn load_settings(app: &AppHandle) -> Settings {
    let mut candidates = vec![];
    if let Ok(dir) = app.path().app_config_dir() {
        candidates.push(dir.join("settings.json"));
    }
    candidates.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("settings.json"),
    );
    for p in candidates {
        if let Ok(text) = fs::read_to_string(&p) {
            if let Ok(s) = serde_json::from_str::<Settings>(&text) {
                return s;
            }
        }
    }
    Settings::default()
}

/// The engine folder (where the engine's `pyproject.toml` and `data\` live).
fn project_dir(s: &Settings) -> PathBuf {
    if let Some(p) = &s.project_dir {
        return PathBuf::from(p);
    }
    if let Ok(p) = std::env::var("GRAIN_SEARCH_ENGINE_DIR") {
        return PathBuf::from(p);
    }
    // app/src-tauri -> <repo>/engine (development builds only; replaced by app folders in Phase 1)
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("engine");
    p.canonicalize().map(strip_unc).unwrap_or(p)
}

/// `canonicalize` on Windows returns `\\?\C:\...`; uv and Python prefer plain paths.
fn strip_unc(p: PathBuf) -> PathBuf {
    let s = p.to_string_lossy();
    match s.strip_prefix(r"\\?\") {
        Some(rest) => PathBuf::from(rest),
        None => p,
    }
}

// -- app state -----------------------------------------------------------------------------
struct AppState {
    settings: Settings,
    child: Arc<Mutex<Option<Child>>>,
    hotkey_error: Mutex<Option<String>>,
}

#[derive(Clone, Serialize, Deserialize)]
struct ServiceInfo {
    url: String,
    token: String,
}

#[derive(Serialize)]
struct UiSettings {
    glass: bool,
    hotkey: String,
    hotkey_error: Option<String>,
}

// -- talking to the service (tiny HTTP client, 127.0.0.1 only) ---------------------------
fn read_info(dir: &Path) -> Option<ServiceInfo> {
    let text = fs::read_to_string(dir.join("data").join("service.json")).ok()?;
    serde_json::from_str(&text).ok()
}

fn http(info: &ServiceInfo, method: &str, path: &str) -> Option<u16> {
    let host = info.url.trim_start_matches("http://").trim_end_matches('/');
    let addr = host.to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        info.token
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = [0u8; 64];
    let n = stream.read(&mut buf).ok()?;
    let head = String::from_utf8_lossy(&buf[..n]);
    head.split_whitespace().nth(1)?.parse().ok()
}

fn healthy(info: &ServiceInfo) -> bool {
    http(info, "GET", "/health") == Some(200)
}

fn spawn_service(s: &Settings, dir: &Path) -> Result<Child, String> {
    let data = dir.join("data");
    fs::create_dir_all(&data).map_err(|e| format!("Cannot create {}: {e}", data.display()))?;
    let log = fs::File::create(data.join("service.log")).map_err(|e| e.to_string())?;
    let log2 = log.try_clone().map_err(|e| e.to_string())?;
    let uv = s.uv.clone().unwrap_or_else(|| "uv".into());
    let mut cmd = Command::new(&uv);
    cmd.args(["run", "grain", "serve"])
        .current_dir(dir)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log2);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn().map_err(|e| {
        format!(
            "Could not start `{uv} run grain serve` in {}: {e}. Is uv installed? You can set \"uv\" to its full path in settings.json.",
            dir.display()
        )
    })
}

/// Makes sure the search service is up (starting it if needed) and returns its address + token.
#[tauri::command]
async fn ensure_service(state: tauri::State<'_, AppState>) -> Result<ServiceInfo, String> {
    let settings = state.settings.clone();
    let child = state.child.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let dir = project_dir(&settings);
        if let Some(info) = read_info(&dir) {
            if healthy(&info) {
                return Ok(info);
            }
        }
        {
            let mut guard = child.lock().unwrap();
            let running = guard
                .as_mut()
                .map(|c| matches!(c.try_wait(), Ok(None)))
                .unwrap_or(false);
            if !running {
                *guard = Some(spawn_service(&settings, &dir)?);
            }
        }
        let deadline = Instant::now() + SERVICE_START_TIMEOUT;
        loop {
            if let Some(info) = read_info(&dir) {
                if healthy(&info) {
                    return Ok(info);
                }
            }
            if let Some(c) = child.lock().unwrap().as_mut() {
                if let Ok(Some(code)) = c.try_wait() {
                    return Err(format!(
                        "The search service stopped ({code}). Details: {}",
                        dir.join("data").join("service.log").display()
                    ));
                }
            }
            if Instant::now() > deadline {
                return Err(
                    "The search service took too long to start (see data\\service.log).".into(),
                );
            }
            std::thread::sleep(Duration::from_millis(400));
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Stops the service, but only if this app started it.
fn stop_service(app: &AppHandle) {
    let state = app.state::<AppState>();
    let mut guard = state.child.lock().unwrap();
    if let Some(mut c) = guard.take() {
        if let Some(info) = read_info(&project_dir(&state.settings)) {
            let _ = http(&info, "POST", "/shutdown");
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = c.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        let _ = c.kill();
    }
}

fn service_post(app: &AppHandle, path: &str) {
    let state = app.state::<AppState>();
    if let Some(info) = read_info(&project_dir(&state.settings)) {
        let _ = http(&info, "POST", path);
    }
}

// -- window ----------------------------------------------------------------------------------
fn width(app: &AppHandle) -> f64 {
    if app.state::<AppState>().settings.glass {
        WIDTH_GLASS
    } else {
        WIDTH_ROUNDED
    }
}

/// Centre horizontally on the monitor that has the mouse, about a fifth from the top.
fn place(w: &WebviewWindow) {
    let monitors = w.available_monitors().unwrap_or_default();
    let cursor = w.cursor_position().ok();
    let on_cursor = cursor.and_then(|c| {
        monitors
            .iter()
            .find(|m| {
                let (p, s) = (m.position(), m.size());
                c.x >= p.x as f64
                    && c.x < (p.x + s.width as i32) as f64
                    && c.y >= p.y as f64
                    && c.y < (p.y + s.height as i32) as f64
            })
            .cloned()
    });
    let monitor = on_cursor.or_else(|| w.primary_monitor().ok().flatten());
    if let (Some(m), Ok(size)) = (monitor, w.outer_size()) {
        let (p, s) = (m.position(), m.size());
        let x = p.x + (s.width as i32 - size.width as i32) / 2;
        let y = p.y + (s.height as f64 * 0.2) as i32;
        let _ = w.set_position(PhysicalPosition::new(x, y));
    }
}

fn show(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        place(&w);
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        let _ = app.emit("grain://shown", ());
    }
}

fn toggle(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false) {
            let _ = w.hide();
        } else {
            show(app);
        }
    }
}

#[tauri::command]
fn hide_window(window: WebviewWindow) {
    let _ = window.hide();
}

#[tauri::command]
fn set_height(app: AppHandle, window: WebviewWindow, height: f64) -> Result<(), String> {
    let h = height.clamp(60.0, 900.0);
    window
        .set_size(LogicalSize::new(width(&app), h))
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn get_settings(state: tauri::State<'_, AppState>) -> UiSettings {
    UiSettings {
        glass: state.settings.glass,
        hotkey: state.settings.hotkey.clone(),
        hotkey_error: state.hotkey_error.lock().unwrap().clone(),
    }
}

// -- tray ------------------------------------------------------------------------------------
fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let hotkey = app.state::<AppState>().settings.hotkey.clone();
    let open = MenuItem::with_id(
        app,
        "open",
        format!("Open Grain Search ({hotkey})"),
        true,
        None::<&str>,
    )?;
    let pause = MenuItem::with_id(app, "pause", "Pause indexing", true, None::<&str>)?;
    let resume = MenuItem::with_id(app, "resume", "Resume indexing", true, None::<&str>)?;
    let rescan = MenuItem::with_id(app, "rescan", "Re-scan all folders", true, None::<&str>)?;
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let autostart = CheckMenuItem::with_id(
        app,
        "autostart",
        "Start with Windows",
        true,
        autostart_on,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let s1 = PredefinedMenuItem::separator(app)?;
    let s2 = PredefinedMenuItem::separator(app)?;
    let s3 = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &open, &s1, &pause, &resume, &rescan, &s2, &autostart, &s3, &quit,
        ],
    )?;

    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip(format!("Grain Search ({hotkey})"))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show(app),
            "pause" => service_post(app, "/index/pause"),
            "resume" => service_post(app, "/index/resume"),
            "rescan" => service_post(app, "/index/rescan"),
            "autostart" => {
                let al = app.autolaunch();
                let _ = if al.is_enabled().unwrap_or(false) {
                    al.disable()
                } else {
                    al.enable()
                };
            }
            "quit" => {
                stop_service(app);
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

// -- entry point -------------------------------------------------------------------------------
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // must be first: a second launch just shows the existing window
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app)
        }))
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            let handle = app.handle().clone();
            let settings = load_settings(&handle);
            app.manage(AppState {
                settings: settings.clone(),
                child: Arc::new(Mutex::new(None)),
                hotkey_error: Mutex::new(None),
            });

            if let Err(e) = handle.global_shortcut().register(settings.hotkey.as_str()) {
                *handle.state::<AppState>().hotkey_error.lock().unwrap() = Some(e.to_string());
            }

            if let Some(w) = handle.get_webview_window("main") {
                if settings.glass {
                    use tauri::window::{Effect, EffectsBuilder};
                    let _ = w.set_effects(EffectsBuilder::new().effect(Effect::Acrylic).build());
                    let _ = w.set_shadow(true);
                }
                let _ = w.set_size(LogicalSize::new(width(&handle), 100.0));
            }

            build_tray(&handle)?;

            // first manual launch: show the bar so you can see it works; autostart launches stay hidden
            if !std::env::args().any(|a| a == "--hidden") {
                show(&handle);
            }
            Ok(())
        })
        .on_window_event(|window, event| match event {
            // click anywhere else -> hide, like Spotlight
            WindowEvent::Focused(false) => {
                let _ = window.hide();
            }
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            ensure_service,
            get_settings,
            hide_window,
            set_height
        ])
        .run(tauri::generate_context!())
        .expect("error while running Grain Search");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn strip_unc_removes_verbatim_prefix() {
        assert_eq!(
            strip_unc(PathBuf::from(r"\\?\C:\work\engine")),
            PathBuf::from(r"C:\work\engine")
        );
        assert_eq!(strip_unc(PathBuf::from("/tmp/x")), PathBuf::from("/tmp/x"));
    }

    #[test]
    fn settings_fields_are_optional() {
        let s: Settings = serde_json::from_str(r#"{"glass": true}"#).unwrap();
        assert!(s.glass);
        assert_eq!(s.hotkey, "Alt+Space");
        assert!(s.project_dir.is_none());
        assert!(s.uv.is_none());
    }

    #[test]
    fn read_info_parses_service_file() {
        let dir = std::env::temp_dir().join(format!("grain-test-{}", std::process::id()));
        fs::create_dir_all(dir.join("data")).unwrap();
        assert!(read_info(&dir).is_none());
        fs::write(
            dir.join("data").join("service.json"),
            r#"{"url": "http://127.0.0.1:8766", "token": "abc", "pid": 1}"#,
        )
        .unwrap();
        let info = read_info(&dir).expect("service.json should parse");
        assert_eq!(info.url, "http://127.0.0.1:8766");
        assert_eq!(info.token, "abc");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn http_sends_token_and_reads_status() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 1024];
            let n = stream.read(&mut buf).unwrap();
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
            req
        });
        let info = ServiceInfo {
            url: format!("http://127.0.0.1:{port}"),
            token: "secret".into(),
        };
        assert_eq!(http(&info, "GET", "/health"), Some(200));
        let req = server.join().unwrap();
        assert!(req.starts_with("GET /health HTTP/1.1"));
        assert!(req.contains("Authorization: Bearer secret"));
    }

    #[test]
    fn http_returns_none_when_nothing_listens() {
        let port = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port(); // listener dropped: port is closed
        let info = ServiceInfo {
            url: format!("http://127.0.0.1:{port}"),
            token: "x".into(),
        };
        assert_eq!(http(&info, "GET", "/health"), None);
    }
}
