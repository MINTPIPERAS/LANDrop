use std::path::PathBuf;
use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, WindowEvent};

mod autostart;
mod job;
mod service;

/// Runtime window behaviour; the frontend mirrors the saved config into it.
struct Settings {
    close_to_tray: Mutex<bool>,
}

#[tauri::command]
fn show_main_window(app: tauri::AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "主窗口不可用".to_string())?;
    // ``show`` alone does nothing for a minimized window, so the hotkey would
    // hand focus to a window the user still cannot see.
    if window.is_minimized().unwrap_or(false) {
        window.unminimize().map_err(|error| error.to_string())?;
    }
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

fn reveal_window(app: &tauri::AppHandle) {
    let _ = show_main_window(app.clone());
}

#[tauri::command]
fn service_status(state: tauri::State<'_, service::ServiceState>) -> Result<service::ServiceInfo, String> {
    state.start()
}

#[tauri::command]
fn service_info(state: tauri::State<'_, service::ServiceState>) -> Result<service::ServiceInfo, String> {
    state.info()
}

#[tauri::command]
fn set_close_to_tray(state: tauri::State<'_, Settings>, enabled: bool) {
    if let Ok(mut flag) = state.close_to_tray.lock() {
        *flag = enabled;
    }
}

#[tauri::command]
fn autostart_status() -> bool {
    autostart::is_enabled()
}

#[tauri::command]
fn set_autostart(enabled: bool) -> Result<bool, String> {
    if enabled {
        autostart::enable()?;
    } else {
        autostart::disable()?;
    }
    Ok(autostart::is_enabled())
}

fn stop_and_exit(app: &tauri::AppHandle) {
    app.state::<service::ServiceState>().stop();
    app.exit(0);
}

#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    stop_and_exit(&app);
}

fn app_data_dir() -> PathBuf {
    std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("LANDrop")
}

/// The configured log directory, falling back to the default location.
fn log_dir() -> PathBuf {
    let base = app_data_dir();
    if let Ok(text) = std::fs::read_to_string(base.join("config.json")) {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
            if let Some(dir) = value.get("log_dir").and_then(|item| item.as_str()) {
                let trimmed = dir.trim();
                if !trimmed.is_empty() {
                    return PathBuf::from(trimmed);
                }
            }
        }
    }
    base.join("logs")
}

fn open_logs() {
    let dir = log_dir();
    let _ = std::fs::create_dir_all(&dir);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let _ = std::process::Command::new("explorer.exe")
            .arg(dir)
            .creation_flags(0x0800_0000)
            .spawn();
    }
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", "打开日志目录", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &logs, &separator, &quit])?;

    let mut builder = TrayIconBuilder::with_id("landrop")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("LANDrop")
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => reveal_window(app),
            "logs" => open_logs(),
            "quit" => stop_and_exit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                reveal_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    let tray = builder.build(app)?;
    // Dropping the handle removes the icon, so keep it alive with the app.
    app.manage(tray);
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let start_hidden = std::env::args().any(|arg| arg == "--autostart");

    tauri::Builder::default()
        // Must be the first plugin: a second launch hands off to this instance.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Login autostart should not steal focus from what the user is doing.
            if args.iter().any(|arg| arg == "--autostart") {
                return;
            }
            reveal_window(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(service::ServiceState::default())
        .manage(Settings {
            close_to_tray: Mutex::new(false),
        })
        .invoke_handler(tauri::generate_handler![
            show_main_window,
            service_status,
            service_info,
            set_close_to_tray,
            autostart_status,
            set_autostart,
            quit_app
        ])
        .setup(move |app| {
            // Start the transfer service eagerly so the UI can connect immediately.
            let state = app.state::<service::ServiceState>();
            let _ = state.start();

            if let Err(error) = build_tray(app) {
                eprintln!("托盘初始化失败：{error}");
            }
            autostart::ensure_current_path();

            if let Some(window) = app.get_webview_window("main") {
                if !start_hidden {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
                let handle = app.handle().clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        let close_to_tray = handle
                            .state::<Settings>()
                            .close_to_tray
                            .lock()
                            .map(|flag| *flag)
                            .unwrap_or(false);
                        if close_to_tray {
                            // Keep the service (and schedules) alive in the tray.
                            api.prevent_close();
                            if let Some(window) = handle.get_webview_window("main") {
                                let _ = window.hide();
                            }
                        }
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building LANDrop desktop application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                app_handle.state::<service::ServiceState>().stop();
            }
        });
}
