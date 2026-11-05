use tauri::Manager;

mod job;
mod service;

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

#[tauri::command]
fn service_status(state: tauri::State<'_, service::ServiceState>) -> Result<service::ServiceInfo, String> {
    state.start()
}

#[tauri::command]
fn service_info(state: tauri::State<'_, service::ServiceState>) -> Result<service::ServiceInfo, String> {
    state.info()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(service::ServiceState::default())
        .invoke_handler(tauri::generate_handler![
            show_main_window,
            service_status,
            service_info
        ])
        .setup(|app| {
            // Start the transfer service eagerly so the UI can connect immediately.
            let state = app.state::<service::ServiceState>();
            let _ = state.start();

            if let Some(window) = app.get_webview_window("main") {
                let handle = app.handle().clone();
                window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { .. } = event {
                        handle.state::<service::ServiceState>().stop();
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
