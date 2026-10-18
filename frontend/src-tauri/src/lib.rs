use std::process::{Child, Command};
use std::sync::Mutex;

#[cfg(not(debug_assertions))]
use std::os::windows::process::CommandExt;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};
use tauri_plugin_autostart::ManagerExt;

// ── App state ──────────────────────────────────────────────────────────────────

struct BackendProcess(Mutex<Option<Child>>);

// ── Python backend lifecycle ───────────────────────────────────────────────────

fn find_project_root() -> std::path::PathBuf {
    let mut log_lines: Vec<String> = vec![];

    // Walk up from cwd: works in dev (cwd is inside the project tree).
    if let Ok(mut dir) = std::env::current_dir() {
        log_lines.push(format!("cwd={dir:?}"));
        loop {
            if dir.join("api.py").exists() {
                log_lines.push(format!("found via cwd-walk: {dir:?}"));
                write_root_log(&log_lines, &dir);
                return dir;
            }
            if !dir.pop() { break; }
        }
    }

    // Check next to the .exe (for portable / side-by-side installs).
    if let Ok(exe) = std::env::current_exe() {
        log_lines.push(format!("exe={exe:?}"));
        if let Some(parent) = exe.parent() {
            if parent.join("api.py").exists() {
                let p = parent.to_path_buf();
                log_lines.push(format!("found via exe-parent: {p:?}"));
                write_root_log(&log_lines, &p);
                return p;
            }
        }
    }

    // Read the path written by setup.ps1 / PowerShell into
    // %LOCALAPPDATA%\HealthLedger\project_root.txt.
    let localappdata = std::env::var_os("LOCALAPPDATA");
    log_lines.push(format!("LOCALAPPDATA={localappdata:?}"));
    if let Some(lad) = localappdata {
        let config = std::path::Path::new(&lad)
            .join("HealthLedger")
            .join("project_root.txt");
        log_lines.push(format!("config_file={config:?} exists={}", config.exists()));
        match std::fs::read_to_string(&config) {
            Ok(path_str) => {
                // Strip UTF-8 BOM if present (PS5 Set-Content -Encoding UTF8 adds one)
                let candidate = std::path::PathBuf::from(
                    path_str.trim_start_matches('\u{FEFF}').trim()
                );
                let has_api = candidate.join("api.py").exists();
                log_lines.push(format!("candidate={candidate:?} has_api.py={has_api}"));
                if has_api {
                    log_lines.push(format!("found via project_root.txt: {candidate:?}"));
                    write_root_log(&log_lines, &candidate);
                    return candidate;
                }
            }
            Err(e) => { log_lines.push(format!("read_err={e}")); }
        }
    }

    let fallback = std::path::PathBuf::from(".");
    log_lines.push("all lookups failed; using .".to_string());
    write_root_log(&log_lines, &fallback);
    fallback
}

fn write_root_log(lines: &[String], result: &std::path::Path) {
    let tmp = std::env::var("TEMP").unwrap_or_else(|_| "C:\\Temp".to_string());
    let path = std::path::Path::new(&tmp).join("ledger_root_debug.txt");
    let content = format!("{}\nresult={result:?}\n", lines.join("\n"));
    let _ = std::fs::write(&path, &content);
}

fn backend_log(msg: &str) {
    let tmp = std::env::var("TEMP").unwrap_or_else(|_| "C:\\Temp".to_string());
    let path = std::path::Path::new(&tmp).join("ledger_backend_debug.txt");
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let line = format!("[{ts}] {msg}\n");
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = f.write_all(line.as_bytes());
    }
}

fn port_in_use(port: u16) -> bool {
    std::net::TcpStream::connect(("127.0.0.1", port)).is_ok()
}

fn spawn_backend() -> Option<Child> {
    // If something is already listening on 8787, reuse it; don't fight over the port.
    if port_in_use(8787) {
        backend_log("port 8787 already in use; skipping spawn, reusing existing backend");
        return None;
    }

    let project_root = find_project_root();
    backend_log(&format!("project_root={project_root:?}"));

    let python = project_root.join(".venv").join("Scripts").join("python.exe");
    backend_log(&format!("python={python:?} exists={}", python.exists()));

    if !python.exists() {
        backend_log("ABORT: python not found");
        return None;
    }

    let mut cmd = Command::new(&python);
    cmd.args([
        "-m", "uvicorn",
        "api:app",
        "--host", "127.0.0.1",
        "--port", "8787",
        "--no-access-log",
    ])
    .current_dir(&project_root)
    .env("PYTHONIOENCODING", "utf-8")
    .env("PYTHONUTF8", "1")
    .env("PYTHONUNBUFFERED", "1");  // flush every log line immediately

    // Dev builds: inherit the terminal so backend logs are visible in the same
    // window as the Next.js / Tauri output, essential for live debugging.
    // Release builds: redirect to logs/uvicorn.log and suppress the console window.
    #[cfg(debug_assertions)]
    {
        cmd.stdout(std::process::Stdio::inherit());
        cmd.stderr(std::process::Stdio::inherit());
    }

    #[cfg(not(debug_assertions))]
    {
        let log_dir = project_root.join("logs");
        let _ = std::fs::create_dir_all(&log_dir);
        let log_file = log_dir.join("uvicorn.log");

        if let Ok(f) = std::fs::OpenOptions::new().create(true).append(true).open(&log_file) {
            use std::os::windows::io::IntoRawHandle;
            cmd.stdout(unsafe {
                <std::process::Stdio as std::os::windows::io::FromRawHandle>::from_raw_handle(
                    f.into_raw_handle(),
                )
            });
        }
        if let Ok(f2) = std::fs::OpenOptions::new().create(true).append(true).open(&log_file) {
            use std::os::windows::io::IntoRawHandle;
            cmd.stderr(unsafe {
                <std::process::Stdio as std::os::windows::io::FromRawHandle>::from_raw_handle(
                    f2.into_raw_handle(),
                )
            });
        }

        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    match cmd.spawn() {
        Ok(child) => {
            backend_log(&format!("uvicorn spawned PID={}", child.id()));
            Some(child)
        }
        Err(e) => {
            backend_log(&format!("spawn FAILED: {e}"));
            None
        }
    }
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Ok(mut guard) = app.state::<BackendProcess>().0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
            println!("[backend] uvicorn stopped");
        }
    }
}

// ── Entry point ────────────────────────────────────────────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // ── Plugins ───────────────────────────────────────────────────────
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        // ── State ─────────────────────────────────────────────────────────
        .manage(BackendProcess(Mutex::new(spawn_backend())))
        // ── Setup ─────────────────────────────────────────────────────────
        .setup(|app| {
            // Register with Windows startup (idempotent)
            let _ = app.autolaunch().enable();

            // Build tray menu
            let show = MenuItem::with_id(app, "show", "Open Health Ledger", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Health Ledger")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => {
                        kill_backend(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    // Left-click the tray icon → show the window
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        // ── X button minimizes to tray instead of closing ──────────────────
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("Health Ledger failed to start");
}
