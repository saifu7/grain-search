//! Grain Search shell: global hotkey, hidden-but-preloaded window, tray icon, autostart,
//! and starting/stopping the Python search service (`uv run grain serve`, on any free port).

use std::ffi::OsString;
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
    AppHandle, Emitter, LogicalSize, Manager, PhysicalPosition, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

mod setup;

const WIDTH_ROUNDED: f64 = 756.0; // 720 panel + 2 x 18 px transparent margin for the shadow
const WIDTH_GLASS: f64 = 720.0;
const SERVICE_START_TIMEOUT: Duration = Duration::from_secs(180);
const SETTINGS: &str = "settings"; // label of the Settings window
const SETUP: &str = "setup"; // label of the first-run setup window (installed app)

// -- app folders -------------------------------------------------------------------------
/// Where Grain Search keeps its files. Must match the engine's `paths.py` exactly:
/// Windows: %APPDATA%\GrainSearch (settings) and %LOCALAPPDATA%\GrainSearch (service.json,
/// logs, index); elsewhere the XDG folders; GRAIN_SEARCH_HOME=<folder> puts everything there.
#[derive(Clone, Debug, PartialEq)]
struct AppDirs {
    config: PathBuf,
    data: PathBuf,
}

impl AppDirs {
    fn service_file(&self) -> PathBuf {
        self.data.join("service.json")
    }
    fn log_file(&self) -> PathBuf {
        self.data.join("logs").join("service.log")
    }
}

fn resolve_app_dirs(get: impl Fn(&str) -> Option<String>, windows: bool) -> AppDirs {
    let var = |k: &str| {
        get(k)
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
    };
    if let Some(home) = var("GRAIN_SEARCH_HOME") {
        let p = PathBuf::from(home);
        return AppDirs {
            config: p.clone(),
            data: p,
        };
    }
    if windows {
        let user = var("USERPROFILE").unwrap_or_default();
        let roaming = var("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(&user).join("AppData").join("Roaming"));
        let local = var("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(&user).join("AppData").join("Local"));
        return AppDirs {
            config: roaming.join("GrainSearch"),
            data: local.join("GrainSearch"),
        };
    }
    let home = PathBuf::from(var("HOME").unwrap_or_default());
    let config = var("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    let data = var("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local").join("share"));
    AppDirs {
        config: config.join("grain-search"),
        data: data.join("grain-search"),
    }
}

fn app_dirs() -> AppDirs {
    resolve_app_dirs(|k| std::env::var(k).ok(), cfg!(windows))
}

// -- settings ------------------------------------------------------------------------------
/// Optional `settings.json` (window/hotkey options) in the settings folder (%APPDATA%\GrainSearch\).
/// Every field is optional. Search settings live in config.json, managed by the engine.
#[derive(Clone, Deserialize)]
#[serde(default)]
struct Settings {
    hotkey: String,
    glass: bool,
    /// The engine folder; normally found automatically (see `find_engine_dir`).
    #[serde(alias = "project_dir")]
    engine_dir: Option<String>,
    /// Full path to uv; normally found automatically (see `find_uv`).
    uv: Option<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            hotkey: DEFAULT_HOTKEY.into(),
            glass: false,
            engine_dir: None,
            uv: None,
        }
    }
}

fn load_settings(dirs: &AppDirs) -> Settings {
    fs::read_to_string(dirs.config.join("settings.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<Settings>(&text).ok())
        .unwrap_or_default()
}

// -- finding the engine and uv ---------------------------------------------------------------
/// A folder holding the Python engine (`pyproject.toml` + `src/grain_search`).
fn is_engine_dir(p: &Path) -> bool {
    p.join("pyproject.toml").is_file() && p.join("src").join("grain_search").is_dir()
}

/// Where the engine is, decided at run time so the app works wherever it is installed or built:
/// 1. `engine_dir` in settings.json, 2. GRAIN_SEARCH_ENGINE_DIR,
/// 3. an `engine` folder next to the app or in a folder above it - next to the installed
///    app, or `<repo>/engine` for a development build in `<repo>/app/src-tauri/target/debug`.
fn find_engine_dir(
    configured: Option<&str>,
    env_dir: Option<String>,
    exe: Option<PathBuf>,
) -> Result<PathBuf, String> {
    let given = configured
        .map(str::to_string)
        .or(env_dir)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty());
    if let Some(p) = given {
        let p = PathBuf::from(p);
        return if is_engine_dir(&p) {
            Ok(p)
        } else {
            Err(format!(
                "The engine folder {} has no pyproject.toml / src\\grain_search. Check \"engine_dir\" in settings.json or GRAIN_SEARCH_ENGINE_DIR.",
                p.display()
            ))
        };
    }
    let start = exe.as_deref().and_then(Path::parent);
    for dir in start.into_iter().flat_map(Path::ancestors).take(8) {
        let candidate = dir.join("engine");
        if is_engine_dir(&candidate) {
            return Ok(candidate.canonicalize().map(strip_unc).unwrap_or(candidate));
        }
    }
    Err(format!(
        "Could not find the Grain Search engine near {}. Set \"engine_dir\" in {} or the GRAIN_SEARCH_ENGINE_DIR environment variable to the repository's engine folder.",
        start.map(|p| p.display().to_string()).unwrap_or_else(|| "the app".into()),
        app_dirs().config.join("settings.json").display()
    ))
}

/// The uv program: settings.json "uv", else on PATH, else where the uv installers put it
/// (apps started from the Start menu or at sign-in may not see a PATH changed later).
fn find_uv(configured: Option<&str>, path_var: Option<OsString>, home: Option<PathBuf>) -> PathBuf {
    if let Some(u) = configured.map(str::trim).filter(|u| !u.is_empty()) {
        return PathBuf::from(u);
    }
    let name = if cfg!(windows) { "uv.exe" } else { "uv" };
    let on_path = path_var
        .iter()
        .flat_map(std::env::split_paths)
        .map(|d| d.join(name));
    let usual = home
        .iter()
        .flat_map(|h| [h.join(".local").join("bin"), h.join(".cargo").join("bin")])
        .map(|d| d.join(name));
    on_path
        .chain(usual)
        .find(|p| p.is_file())
        .unwrap_or_else(|| PathBuf::from(name))
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
    dirs: AppDirs,
    child: Arc<Mutex<Option<Child>>>,
    /// The shortcut in use now (starts as settings.hotkey; Settings can change it live).
    hotkey: Mutex<String>,
    /// Why the shortcut doesn't work, when Windows refused it (another program owns it).
    hotkey_error: Mutex<Option<String>>,
    /// The tray's "Open Grain Search (<hotkey>)" item, to update its text after a change.
    open_item: Mutex<Option<MenuItem<tauri::Wry>>>,
    /// Installed app: sets up the Python runtime from the installer's `engine` folder (setup.rs).
    /// None in development, where the engine runs from the repository with `uv run`.
    setup: Option<Arc<setup::Runner>>,
}

impl AppState {
    /// The installed app still has setup work to do (first start, or an update with new libraries).
    fn setup_pending(&self) -> bool {
        self.setup
            .as_ref()
            .map(|r| !matches!(setup::check(&r.paths), Ok(setup::Needed::Nothing)))
            .unwrap_or(false)
    }
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

// -- the service dies with the app (Windows) ---------------------------------------------------
/// The service runs inside a Windows *job object* with "kill on close". Only this app holds the
/// job's handle, so when the app's process ends - Quit, a crash, Task Manager, Ctrl+C in the
/// development terminal - Windows closes the handle and ends everything in the job: `uv` and the
/// Python service it started. Programs the engine opens for the user (Word, a PDF viewer) break
/// away from the job (`opener.py`), so they stay open.
///
/// The few Win32 calls are declared here directly (no extra crate).
#[cfg(windows)]
mod job {
    use std::ffi::c_void;
    use std::io;
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use std::sync::OnceLock;

    type Handle = *mut c_void;

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9; // JobObjectExtendedLimitInformation
    const JOB_OBJECT_LIMIT_BREAKAWAY_OK: u32 = 0x0000_0800;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;

    #[repr(C)]
    #[derive(Default)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    /// JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    #[repr(C)]
    #[derive(Default)]
    struct ExtendedLimitInformation {
        basic_limit_information: BasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    // Same size as the Windows SDK's struct, or the build fails.
    #[cfg(target_pointer_width = "64")]
    const _: () = assert!(std::mem::size_of::<ExtendedLimitInformation>() == 144);
    #[cfg(target_pointer_width = "32")]
    const _: () = assert!(std::mem::size_of::<ExtendedLimitInformation>() == 112);

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: i32, info: *const c_void, len: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn CloseHandle(handle: Handle) -> i32;
    }

    pub struct Job(Handle);

    // The handle is only passed to thread-safe Win32 calls.
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    impl Job {
        pub fn new() -> io::Result<Job> {
            // SAFETY: plain Win32 calls; `info` outlives the call and has the documented layout.
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let job = Job(handle);
                let mut info = ExtendedLimitInformation::default();
                info.basic_limit_information.limit_flags =
                    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
                let ok = SetInformationJobObject(
                    job.0,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    &info as *const ExtendedLimitInformation as *const c_void,
                    std::mem::size_of::<ExtendedLimitInformation>() as u32,
                );
                if ok == 0 {
                    return Err(io::Error::last_os_error()); // `job` is dropped: handle closed
                }
                Ok(job)
            }
        }

        pub fn add(&self, child: &Child) -> io::Result<()> {
            // SAFETY: both handles are valid for the duration of the call.
            let ok = unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as Handle) };
            if ok == 0 {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: we own the handle. Closing the last handle ends the processes in the job.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }

    /// The app-wide job. Kept in a static that is never dropped, so the handle stays open
    /// until the app's process ends - that is the whole point.
    pub fn add_to_app_job(child: &Child) -> Result<(), String> {
        static APP_JOB: OnceLock<Result<Job, String>> = OnceLock::new();
        match APP_JOB.get_or_init(|| Job::new().map_err(|e| e.to_string())) {
            Ok(job) => job.add(child).map_err(|e| e.to_string()),
            Err(e) => Err(e.clone()),
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::process::{Command, Stdio};
        use std::time::{Duration, Instant};

        fn exits_within(child: &mut Child, limit: Duration) -> bool {
            let deadline = Instant::now() + limit;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            false
        }

        fn long_running() -> Child {
            // ~30 s; output discarded
            Command::new("ping")
                .args(["-n", "30", "127.0.0.1"])
                .stdout(Stdio::null())
                .spawn()
                .expect("start ping")
        }

        #[test]
        fn closing_the_job_ends_its_processes() {
            let job = Job::new().expect("create job");
            let mut child = long_running();
            job.add(&child).expect("assign to job");
            assert!(!exits_within(&mut child, Duration::from_millis(500)));
            drop(job); // what Windows does when the app's process ends
            assert!(
                exits_within(&mut child, Duration::from_secs(5)),
                "the process should end with the job"
            );
        }

        #[test]
        fn processes_outside_the_job_are_left_alone() {
            let job = Job::new().expect("create job");
            let mut child = long_running();
            drop(job);
            assert!(!exits_within(&mut child, Duration::from_secs(1)));
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

// -- talking to the service (tiny HTTP client, 127.0.0.1 only) ---------------------------
fn read_info(service_file: &Path) -> Option<ServiceInfo> {
    let text = fs::read_to_string(service_file).ok()?;
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

/// How the engine is started.
enum Launch {
    /// Development: `uv run grain serve` in the repository's engine folder.
    Dev(PathBuf),
    /// Installed app: the runtime the setup window made (`python -m grain_search serve`).
    Installed(setup::Paths),
}

/// Development settings (engine_dir, GRAIN_SEARCH_ENGINE_DIR) win; otherwise the installed
/// runtime; otherwise the repository next to a development build.
fn launch_mode(settings: &Settings, installed: Option<&setup::Paths>) -> Result<Launch, String> {
    let env_dir = std::env::var("GRAIN_SEARCH_ENGINE_DIR").ok();
    let explicit = settings.engine_dir.is_some() || env_dir.is_some();
    if let (false, Some(paths)) = (explicit, installed) {
        return match setup::check(paths) {
            Ok(setup::Needed::Nothing) => Ok(Launch::Installed(paths.clone())),
            Ok(_) => {
                Err("Grain Search is still being set up: finish it in the setup window.".into())
            }
            Err(e) => Err(format!(
                "The installation is incomplete ({e}). Reinstall Grain Search."
            )),
        };
    }
    let dir = find_engine_dir(
        settings.engine_dir.as_deref(),
        env_dir,
        std::env::current_exe().ok(),
    )?;
    Ok(Launch::Dev(dir))
}

fn spawn_service(s: &Settings, launch: &Launch, dirs: &AppDirs) -> Result<Child, String> {
    let log_path = dirs.log_file();
    if let Some(logs) = log_path.parent() {
        fs::create_dir_all(logs).map_err(|e| format!("Cannot create {}: {e}", logs.display()))?;
    }
    let mut log = fs::File::create(&log_path)
        .map_err(|e| format!("Cannot write {}: {e}", log_path.display()))?;
    let (mut cmd, what) = match launch {
        Launch::Dev(dir) => {
            let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
                .map(PathBuf::from);
            let uv = find_uv(s.uv.as_deref(), std::env::var_os("PATH"), home);
            let mut cmd = Command::new(&uv);
            cmd.args(["run", "grain", "serve"]).current_dir(dir);
            (
                cmd,
                format!("{} run grain serve  (in {})", uv.display(), dir.display()),
            )
        }
        Launch::Installed(paths) => {
            let mut cmd = Command::new(paths.python());
            cmd.args(["-m", "grain_search", "serve"])
                .current_dir(&paths.runtime)
                .env("PYTHONNOUSERSITE", "1"); // never mix in packages from the user's own Python
            (
                cmd,
                format!("{} -m grain_search serve", paths.python().display()),
            )
        }
    };
    let _ = writeln!(log, "Starting: {what}");
    let log2 = log.try_clone().map_err(|e| e.to_string())?;
    cmd.env("PYTHONUNBUFFERED", "1")
        // the engine also watches this process and shuts down cleanly when it is gone
        .env("GRAIN_SEARCH_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log2);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn().map_err(|e| match launch {
        Launch::Dev(_) => format!(
            "Could not start `{what}`: {e}. Is uv installed? You can set \"uv\" to its full path in settings.json."
        ),
        Launch::Installed(_) => format!("Could not start the search engine ({e}). Run the setup again: reinstall Grain Search."),
    })
}

/// Makes sure the search service is up (starting it if needed) and returns its address + token.
#[tauri::command]
async fn ensure_service(state: tauri::State<'_, AppState>) -> Result<ServiceInfo, String> {
    let settings = state.settings.clone();
    let dirs = state.dirs.clone();
    let child = state.child.clone();
    let installed = state.setup.as_ref().map(|r| r.paths.clone());
    tauri::async_runtime::spawn_blocking(move || {
        let service_file = dirs.service_file();
        if let Some(info) = read_info(&service_file) {
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
                let launch = launch_mode(&settings, installed.as_ref())?;
                let started = spawn_service(&settings, &launch, &dirs)?;
                #[cfg(windows)]
                if let Err(e) = job::add_to_app_job(&started) {
                    // Not fatal: the engine still stops itself when the app is gone.
                    eprintln!("Could not tie the search service to the app: {e}");
                }
                *guard = Some(started);
            }
        }
        let deadline = Instant::now() + SERVICE_START_TIMEOUT;
        loop {
            if let Some(info) = read_info(&service_file) {
                if healthy(&info) {
                    return Ok(info);
                }
            }
            if let Some(c) = child.lock().unwrap().as_mut() {
                if let Ok(Some(code)) = c.try_wait() {
                    return Err(format!(
                        "The search service stopped ({code}). Details: {}",
                        dirs.log_file().display()
                    ));
                }
            }
            if Instant::now() > deadline {
                return Err(format!(
                    "The search service took too long to start (see {}).",
                    dirs.log_file().display()
                ));
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
        if let Some(info) = read_info(&state.dirs.service_file()) {
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
    if let Some(info) = read_info(&state.dirs.service_file()) {
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
    if app.state::<AppState>().setup_pending() {
        let _ = show_setup(app); // nothing to search until the setup has finished
        return;
    }
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

/// Opens the Settings window (folders, exclusions, indexing), or brings it to the front.
/// Must be `async`: creating a window from a synchronous command deadlocks on Windows
/// (the WebView2 setup waits for the thread the command is blocking).
#[tauri::command]
async fn open_settings(app: AppHandle) -> Result<(), String> {
    show_settings(&app).map_err(|e| e.to_string())
}

fn focus_settings(w: &WebviewWindow) -> tauri::Result<()> {
    let _ = w.unminimize();
    w.show()?;
    w.set_focus()
}

fn show_settings(app: &AppHandle) -> tauri::Result<()> {
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.hide();
    }
    if let Some(w) = app.get_webview_window(SETTINGS) {
        return focus_settings(&w);
    }
    // Same page as the search bar; a startup script tells it to show Settings
    // (no "#settings" in the URL: some setups treat it as part of the file name).
    let built = WebviewWindowBuilder::new(app, SETTINGS, WebviewUrl::App("index.html".into()))
        .title("Grain Search Settings")
        .initialization_script("window.__GRAIN_VIEW__ = 'settings';")
        .inner_size(760.0, 640.0)
        .min_inner_size(560.0, 460.0)
        .center()
        .build();
    match built {
        Ok(_) => Ok(()),
        // two clicks at once: the other one created it - just bring it forward
        Err(e) => match app.get_webview_window(SETTINGS) {
            Some(w) => focus_settings(&w),
            None => Err(e),
        },
    }
}

/// The standard Windows folder picker. Returns the chosen folder, or None if cancelled.
#[tauri::command]
async fn pick_folder(app: AppHandle, title: Option<String>) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_title(title.unwrap_or_else(|| "Choose a folder".into()))
            .blocking_pick_folder()
            .and_then(|p| p.into_path().ok())
            .map(|p| p.to_string_lossy().into_owned())
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn get_settings(state: tauri::State<'_, AppState>) -> UiSettings {
    UiSettings {
        glass: state.settings.glass,
        hotkey: state.hotkey.lock().unwrap().clone(),
        hotkey_error: state.hotkey_error.lock().unwrap().clone(),
    }
}

// -- first-run setup (installed app) -----------------------------------------------------------
/// The small "Setting up Grain Search" window (setup.rs does the work).
fn show_setup(app: &AppHandle) -> tauri::Result<()> {
    if let Some(w) = app.get_webview_window(SETUP) {
        let _ = w.unminimize();
        w.show()?;
        return w.set_focus();
    }
    WebviewWindowBuilder::new(app, SETUP, WebviewUrl::App("index.html".into()))
        .title("Setting up Grain Search")
        .initialization_script("window.__GRAIN_VIEW__ = 'setup';")
        .inner_size(520.0, 540.0)
        .resizable(false)
        .center()
        .build()
        .map(|_| ())
}

#[tauri::command]
fn setup_status(state: tauri::State<'_, AppState>) -> setup::Status {
    state
        .setup
        .as_ref()
        .map(|r| r.status.lock().unwrap().clone())
        .unwrap_or_default()
}

/// Starts (or retries) the setup in the background; progress arrives as `grain://setup` events.
/// When it has finished, the setup window closes and Settings opens (AI model -> Download).
#[tauri::command]
async fn setup_start(app: AppHandle) -> Result<(), String> {
    let runner = app
        .state::<AppState>()
        .setup
        .clone()
        .ok_or("Nothing to set up: this is a development build.")?;
    {
        let mut st = runner.status.lock().unwrap();
        if st.running {
            return Ok(()); // already going (e.g. the window asked twice)
        }
        st.running = true;
    }
    std::thread::spawn(move || {
        let report = |st: &setup::Status| {
            let _ = app.emit("grain://setup", st);
        };
        if runner.run(&report).is_ok() {
            std::thread::sleep(Duration::from_millis(900)); // let the last ✓ show
            if let Some(w) = app.get_webview_window(SETUP) {
                let _ = w.close();
            }
            let _ = show_settings(&app);
        }
    });
    Ok(())
}

#[tauri::command]
fn setup_cancel(state: tauri::State<'_, AppState>) {
    if let Some(r) = &state.setup {
        r.cancel();
    }
}

// -- keyboard shortcut ----------------------------------------------------------------------
const DEFAULT_HOTKEY: &str = "Alt+Space";

/// Checks a shortcut typed or recorded in Settings and writes it the one standard way
/// ("Ctrl+Alt+Shift+K"). At least one of Ctrl/Alt/Shift is required (a bare key would take over
/// normal typing everywhere); the Windows key is not allowed (Windows reserves most Win+ combos).
fn check_hotkey(text: &str) -> Result<String, String> {
    let (mut ctrl, mut alt, mut shift) = (false, false, false);
    let mut key: Option<String> = None;
    for part in text.split('+').map(str::trim).filter(|p| !p.is_empty()) {
        match part.to_ascii_lowercase().as_str() {
            "ctrl" | "control" => ctrl = true,
            "alt" => alt = true,
            "shift" => shift = true,
            "win" | "super" | "meta" | "cmd" | "command" => {
                return Err("The Windows key can't be used in the shortcut.".into())
            }
            k => {
                if key.is_some() {
                    return Err("Use one key plus Ctrl, Alt or Shift.".into());
                }
                let normal = if k == "space" {
                    "Space".to_string()
                } else if k.len() == 1 && k.chars().all(|c| c.is_ascii_alphanumeric()) {
                    k.to_ascii_uppercase()
                } else if let Some(n) = k.strip_prefix('f').and_then(|n| n.parse::<u8>().ok()) {
                    if !(1..=12).contains(&n) {
                        return Err(format!("{part} can't be used; pick F1 to F12."));
                    }
                    format!("F{n}")
                } else {
                    return Err(format!(
                        "{part} can't be used; pick a letter, a digit, Space or F1 to F12."
                    ));
                };
                key = Some(normal);
            }
        }
    }
    let key = key.ok_or("Add a key to Ctrl, Alt or Shift, for example Ctrl+Space.")?;
    if !(ctrl || alt || shift) {
        return Err("Use at least one of Ctrl, Alt or Shift.".into());
    }
    let mut parts: Vec<&str> = Vec::new();
    if ctrl {
        parts.push("Ctrl");
    }
    if alt {
        parts.push("Alt");
    }
    if shift {
        parts.push("Shift");
    }
    parts.push(&key);
    Ok(parts.join("+"))
}

/// Writes the shortcut into settings.json, keeping every other setting in the file as it is.
fn save_hotkey(file: &Path, hotkey: &str) -> Result<(), String> {
    let mut obj = match fs::read_to_string(file) {
        Ok(text) => match serde_json::from_str::<serde_json::Value>(&text) {
            Ok(serde_json::Value::Object(map)) => map,
            _ => {
                return Err(format!(
                    "{} isn't valid JSON; fix or delete it.",
                    file.display()
                ))
            }
        },
        Err(_) => serde_json::Map::new(), // no file yet
    };
    obj.insert("hotkey".into(), serde_json::Value::String(hotkey.into()));
    if let Some(dir) = file.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let text =
        serde_json::to_string_pretty(&serde_json::Value::Object(obj)).map_err(|e| e.to_string())?;
    let tmp = file.with_extension("json.tmp");
    fs::write(&tmp, text + "\n").map_err(|e| e.to_string())?;
    fs::rename(&tmp, file).map_err(|e| e.to_string())
}

fn taken_message(hotkey: &str) -> String {
    format!("{hotkey} is already used by another program, so it won't respond.")
}

/// Settings -> Keyboard shortcut. Tries the new shortcut first; only if Windows accepts it is the
/// old one released and the new one saved. If another program owns it, nothing changes.
/// Async on purpose: the shortcut library registers on the main thread, and a plain (sync)
/// command runs on the main thread itself, which would freeze the app.
#[tauri::command]
async fn set_hotkey(app: AppHandle, hotkey: String) -> Result<String, String> {
    let wanted = check_hotkey(&hotkey)?;
    let state = app.state::<AppState>();
    let current = state.hotkey.lock().unwrap().clone();
    let current_works = state.hotkey_error.lock().unwrap().is_none();
    if wanted.eq_ignore_ascii_case(&current) && current_works {
        return Ok(current);
    }
    let shortcuts = app.global_shortcut();
    if let Err(e) = shortcuts.register(wanted.as_str()) {
        eprintln!("register {wanted}: {e}");
        let keep = if current_works {
            format!(" Your shortcut stays {current}.")
        } else {
            String::new()
        };
        return Err(taken_message(&wanted) + &keep);
    }
    if current_works && !wanted.eq_ignore_ascii_case(&current) {
        let _ = shortcuts.unregister(current.as_str());
    }
    *state.hotkey.lock().unwrap() = wanted.clone();
    *state.hotkey_error.lock().unwrap() = None;
    if let Err(e) = save_hotkey(&state.dirs.config.join("settings.json"), &wanted) {
        eprintln!("Could not save the shortcut: {e}"); // works for now; back to the old one after a restart
    }
    if let Some(item) = state.open_item.lock().unwrap().as_ref() {
        let _ = item.set_text(format!("Open Grain Search ({wanted})"));
    }
    if let Some(tray) = app.tray_by_id("main") {
        let _ = tray.set_tooltip(Some(format!("Grain Search ({wanted})")));
    }
    Ok(wanted)
}

// -- tray ------------------------------------------------------------------------------------
fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let hotkey = app.state::<AppState>().hotkey.lock().unwrap().clone();
    let open = MenuItem::with_id(
        app,
        "open",
        format!("Open Grain Search ({hotkey})"),
        true,
        None::<&str>,
    )?;
    *app.state::<AppState>().open_item.lock().unwrap() = Some(open.clone());
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "Pause indexing", true, None::<&str>)?;
    let resume = MenuItem::with_id(app, "resume", "Resume indexing", true, None::<&str>)?;
    let rescan = MenuItem::with_id(app, "rescan", "Re-scan all folders", true, None::<&str>)?;
    // Free GPU memory by hand; Alt+Space brings back whichever was unloaded.
    let unload = MenuItem::with_id(app, "unload", "Unload model", true, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "Stop engine", true, None::<&str>)?;
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
    let s4 = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &open, &settings, &s1, &pause, &resume, &rescan, &s2, &unload, &stop, &s3, &autostart,
            &s4, &quit,
        ],
    )?;

    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip(format!("Grain Search ({hotkey})"))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show(app),
            "settings" => {
                let _ = show_settings(app);
            }
            "pause" => service_post(app, "/index/pause"),
            "resume" => service_post(app, "/index/resume"),
            "rescan" => service_post(app, "/index/rescan"),
            // both can take a few seconds: keep the tray responsive
            "unload" => {
                let app = app.clone();
                std::thread::spawn(move || service_post(&app, "/model/unload"));
            }
            "stop" => {
                let app = app.clone();
                std::thread::spawn(move || stop_service(&app));
            }
            "autostart" => {
                let al = app.autolaunch();
                let _ = if al.is_enabled().unwrap_or(false) {
                    al.disable()
                } else {
                    al.enable()
                };
            }
            "quit" => {
                if let Some(r) = &app.state::<AppState>().setup {
                    r.cancel(); // an unfinished setup continues next time
                }
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
        .plugin(tauri_plugin_dialog::init())
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
            let dirs = app_dirs();
            let settings = load_settings(&dirs);
            // Installed app: the installer put an `engine` folder (runtime.json, uv, the engine's
            // code) next to the program. Development builds keep running the repository's engine
            // (set GRAIN_SEARCH_USE_RUNTIME=1 to try the installed way with `tauri dev`).
            let bundle = app
                .path()
                .resource_dir()
                .ok()
                .map(|d| d.join("engine"))
                .filter(|d| d.join("runtime.json").is_file());
            let use_bundle = cfg!(not(debug_assertions))
                || std::env::var_os("GRAIN_SEARCH_USE_RUNTIME").is_some();
            let setup_runner = bundle.filter(|_| use_bundle).map(|bundle| {
                Arc::new(setup::Runner {
                    paths: setup::Paths {
                        bundle,
                        runtime: dirs.data.join("runtime"),
                    },
                    status: Arc::new(Mutex::new(setup::Status::default())),
                    child: Arc::new(Mutex::new(None)),
                    cancelled: Arc::new(Mutex::new(false)),
                })
            });
            app.manage(AppState {
                settings: settings.clone(),
                dirs,
                child: Arc::new(Mutex::new(None)),
                hotkey: Mutex::new(settings.hotkey.clone()),
                hotkey_error: Mutex::new(None),
                open_item: Mutex::new(None),
                setup: setup_runner,
            });

            if let Err(e) = handle.global_shortcut().register(settings.hotkey.as_str()) {
                eprintln!("register {}: {e}", settings.hotkey);
                *handle.state::<AppState>().hotkey_error.lock().unwrap() =
                    Some(taken_message(&settings.hotkey));
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

            // first manual launch: show the bar so you can see it works; autostart launches stay hidden.
            // Setup still to do (first start after installing, or an update): always show it.
            if handle.state::<AppState>().setup_pending() {
                show_setup(&handle)?;
            } else if !std::env::args().any(|a| a == "--hidden") {
                show(&handle);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return; // the Settings window behaves like a normal window
            }
            match event {
                // click anywhere else -> hide the search bar, like Spotlight
                WindowEvent::Focused(false) => {
                    let _ = window.hide();
                }
                WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = window.hide();
                }
                _ => {}
            }
        })
        .invoke_handler(tauri::generate_handler![
            ensure_service,
            get_settings,
            hide_window,
            open_settings,
            pick_folder,
            set_height,
            set_hotkey,
            setup_cancel,
            setup_start,
            setup_status
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
        assert!(s.engine_dir.is_none());
        assert!(s.uv.is_none());
        // the old name still works
        let old: Settings = serde_json::from_str(r#"{"project_dir": "D:/x/engine"}"#).unwrap();
        assert_eq!(old.engine_dir.as_deref(), Some("D:/x/engine"));
    }

    #[test]
    fn hotkey_is_checked_and_written_one_way() {
        assert_eq!(check_hotkey("alt+space").unwrap(), "Alt+Space");
        assert_eq!(check_hotkey(" shift + ctrl + k ").unwrap(), "Ctrl+Shift+K");
        assert_eq!(check_hotkey("Control+Alt+f5").unwrap(), "Ctrl+Alt+F5");
        assert_eq!(check_hotkey("Ctrl+1").unwrap(), "Ctrl+1");
        assert_eq!(check_hotkey(DEFAULT_HOTKEY).unwrap(), DEFAULT_HOTKEY);
    }

    #[test]
    fn hotkey_rules() {
        let err = |s: &str| check_hotkey(s).unwrap_err();
        assert!(
            err("K").contains("Ctrl, Alt or Shift"),
            "a bare key would steal typing"
        );
        assert!(err("Win+K").contains("Windows key"));
        assert!(err("Ctrl+Alt").contains("Add a key"));
        assert!(err("Ctrl+K+J").contains("one key"));
        assert!(err("Ctrl+F13").contains("F1 to F12"));
        assert!(err("Ctrl+Enter").contains("can't be used"));
        assert!(err("").contains("Add a key"));
    }

    #[test]
    fn saving_the_hotkey_keeps_other_settings() {
        let t = TempTree::new("hotkey");
        let file = t.0.join("GrainSearch").join("settings.json");
        save_hotkey(&file, "Ctrl+Space").unwrap(); // no file yet: created
        let v: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&file).unwrap()).unwrap();
        assert_eq!(v["hotkey"], "Ctrl+Space");

        fs::write(
            &file,
            r#"{"engine_dir": "D:/x/engine", "glass": true, "hotkey": "Alt+Space"}"#,
        )
        .unwrap();
        save_hotkey(&file, "Ctrl+Shift+K").unwrap();
        let v: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&file).unwrap()).unwrap();
        assert_eq!(v["hotkey"], "Ctrl+Shift+K");
        assert_eq!(v["engine_dir"], "D:/x/engine");
        assert_eq!(v["glass"], true);
        let s: Settings = serde_json::from_value(v).unwrap(); // still readable by load_settings
        assert_eq!(s.hotkey, "Ctrl+Shift+K");

        fs::write(&file, "{ not json").unwrap();
        assert!(save_hotkey(&file, "Ctrl+K")
            .unwrap_err()
            .contains("valid JSON"));
        assert_eq!(
            fs::read_to_string(&file).unwrap(),
            "{ not json",
            "a broken file is never overwritten"
        );
    }

    /// A temporary folder tree; removed when dropped.
    struct TempTree(PathBuf);
    impl TempTree {
        fn new(name: &str) -> Self {
            let p = std::env::temp_dir().join(format!("grain-{name}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&p);
            fs::create_dir_all(&p).unwrap();
            TempTree(p)
        }
        fn engine(&self, rel: &str) -> PathBuf {
            let e = self.0.join(rel);
            fs::create_dir_all(e.join("src").join("grain_search")).unwrap();
            fs::write(e.join("pyproject.toml"), "[project]\n").unwrap();
            e
        }
        fn file(&self, rel: &str) -> PathBuf {
            let f = self.0.join(rel);
            fs::create_dir_all(f.parent().unwrap()).unwrap();
            fs::write(&f, "").unwrap();
            f
        }
    }
    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn same(a: &Path, b: &Path) -> bool {
        a.canonicalize().unwrap() == b.canonicalize().unwrap()
    }

    #[test]
    fn engine_found_from_a_development_build() {
        let t = TempTree::new("devbuild");
        let engine = t.engine("repo/engine");
        let exe = t.file("repo/app/src-tauri/target/debug/grain-search.exe");
        let found = find_engine_dir(None, None, Some(exe)).unwrap();
        assert!(same(&found, &engine));
        assert!(!found.to_string_lossy().starts_with(r"\\?\"));
    }

    #[test]
    fn engine_found_next_to_the_installed_app() {
        let t = TempTree::new("installed");
        let engine = t.engine("Grain Search/engine");
        let exe = t.file("Grain Search/grain-search.exe");
        assert!(same(
            &find_engine_dir(None, None, Some(exe)).unwrap(),
            &engine
        ));
    }

    #[test]
    fn engine_setting_wins_and_is_checked() {
        let t = TempTree::new("setting");
        let a = t.engine("a/engine");
        let b = t.engine("b/engine");
        let exe = t.file("a/app.exe");
        let pick = |c: Option<&str>, e: Option<&str>| {
            find_engine_dir(c, e.map(String::from), Some(exe.clone()))
        };
        let b_str = b.to_string_lossy().to_string();
        assert!(same(&pick(Some(&b_str), None).unwrap(), &b));
        assert!(same(&pick(None, Some(&b_str)).unwrap(), &b));
        assert!(
            same(&pick(Some("  "), None).unwrap(), &a),
            "blank means automatic"
        );
        let err = pick(Some(&t.0.join("nope").to_string_lossy()), None).unwrap_err();
        assert!(err.contains("engine_dir"), "{err}");
    }

    #[test]
    fn engine_missing_gives_a_helpful_error() {
        let t = TempTree::new("missing");
        let exe = t.file("somewhere/app.exe");
        let err = find_engine_dir(None, None, Some(exe)).unwrap_err();
        assert!(err.contains("GRAIN_SEARCH_ENGINE_DIR"), "{err}");
    }

    #[test]
    fn uv_from_setting_path_or_usual_places() {
        let t = TempTree::new("uv");
        let name = if cfg!(windows) { "uv.exe" } else { "uv" };
        assert_eq!(
            find_uv(Some(r"C:\tools\uv.exe"), None, None),
            PathBuf::from(r"C:\tools\uv.exe")
        );
        // not on PATH, not installed: fall back to the bare name (the error message explains)
        assert_eq!(find_uv(None, None, None), PathBuf::from(name));
        // installed by the official installer in ~/.local/bin, which is not on PATH
        let home = t.0.join("home");
        let installed = t.file(&format!("home/.local/bin/{name}"));
        assert_eq!(find_uv(None, None, Some(home.clone())), installed);
        // PATH comes first
        let on_path = t.file(&format!("tools/{name}"));
        let path = std::env::join_paths([t.0.join("empty"), t.0.join("tools")]).unwrap();
        assert_eq!(find_uv(None, Some(path), Some(home)), on_path);
    }

    #[test]
    fn read_info_parses_service_file() {
        let dir = std::env::temp_dir().join(format!("grain-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join("service.json");
        assert!(read_info(&file).is_none());
        fs::write(
            &file,
            r#"{"url": "http://127.0.0.1:8766", "token": "abc", "pid": 1}"#,
        )
        .unwrap();
        let info = read_info(&file).expect("service.json should parse");
        assert_eq!(info.url, "http://127.0.0.1:8766");
        assert_eq!(info.token, "abc");
        let _ = fs::remove_dir_all(&dir);
    }

    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| {
            pairs
                .iter()
                .find(|(key, _)| *key == k)
                .map(|(_, v)| v.to_string())
        }
    }

    // Same cases as engine/tests/test_config.py::AppPathsTest - the two sides must agree.
    #[test]
    fn app_dirs_windows() {
        let d = resolve_app_dirs(
            env(&[("APPDATA", r"C:\U\R"), ("LOCALAPPDATA", r"C:\U\L")]),
            true,
        );
        assert_eq!(d.config, PathBuf::from(r"C:\U\R").join("GrainSearch"));
        assert_eq!(d.data, PathBuf::from(r"C:\U\L").join("GrainSearch"));
        assert_eq!(d.service_file(), d.data.join("service.json"));
        assert_eq!(d.log_file(), d.data.join("logs").join("service.log"));
    }

    #[test]
    fn app_dirs_windows_fallback_to_userprofile() {
        let d = resolve_app_dirs(env(&[("USERPROFILE", r"C:\Users\me")]), true);
        assert!(d.config.to_string_lossy().contains("Roaming"));
        assert!(d.data.to_string_lossy().contains("Local"));
    }

    #[test]
    fn app_dirs_posix_follows_xdg() {
        let d = resolve_app_dirs(env(&[("HOME", "/home/me")]), false);
        assert_eq!(d.config, PathBuf::from("/home/me/.config/grain-search"));
        assert_eq!(d.data, PathBuf::from("/home/me/.local/share/grain-search"));
        let d = resolve_app_dirs(
            env(&[
                ("HOME", "/home/me"),
                ("XDG_CONFIG_HOME", "/c"),
                ("XDG_DATA_HOME", "/d"),
            ]),
            false,
        );
        assert_eq!(d.config, PathBuf::from("/c/grain-search"));
        assert_eq!(d.data, PathBuf::from("/d/grain-search"));
    }

    #[test]
    fn app_dirs_home_override() {
        for win in [true, false] {
            let d = resolve_app_dirs(
                env(&[("GRAIN_SEARCH_HOME", "/x/profile"), ("APPDATA", "/nope")]),
                win,
            );
            assert_eq!(d.config, PathBuf::from("/x/profile"));
            assert_eq!(d.data, PathBuf::from("/x/profile"));
        }
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
