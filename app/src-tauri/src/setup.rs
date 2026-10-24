//! First-run setup of the installed app (roadmap Phase 4).
//!
//! The installer stays small (~15 MB): next to the app it puts an `engine` folder with our own
//! code (`grain_search-*.whl`), `uv`, `requirements.txt` (every library pinned from uv.lock) and
//! `runtime.json` (Python and PyTorch versions). On first start the setup window runs these steps
//! with uv, into %LOCALAPPDATA%\GrainSearch\runtime:
//!
//!   1. GPU check     nvidia-smi names the NVIDIA GPU -> PyTorch's CUDA build, otherwise the CPU build
//!   2. disk check    enough free space for that build
//!   3. Python        `uv venv` downloads Python into runtime\python and makes runtime\.venv
//!   4. PyTorch       `uv pip install torch==… torchvision==…` from PyTorch's CUDA or CPU site
//!   5. libraries     `uv pip install -r requirements.txt` (keeps the PyTorch from step 4)
//!   6. engine        `uv pip install grain_search-….whl`
//!   7. setup.json    what was installed, so later starts (and updates) know what to redo
//!
//! PyTorch must come before the libraries: sentence-transformers needs torch, and if it isn't
//! there yet uv takes it from PyPI, which only has the CPU build for Windows. The PyTorch step
//! would then find "torch 2.11.0" already installed and do nothing. After a CUDA setup we also ask
//! the installed PyTorch whether it really is the CUDA build.
//!
//! uv keeps every download in runtime\cache, so Retry only fetches what is still missing.
//! A wrong GPU guess is harmless: the CUDA build also runs on the CPU.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// runtime.json, written by scripts/runtime_bundle.py.
#[derive(Clone, Debug, Deserialize)]
pub struct RuntimeInfo {
    pub python: String,
    pub torch: String,
    pub torchvision: String,
    pub torch_index: TorchIndex,
    pub wheel: String,
    pub requirements: String,
    pub fingerprint: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TorchIndex {
    pub cuda: String,
    pub cpu: String,
}

/// runtime\setup.json: what the last complete setup installed.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Marker {
    pub fingerprint: String,
    pub wheel: String,
    /// "cuda" | "cpu": only for people reading setup.json (troubleshooting)
    #[allow(dead_code)]
    pub build: String,
    /// SETUP_REV of the setup that wrote it (missing in files from before it existed = 0).
    #[serde(default)]
    pub setup: u32,
}

/// Raise when the setup steps change in a way that existing installs must redo: a lower number in
/// setup.json means a full setup on the next start.
/// 1 = first version (installed the CPU PyTorch on GPU PCs); 2 = PyTorch before the libraries.
pub const SETUP_REV: u32 = 2;

/// The uv steps, in order.
pub const INSTALL_STEPS: [&str; 4] = ["python", "pytorch", "libraries", "engine"];

/// Where everything is.
#[derive(Clone, Debug)]
pub struct Paths {
    /// The installer's `engine` folder (next to the app).
    pub bundle: PathBuf,
    /// %LOCALAPPDATA%\GrainSearch\runtime
    pub runtime: PathBuf,
}

impl Paths {
    pub fn venv(&self) -> PathBuf {
        self.runtime.join(".venv")
    }
    pub fn python(&self) -> PathBuf {
        if cfg!(windows) {
            self.venv().join("Scripts").join("python.exe")
        } else {
            self.venv().join("bin").join("python")
        }
    }
    pub fn marker(&self) -> PathBuf {
        self.runtime.join("setup.json")
    }
    pub fn uv(&self) -> PathBuf {
        self.bundle
            .join(if cfg!(windows) { "uv.exe" } else { "uv" })
    }
    pub fn info(&self) -> Result<RuntimeInfo, String> {
        let file = self.bundle.join("runtime.json");
        let text =
            std::fs::read_to_string(&file).map_err(|e| format!("{}: {e}", file.display()))?;
        serde_json::from_str(&text).map_err(|e| format!("{}: {e}", file.display()))
    }
    fn read_marker(&self) -> Option<Marker> {
        let text = std::fs::read_to_string(self.marker()).ok()?;
        serde_json::from_str(&text).ok()
    }
}

/// What the next start has to do.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Needed {
    Nothing,
    /// Only our own code changed (an update without new libraries): reinstall the wheel, seconds.
    EngineOnly,
    /// First start, an interrupted setup, or an update with new libraries.
    Full,
}

pub fn needed(
    fingerprint: &str,
    wheel: &str,
    marker: Option<&Marker>,
    python_exists: bool,
) -> Needed {
    match marker {
        Some(m) if python_exists && m.fingerprint == fingerprint && m.setup >= SETUP_REV => {
            if m.wheel == wheel {
                Needed::Nothing
            } else {
                Needed::EngineOnly
            }
        }
        _ => Needed::Full,
    }
}

pub fn check(paths: &Paths) -> Result<Needed, String> {
    let info = paths.info()?;
    Ok(needed(
        &info.fingerprint,
        &info.wheel,
        paths.read_marker().as_ref(),
        paths.python().is_file(),
    ))
}

/// First line of `nvidia-smi --query-gpu=name --format=csv,noheader`, e.g. "NVIDIA GeForce RTX 3070".
pub fn parse_gpu(stdout: &str) -> Option<String> {
    stdout
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(str::to_string)
}

fn hidden(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd
}

/// The NVIDIA GPU's name, if there is one with a working driver (the driver ships nvidia-smi).
pub fn detect_gpu() -> Option<String> {
    let out =
        hidden(Command::new("nvidia-smi").args(["--query-gpu=name", "--format=csv,noheader"]))
            .stdin(Stdio::null())
            .output()
            .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_gpu(&String::from_utf8_lossy(&out.stdout))
}

/// "cuda" or "cpu": which PyTorch build `python` has (None if it can't be asked).
fn torch_build(python: &Path) -> Option<&'static str> {
    let out = hidden(Command::new(python).args([
        "-c",
        "import torch; print('cuda' if torch.version.cuda else 'cpu')",
    ]))
    .stdin(Stdio::null())
    .output()
    .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_torch_build(&String::from_utf8_lossy(&out.stdout))
}

pub fn parse_torch_build(stdout: &str) -> Option<&'static str> {
    match stdout.trim() {
        "cuda" => Some("cuda"),
        "cpu" => Some("cpu"),
        _ => None,
    }
}

const GB: u64 = 1024 * 1024 * 1024;

/// Room needed in the runtime folder: Python + libraries + PyTorch, plus uv's cache of the downloads.
pub fn space_needed(cuda: bool) -> u64 {
    if cuda {
        7 * GB
    } else {
        2 * GB
    }
}

/// Free bytes on the drive holding `path` (or its nearest existing parent). None if unknown.
pub fn free_space(path: &Path) -> Option<u64> {
    let existing = path.ancestors().find(|p| p.exists())?;
    free_space_at(existing)
}

#[cfg(windows)]
fn free_space_at(dir: &Path) -> Option<u64> {
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "kernel32")]
    extern "system" {
        fn GetDiskFreeSpaceExW(
            dir: *const u16,
            free: *mut u64,
            total: *mut u64,
            total_free: *mut u64,
        ) -> i32;
    }
    let wide: Vec<u16> = dir.as_os_str().encode_wide().chain(Some(0)).collect();
    let (mut free, mut total, mut total_free) = (0u64, 0u64, 0u64);
    // SAFETY: a valid NUL-terminated path and three valid out-pointers for the duration of the call.
    let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut free, &mut total, &mut total_free) };
    (ok != 0).then_some(free)
}

#[cfg(not(windows))]
fn free_space_at(_dir: &Path) -> Option<u64> {
    None // only the Windows app has an installer; elsewhere the check is skipped
}

pub fn gb(bytes: u64) -> String {
    format!("{:.1} GB", bytes as f64 / GB as f64)
}

/// The last useful line of uv's error output, in plain words where we recognise it.
pub fn explain_failure(stderr: &str) -> String {
    let lines: Vec<&str> = stderr
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    let all = stderr.to_ascii_lowercase();
    let last = lines.last().copied().unwrap_or("");
    let last = last
        .trim_start_matches("error:")
        .trim_start_matches("Caused by:")
        .trim();
    if [
        "dns error",
        "failed to fetch",
        "connect",
        "timed out",
        "network",
        "tls",
    ]
    .iter()
    .any(|k| all.contains(k))
    {
        return format!(
            "Couldn't download: check your internet connection and try again. ({last})"
        );
    }
    if all.contains("no space left")
        || all.contains("not enough space")
        || all.contains("os error 112")
    {
        return "The drive is full: free some space and try again.".into();
    }
    if last.is_empty() {
        "Setup stopped without saying why.".into()
    } else {
        last.to_string()
    }
}

// -- progress, shared with the setup window ---------------------------------------------------
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Step {
    pub id: &'static str,
    pub label: String,
    pub state: &'static str, // pending | running | done | failed | skipped
}

#[derive(Clone, Debug, Serialize, Default)]
pub struct Status {
    pub running: bool,
    pub done: bool,
    pub steps: Vec<Step>,
    pub gpu: Option<String>,
    pub build: Option<&'static str>,
    pub downloaded_mb: u64,
    pub error: Option<String>,
}

pub fn initial_steps() -> Vec<Step> {
    [
        ("gpu", "Check for an NVIDIA GPU"),
        ("disk", "Check free disk space"),
        ("python", "Download Python"),
        ("pytorch", "Download PyTorch"),
        ("libraries", "Download libraries"),
        ("engine", "Install Grain Search's engine"),
    ]
    .into_iter()
    .map(|(id, label)| Step {
        id,
        label: label.to_string(),
        state: "pending",
    })
    .collect()
}

/// The uv command for one install step.
pub fn uv_args(
    step: &str,
    info: &RuntimeInfo,
    paths: &Paths,
    cuda: bool,
    full: bool,
) -> Vec<String> {
    let py = paths.python().to_string_lossy().into_owned();
    let s = |v: &str| v.to_string();
    match step {
        "python" => {
            let mut a = vec![
                s("venv"),
                s("--python"),
                info.python.clone(),
                s("--python-preference"),
                s("only-managed"),
            ];
            if full {
                a.push(s("--clear")); // libraries changed: start from a clean environment (downloads stay cached)
            }
            a.push(paths.venv().to_string_lossy().into_owned());
            a
        }
        "libraries" => vec![
            s("pip"),
            s("install"),
            s("--python"),
            py,
            s("-r"),
            paths
                .bundle
                .join(&info.requirements)
                .to_string_lossy()
                .into_owned(),
        ],
        "pytorch" => vec![
            s("pip"),
            s("install"),
            s("--python"),
            py,
            format!("torch=={}", info.torch),
            format!("torchvision=={}", info.torchvision),
            s("--index-url"),
            if cuda {
                info.torch_index.cuda.clone()
            } else {
                info.torch_index.cpu.clone()
            },
            s("--extra-index-url"),
            s("https://pypi.org/simple"),
        ],
        "engine" => vec![
            s("pip"),
            s("install"),
            s("--python"),
            py,
            s("--no-deps"),
            s("--reinstall"),
            paths
                .bundle
                .join(&info.wheel)
                .to_string_lossy()
                .into_owned(),
        ],
        _ => Vec::new(),
    }
}

fn folder_bytes(dir: &Path) -> u64 {
    let mut total = 0;
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        if let Ok(entries) = std::fs::read_dir(&d) {
            for e in entries.flatten() {
                match e.file_type() {
                    Ok(t) if t.is_dir() => stack.push(e.path()),
                    Ok(_) => total += e.metadata().map(|m| m.len()).unwrap_or(0),
                    Err(_) => {}
                }
            }
        }
    }
    total
}

/// Runs the setup in the current thread, reporting through `report` after every change.
/// `cancel` is checked between steps and kills a running uv.
pub struct Runner {
    pub paths: Paths,
    pub status: Arc<Mutex<Status>>,
    pub child: Arc<Mutex<Option<Child>>>,
    pub cancelled: Arc<Mutex<bool>>,
}

impl Runner {
    fn set(&self, report: &dyn Fn(&Status), f: impl FnOnce(&mut Status)) {
        let snapshot = {
            let mut st = self.status.lock().unwrap();
            f(&mut st);
            st.clone()
        };
        report(&snapshot);
    }

    fn step_state(&self, report: &dyn Fn(&Status), id: &str, state: &'static str) {
        self.set(report, |st| {
            if let Some(s) = st.steps.iter_mut().find(|s| s.id == id) {
                s.state = state;
            }
        });
    }

    fn fail(&self, report: &dyn Fn(&Status), id: &str, msg: String) -> Result<(), String> {
        self.set(report, |st| {
            if let Some(s) = st.steps.iter_mut().find(|s| s.id == id) {
                s.state = "failed";
            }
            st.running = false;
            st.error = Some(msg.clone());
        });
        Err(msg)
    }

    fn is_cancelled(&self) -> bool {
        *self.cancelled.lock().unwrap()
    }

    pub fn run(&self, report: &dyn Fn(&Status)) -> Result<(), String> {
        *self.cancelled.lock().unwrap() = false;
        self.set(report, |st| {
            *st = Status {
                running: true,
                steps: initial_steps(),
                ..Status::default()
            }
        });
        let info = match self.paths.info() {
            Ok(i) => i,
            Err(e) => {
                return self.fail(
                    report,
                    "gpu",
                    format!("The installation is incomplete ({e}). Reinstall Grain Search."),
                )
            }
        };
        let full = check(&self.paths)? != Needed::EngineOnly;

        // 1. GPU
        self.step_state(report, "gpu", "running");
        let gpu = detect_gpu();
        let cuda = gpu.is_some();
        self.set(report, |st| {
            st.gpu = gpu.clone();
            st.build = Some(if cuda { "cuda" } else { "cpu" });
        });
        self.step_state(report, "gpu", "done");

        // 2. disk
        self.step_state(report, "disk", "running");
        if full {
            let need = space_needed(cuda);
            if let Some(free) = free_space(&self.paths.runtime) {
                if free < need {
                    return self.fail(
                        report,
                        "disk",
                        format!(
                            "Not enough space on the drive with {}: needs {}, {} free.",
                            self.paths.runtime.display(),
                            gb(need),
                            gb(free)
                        ),
                    );
                }
            }
        }
        self.step_state(report, "disk", "done");
        std::fs::create_dir_all(&self.paths.runtime).map_err(|e| e.to_string())?;
        let _ = std::fs::remove_file(self.paths.marker()); // not complete until step 7

        // 3-6. uv
        for id in INSTALL_STEPS {
            if !full && id != "engine" {
                self.step_state(report, id, "skipped");
                continue;
            }
            if self.is_cancelled() {
                return self.fail(report, id, "Setup was cancelled.".into());
            }
            self.step_state(report, id, "running");
            if let Err(msg) = self.run_uv(report, &uv_args(id, &info, &self.paths, cuda, full)) {
                return self.fail(report, id, msg);
            }
            if id == "libraries" && full && cuda {
                // the libraries must not have swapped in another PyTorch build
                if let Some(build) = torch_build(&self.paths.python()) {
                    if build == "cpu" {
                        return self.fail(
                            report,
                            id,
                            "PyTorch's GPU build didn't install (the CPU build is there instead). \
                             Click Retry; if it happens again, please report it."
                                .into(),
                        );
                    }
                }
            }
            self.step_state(report, id, "done");
        }

        // 7. remember what is installed
        let marker = Marker {
            fingerprint: info.fingerprint.clone(),
            wheel: info.wheel.clone(),
            build: (if cuda { "cuda" } else { "cpu" }).into(),
            setup: SETUP_REV,
        };
        let text = serde_json::to_string_pretty(&marker).map_err(|e| e.to_string())?;
        std::fs::write(self.paths.marker(), text).map_err(|e| e.to_string())?;
        self.set(report, |st| {
            st.running = false;
            st.done = true;
        });
        Ok(())
    }

    fn run_uv(&self, report: &dyn Fn(&Status), args: &[String]) -> Result<(), String> {
        let cache = self.paths.runtime.join("cache");
        let mut cmd = Command::new(self.paths.uv());
        cmd.args(args)
            .env("UV_CACHE_DIR", &cache)
            .env("UV_PYTHON_INSTALL_DIR", self.paths.runtime.join("python"))
            .env("UV_NO_CONFIG", "1") // the user's own uv settings must not change what we install
            .env("UV_NO_PROGRESS", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        let mut child = hidden(&mut cmd)
            .spawn()
            .map_err(|e| format!("Couldn't start uv ({e}). Reinstall Grain Search."))?;
        let mut stderr = child.stderr.take();
        *self.child.lock().unwrap() = Some(child);

        // read uv's messages on another thread (a full pipe would block uv)
        let reader = std::thread::spawn(move || {
            let mut text = String::new();
            if let Some(s) = stderr.as_mut() {
                use std::io::Read;
                let _ = s.read_to_string(&mut text);
            }
            text
        });

        let start = folder_bytes(&cache);
        let code = loop {
            let done = {
                let mut guard = self.child.lock().unwrap();
                match guard.as_mut().map(|c| c.try_wait()) {
                    Some(Ok(Some(status))) => Some(status.success()),
                    Some(Err(_)) | None => Some(false), // gone (cancelled)
                    Some(Ok(None)) => None,
                }
            };
            if let Some(ok) = done {
                break ok;
            }
            let mb = folder_bytes(&cache).saturating_sub(start) / (1024 * 1024);
            self.set(report, |st| st.downloaded_mb = mb);
            std::thread::sleep(Duration::from_millis(500));
        };
        *self.child.lock().unwrap() = None;
        if self.is_cancelled() {
            // don't wait for the message reader: a program uv started may still hold the pipe open
            return Err("Setup was cancelled.".into());
        }
        let err = reader.join().unwrap_or_default();
        if code {
            Ok(())
        } else {
            Err(explain_failure(&err))
        }
    }

    pub fn cancel(&self) {
        *self.cancelled.lock().unwrap() = true;
        if let Some(c) = self.child.lock().unwrap().as_mut() {
            let _ = c.kill();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn info() -> RuntimeInfo {
        RuntimeInfo {
            python: "3.12".into(),
            torch: "2.11.0".into(),
            torchvision: "0.26.0".into(),
            torch_index: TorchIndex {
                cuda: "https://download.pytorch.org/whl/cu128".into(),
                cpu: "https://download.pytorch.org/whl/cpu".into(),
            },
            wheel: "grain_search-0.2.0-py3-none-any.whl".into(),
            requirements: "requirements.txt".into(),
            fingerprint: "abc".into(),
        }
    }

    fn paths() -> Paths {
        Paths {
            bundle: PathBuf::from("/app/engine"),
            runtime: PathBuf::from("/data/runtime"),
        }
    }

    #[test]
    fn what_needs_doing() {
        let m = Marker {
            fingerprint: "abc".into(),
            wheel: "w1".into(),
            build: "cuda".into(),
            setup: SETUP_REV,
        };
        assert_eq!(
            needed("abc", "w1", None, true),
            Needed::Full,
            "never set up"
        );
        assert_eq!(
            needed("abc", "w1", Some(&m), false),
            Needed::Full,
            "Python missing"
        );
        assert_eq!(needed("abc", "w1", Some(&m), true), Needed::Nothing);
        assert_eq!(
            needed("abc", "w2", Some(&m), true),
            Needed::EngineOnly,
            "code-only update"
        );
        assert_eq!(
            needed("xyz", "w2", Some(&m), true),
            Needed::Full,
            "new libraries"
        );
        let old = Marker { setup: 1, ..m };
        assert_eq!(
            needed("abc", "w1", Some(&old), true),
            Needed::Full,
            "set up by an older, broken setup"
        );
        let from_file: Marker =
            serde_json::from_str(r#"{"fingerprint":"abc","wheel":"w1","build":"cpu"}"#).unwrap();
        assert_eq!(from_file.setup, 0, "setup.json from before SETUP_REV");
    }

    #[test]
    fn pytorch_before_the_libraries() {
        let pos = |id| INSTALL_STEPS.iter().position(|s| *s == id).unwrap();
        assert!(
            pos("pytorch") < pos("libraries"),
            "the libraries would pull the CPU PyTorch from PyPI"
        );
        let ui: Vec<_> = initial_steps().into_iter().map(|s| s.id).collect();
        assert_eq!(
            &ui[2..],
            &INSTALL_STEPS,
            "the window lists the steps in the order they run"
        );
        assert_eq!(parse_torch_build("cuda\r\n"), Some("cuda"));
        assert_eq!(parse_torch_build("cpu\n"), Some("cpu"));
        assert_eq!(parse_torch_build(""), None);
    }

    #[test]
    fn gpu_name_from_nvidia_smi() {
        assert_eq!(
            parse_gpu("NVIDIA GeForce RTX 3070\r\n"),
            Some("NVIDIA GeForce RTX 3070".into())
        );
        assert_eq!(parse_gpu("\n  \n"), None);
    }

    #[test]
    fn space_rule() {
        assert!(space_needed(true) > space_needed(false));
        assert_eq!(gb(3 * GB / 2), "1.5 GB");
    }

    #[test]
    fn uv_commands() {
        let (i, p) = (info(), paths());
        let joined = |a: Vec<String>| a.join(" ");
        let venv = joined(uv_args("python", &i, &p, true, true));
        assert!(
            venv.starts_with("venv --python 3.12 --python-preference only-managed --clear"),
            "{venv}"
        );
        assert!(!joined(uv_args("python", &i, &p, true, false)).contains("--clear"));
        let libs = joined(uv_args("libraries", &i, &p, true, true));
        assert!(
            libs.contains("-r") && libs.contains("requirements.txt"),
            "{libs}"
        );
        let gpu = joined(uv_args("pytorch", &i, &p, true, true));
        assert!(gpu.contains("torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128"), "{gpu}");
        let cpu = joined(uv_args("pytorch", &i, &p, false, true));
        assert!(
            cpu.contains("--index-url https://download.pytorch.org/whl/cpu"),
            "{cpu}"
        );
        let eng = joined(uv_args("engine", &i, &p, true, true));
        assert!(
            eng.contains("--no-deps --reinstall")
                && eng.ends_with("grain_search-0.2.0-py3-none-any.whl"),
            "{eng}"
        );
    }

    #[test]
    fn failures_in_plain_words() {
        let net = "error: Failed to fetch: `https://pypi.org/simple/x/`\n  Caused by: dns error\n";
        assert!(explain_failure(net).starts_with("Couldn't download"));
        assert!(
            explain_failure("error: No space left on device (os error 28)")
                .contains("drive is full")
        );
        assert_eq!(explain_failure("error: something odd\n"), "something odd");
        assert_eq!(explain_failure(""), "Setup stopped without saying why.");
    }

    /// run_uv with a stand-in `uv` (a shell script): success, a failure message, and Cancel.
    #[cfg(unix)]
    #[test]
    fn running_uv_reports_and_cancels() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("grain-setup-{}", std::process::id()));
        let bundle = dir.join("engine");
        std::fs::create_dir_all(&bundle).unwrap();
        let script = "#!/bin/sh\ncase \"$1\" in\n  ok) exit 0 ;;\n  slow) sleep 30 ;;\n  *) echo 'error: Failed to fetch: https://pypi.org' >&2; echo '  Caused by: dns error' >&2; exit 2 ;;\nesac\n";
        std::fs::write(bundle.join("uv"), script).unwrap();
        std::fs::set_permissions(bundle.join("uv"), std::fs::Permissions::from_mode(0o755))
            .unwrap();
        let runner = Runner {
            paths: Paths {
                bundle,
                runtime: dir.join("runtime"),
            },
            status: Arc::new(Mutex::new(Status::default())),
            child: Arc::new(Mutex::new(None)),
            cancelled: Arc::new(Mutex::new(false)),
        };
        let quiet = |_: &Status| {};
        assert!(runner.run_uv(&quiet, &["ok".into()]).is_ok());
        let err = runner.run_uv(&quiet, &["fail".into()]).unwrap_err();
        assert!(err.starts_with("Couldn't download"), "{err}");

        let runner = Arc::new(runner);
        let r2 = runner.clone();
        let t = std::thread::spawn(move || r2.run_uv(&|_: &Status| {}, &["slow".into()]));
        std::thread::sleep(Duration::from_millis(700));
        runner.cancel();
        let started = std::time::Instant::now();
        assert_eq!(t.join().unwrap().unwrap_err(), "Setup was cancelled.");
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "Cancel must stop uv at once"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn steps_start_pending() {
        let s = initial_steps();
        assert_eq!(s.len(), 6);
        assert!(s.iter().all(|x| x.state == "pending"));
    }
}
