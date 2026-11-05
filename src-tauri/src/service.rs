//! Lifecycle for the Python transfer service that runs as a Tauri sidecar.

use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;

/// How long the service may take to answer its first health check.
const READY_TIMEOUT: Duration = Duration::from_secs(30);
const PROBE_TIMEOUT: Duration = Duration::from_millis(700);

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceInfo {
    pub base_url: String,
    pub token: String,
}

struct ServiceHandle {
    port: u16,
    token: String,
    child: Child,
    /// Kept alive for the process lifetime so the OS kills the child on exit.
    _job: Option<crate::job::JobHandle>,
}

/// Shared state owned by the Tauri application.
#[derive(Default)]
pub struct ServiceState {
    handle: Mutex<Option<ServiceHandle>>,
}

impl ServiceState {
    pub fn info(&self) -> Result<ServiceInfo, String> {
        let guard = self.handle.lock().map_err(|_| "本机服务状态不可用".to_string())?;
        let handle = guard.as_ref().ok_or_else(|| "本机服务尚未启动".to_string())?;
        Ok(ServiceInfo {
            base_url: format!("http://127.0.0.1:{}", handle.port),
            token: handle.token.clone(),
        })
    }

    /// Start the sidecar if it is not already running.
    pub fn start(&self) -> Result<ServiceInfo, String> {
        let mut guard = self.handle.lock().map_err(|_| "本机服务状态不可用".to_string())?;
        if let Some(handle) = guard.as_mut() {
            if handle.child.try_wait().map_err(|error| error.to_string())?.is_none() {
                return Ok(ServiceInfo {
                    base_url: format!("http://127.0.0.1:{}", handle.port),
                    token: handle.token.clone(),
                });
            }
        }

        let port = reserve_port()?;
        let token = session_token()?;
        let mut command = service_command()?;
        command
            .env("LANDROP_SERVICE_PORT", port.to_string())
            .env("LANDROP_SERVICE_TOKEN", &token)
            .env("PYTHONUTF8", "1")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if let Some(python_path) = dev_python_path() {
            command.env("PYTHONPATH", python_path);
        }

        let mut child = command
            .spawn()
            .map_err(|error| format!("无法启动本机传输服务: {error}"))?;
        let job = crate::job::JobHandle::attach(&child);

        if !wait_until_ready(port, &token, &mut child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("本机传输服务未能在限定时间内就绪".to_string());
        }

        *guard = Some(ServiceHandle {
            port,
            token,
            child,
            _job: job,
        });
        Ok(ServiceInfo {
            base_url: format!("http://127.0.0.1:{port}"),
            token: guard
                .as_ref()
                .map(|handle| handle.token.clone())
                .unwrap_or_default(),
        })
    }

    /// Stop the sidecar. Safe to call more than once.
    pub fn stop(&self) {
        let Ok(mut guard) = self.handle.lock() else {
            return;
        };
        if let Some(mut handle) = guard.take() {
            let _ = handle.child.kill();
            let _ = handle.child.wait();
        }
    }
}

fn reserve_port() -> Result<u16, String> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .map_err(|error| format!("无法分配本机服务端口: {error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| error.to_string())?
        .port();
    drop(listener);
    Ok(port)
}

fn session_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|error| format!("无法生成会话令牌: {error}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Build the sidecar command: a packaged executable when present, otherwise the
/// Python module from the development tree.
fn service_command() -> Result<Command, String> {
    let exe_dir = std::env::current_exe()
        .map_err(|error| error.to_string())?
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| "无法定位应用目录".to_string())?;

    // The bundle places the PyInstaller onedir output here (see tauri.conf.json).
    if let Some(executable) = packaged_service(&exe_dir) {
        return Ok(Command::new(executable));
    }

    if cfg!(debug_assertions) {
        let mut command = Command::new(dev_python());
        command.args(["-m", "landrop.api.app"]);
        if let Some(root) = repo_root() {
            command.current_dir(root);
        }
        return Ok(command);
    }

    Err(format!(
        "找不到本机传输服务程序，请重新安装 LANDrop（已查找 {}）",
        exe_dir.join("landrop-service").join("landrop-service.exe").display()
    ))
}

/// Locate the packaged sidecar in every layout the bundlers produce.
fn packaged_service(exe_dir: &Path) -> Option<PathBuf> {
    let candidates = [
        // Tauri resource: <install>/landrop-service/landrop-service.exe
        exe_dir.join("landrop-service").join("landrop-service.exe"),
        // MSI nests resources in a ``resources`` folder.
        exe_dir
            .join("resources")
            .join("landrop-service")
            .join("landrop-service.exe"),
        // Portable layout: beside the app executable.
        exe_dir.join("landrop-service.exe"),
    ];
    candidates.into_iter().find(|path| path.is_file())
}

fn dev_python() -> String {
    std::env::var("LANDROP_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn repo_root() -> Option<PathBuf> {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest.parent().map(PathBuf::from)
}

fn dev_python_path() -> Option<PathBuf> {
    if !cfg!(debug_assertions) {
        return None;
    }
    repo_root().map(|root| root.join("src"))
}

fn wait_until_ready(port: u16, token: &str, child: &mut Child) -> bool {
    let deadline = Instant::now() + READY_TIMEOUT;
    while Instant::now() < deadline {
        if health_ok(port, token) {
            return true;
        }
        if let Ok(Some(_)) = child.try_wait() {
            return false;
        }
        std::thread::sleep(Duration::from_millis(120));
    }
    false
}

/// Minimal HTTP probe so the shell does not need an HTTP client dependency.
fn health_ok(port: u16, token: &str) -> bool {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, PROBE_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-LANDrop-Token: {token}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buffer = [0u8; 32];
    match stream.read(&mut buffer) {
        Ok(read) if read > 9 => {
            let head = String::from_utf8_lossy(&buffer[..read]);
            head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
        }
        _ => false,
    }
}
