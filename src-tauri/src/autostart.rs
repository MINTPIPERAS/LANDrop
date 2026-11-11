//! Registry-based autostart (HKCU Run) with self-healing for moved installs.

use std::path::{Path, PathBuf};
use std::process::Command;

const RUN_KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run";
const VALUE_NAME: &str = "LANDrop";

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn reg_exe() -> PathBuf {
    let candidate = std::env::var("SystemRoot")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(r"C:\Windows"))
        .join("System32")
        .join("reg.exe");
    if candidate.is_file() {
        candidate
    } else {
        PathBuf::from("reg.exe")
    }
}

fn reg(args: &[&str]) -> Result<String, String> {
    let mut command = Command::new(reg_exe());
    command.args(args);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let output = command
        .output()
        .map_err(|error| format!("无法执行 reg.exe：{error}"))?;
    if !output.status.success() {
        let message = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return Err(message.trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

pub fn current_executable() -> Result<PathBuf, String> {
    std::env::current_exe().map_err(|error| format!("无法确定程序路径：{error}"))
}

fn command_line(exe: &Path) -> String {
    format!("\"{}\" --autostart", exe.display())
}

pub fn enable() -> Result<(), String> {
    let exe = current_executable()?;
    reg(&[
        "add",
        RUN_KEY,
        "/v",
        VALUE_NAME,
        "/t",
        "REG_SZ",
        "/d",
        &command_line(&exe),
        "/f",
    ])?;
    Ok(())
}

fn is_missing(message: &str) -> bool {
    message.contains("无法找到")
        || message.contains("找不到")
        || message.to_lowercase().contains("unable to find")
}

pub fn disable() -> Result<(), String> {
    match reg(&["delete", RUN_KEY, "/v", VALUE_NAME, "/f"]) {
        Ok(_) => Ok(()),
        // Removing an entry that is already absent is a success for the caller.
        Err(message) if is_missing(&message) => Ok(()),
        Err(message) => Err(message),
    }
}

/// The command currently registered for autostart, if any.
pub fn registered_command() -> Option<String> {
    let output = reg(&["query", RUN_KEY, "/v", VALUE_NAME]).ok()?;
    let line = output.lines().find(|line| line.contains("REG_SZ"))?;
    let index = line.find("REG_SZ")? + "REG_SZ".len();
    Some(line[index..].trim().to_string())
}

pub fn is_enabled() -> bool {
    registered_command().is_some()
}

/// Rewrite the value when the executable moved (reinstall or version change).
pub fn ensure_current_path() {
    let Ok(exe) = current_executable() else {
        return;
    };
    let expected = command_line(&exe);
    if let Some(current) = registered_command() {
        if !current.eq_ignore_ascii_case(&expected) {
            let _ = enable();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Opt-in: touches the real HKCU Run key, then restores whatever was there.
    #[test]
    fn toggle_round_trip_restores_previous_state() {
        if std::env::var("LANDROP_TEST_AUTOSTART").is_err() {
            return;
        }
        let previous = registered_command();

        enable().expect("enable autostart");
        assert!(is_enabled());
        assert!(registered_command()
            .unwrap_or_default()
            .to_lowercase()
            .contains("--autostart"));

        disable().expect("disable autostart");
        assert!(!is_enabled());

        if let Some(command) = previous {
            reg(&[
                "add", RUN_KEY, "/v", VALUE_NAME, "/t", "REG_SZ", "/d", &command, "/f",
            ])
            .expect("restore previous value");
            assert_eq!(registered_command().as_deref(), Some(command.as_str()));
        }
    }
}
