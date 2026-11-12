//! Finding and starting the STOCK Hermes Desktop on Windows. Hermes is never modified: we only
//! write its connections.json (registry.rs) and start it.
//!
//! Install kinds:
//!   * MSIX (Store `NousResearchInc.HermesAgent`, out-of-store `NousResearch.Hermes*`; identity
//!     names from apps/desktop/product-identity.cjs, as in connect-desktop.ps1:146-153). Its
//!     userData is virtualized: we write the private per-package copy AND the real %APPDATA% one
//!     (connect-desktop.ps1:193-218). Started via shell:AppsFolder\<family>!<app id>.
//!   * Dev/test command (CB_LAUNCHER_HERMES_EXE, optional CB_LAUNCHER_HERMES_ARGS_JSON /
//!     CB_LAUNCHER_HERMES_CWD, required CB_LAUNCHER_USER_DATA_DIR): e.g. a from-source v0.22.6
//!     build; started with HERMES_DESKTOP_USER_DATA_DIR pointing at that folder.

use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Debug, Clone)]
pub enum Kind {
    Msix { family: String, app_id: String },
    Dev { exe: PathBuf, args: Vec<String>, cwd: Option<PathBuf> },
}

#[derive(Debug, Clone)]
pub struct Install {
    pub kind: Kind,
    /// MSIX package version (timestamp-derived quad, msix-shared.mjs:98-107) or "dev".
    pub version: String,
    /// Every Hermes userData folder to write; [0] is the one the app reads first.
    pub user_data_dirs: Vec<PathBuf>,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Policy {
    /// supported | untested | blocked
    pub state: String,
    pub message: String,
}

fn hidden(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

/// The official Hermes MSIX requires Windows 11 22H2 (build 22621): v0.22.6
/// apps/desktop/BUILDING.md:14, website/docs/getting-started/platform-support.md:20.
pub const MSIX_MIN_BUILD: u32 = 22621;

/// This PC's Windows build number (CurrentBuildNumber), or None if it cannot be read.
pub fn os_build() -> Option<u32> {
    let out = hidden(Command::new("reg").args([
        "query",
        r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        "/v",
        "CurrentBuildNumber",
    ]))
    .output()
    .ok()?;
    parse_build(&String::from_utf8_lossy(&out.stdout))
}

fn parse_build(reg_output: &str) -> Option<u32> {
    reg_output
        .lines()
        .find(|l| l.trim_start().starts_with("CurrentBuildNumber"))?
        .split_whitespace()
        .last()?
        .parse()
        .ok()
}

pub fn detect() -> Option<Install> {
    if let Ok(exe) = std::env::var("CB_LAUNCHER_HERMES_EXE") {
        let dir = std::env::var("CB_LAUNCHER_USER_DATA_DIR").ok()?;
        let args = std::env::var("CB_LAUNCHER_HERMES_ARGS_JSON")
            .ok()
            .and_then(|j| serde_json::from_str::<Vec<String>>(&j).ok())
            .unwrap_or_default();
        let cwd = std::env::var("CB_LAUNCHER_HERMES_CWD").ok().map(PathBuf::from);
        return Some(Install {
            kind: Kind::Dev { exe: PathBuf::from(exe), args, cwd },
            version: "dev".into(),
            user_data_dirs: vec![PathBuf::from(dir)],
        });
    }
    detect_msix()
}

const MSIX_QUERY: &str = r#"
$p = Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
  ($_.Name -like 'NousResearch.Hermes*') -or ($_.Name -eq 'NousResearchInc.HermesAgent') -or
  (($_.Name -like '*Hermes*') -and ($_.Publisher -like '*Nous Research*')) } | Select-Object -First 1
if ($p) {
  $app = (Get-AppxPackageManifest $p).Package.Applications.Application | Select-Object -First 1
  [pscustomobject]@{ name = $p.Name; version = $p.Version.ToString(); family = $p.PackageFamilyName; app_id = $app.Id } | ConvertTo-Json -Compress
}"#;

fn detect_msix() -> Option<Install> {
    let out = hidden(Command::new("powershell").args(["-NoProfile", "-NonInteractive", "-Command", MSIX_QUERY]))
        .output()
        .ok()?;
    let v: Value = serde_json::from_slice(&out.stdout).ok()?;
    let family = v["family"].as_str()?.to_string();
    let app_id = v["app_id"].as_str().unwrap_or("App").to_string();
    let version = v["version"].as_str().unwrap_or_default().to_string();
    Some(Install { user_data_dirs: msix_user_data_dirs(&family), kind: Kind::Msix { family, app_id }, version })
}

/// connect-desktop.ps1:194-218: the Electron userData folder is the productName (Hermes / Hermes
/// Agent / Hermes Light); prefer one that exists. Primary = the private copy if it already holds
/// a registry, else the real path; write both.
fn msix_user_data_dirs(family: &str) -> Vec<PathBuf> {
    let local = PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_default());
    let roaming = PathBuf::from(std::env::var("APPDATA").unwrap_or_default());
    let private_root = local.join("Packages").join(family).join("LocalCache").join("Roaming");
    let names = ["Hermes", "Hermes Agent", "Hermes Light"];
    let pick = names
        .iter()
        .find(|n| private_root.join(n).exists())
        .or_else(|| names.iter().find(|n| roaming.join(n).exists()))
        .unwrap_or(&"Hermes");
    let private = private_root.join(pick);
    let real = roaming.join(pick);
    if private.join(crate::registry::FILE_NAME).exists() {
        vec![private, real]
    } else {
        vec![real, private]
    }
}

/// Version policy from the server's compat table (GET /launcher/compat, also in /launch):
/// a known desktop build takes its release's state; anything else is "untested" (warn, allow);
/// "blocked" refuses. A dev build is always untested.
pub fn policy(install: &Install, compat: &Value) -> Policy {
    let backend = compat["backend_release"].as_str().unwrap_or("?");
    let build = compat["desktop_builds"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|b| b["platform"] == "windows" && b["package_version"].as_str() == Some(install.version.as_str()));
    let Some(build) = build else {
        // Warn, never block: an unknown build is what a partner gets the day Nous ships a new one.
        let what = if install.version == "dev" { "This development build of Hermes".to_string() } else { format!("Hermes {}", install.version) };
        return Policy {
            state: "untested".into(),
            message: format!("{what} has not been tested with int3 yet (server: Hermes {backend}). You can still continue. If anything looks odd, send us a screenshot."),
        };
    };
    let release = build["release"].as_str().unwrap_or("?");
    let state = compat["releases"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|r| r["release"].as_str() == Some(release))
        .and_then(|r| r["state"].as_str())
        .unwrap_or("untested")
        .to_string();
    let message = match state.as_str() {
        "supported" => format!("Hermes {release} is supported."),
        "blocked" => format!("Hermes {release} is blocked on this server. Update Hermes Desktop, then try again."),
        _ => format!("Hermes {release} has not been tested against backend {backend}."),
    };
    Policy { state, message }
}

/// Is a packaged Hermes running? (A dev build is not tracked.)
pub fn running(install: &Install) -> bool {
    if !matches!(install.kind, Kind::Msix { .. }) {
        return false;
    }
    hidden(Command::new("tasklist").args(["/FI", "IMAGENAME eq Hermes.exe", "/NH"]))
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains("Hermes.exe"))
        .unwrap_or(false)
}

/// Close a running packaged Hermes so it re-reads connections.json on start. Only called after
/// the user confirmed the restart in the launcher.
pub fn stop(install: &Install) {
    if matches!(install.kind, Kind::Msix { .. }) {
        let _ = hidden(Command::new("taskkill").args(["/IM", "Hermes.exe"])).status();
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
}

pub fn start(install: &Install) -> std::io::Result<()> {
    match &install.kind {
        Kind::Msix { family, app_id } => {
            Command::new("explorer.exe").arg(format!("shell:AppsFolder\\{family}!{app_id}")).spawn()?;
        }
        Kind::Dev { exe, args, cwd } => {
            let mut cmd = Command::new(exe);
            cmd.args(args).env("HERMES_DESKTOP_USER_DATA_DIR", &install.user_data_dirs[0]);
            if let Ok(home) = std::env::var("CB_LAUNCHER_HERMES_HOME") {
                cmd.env("HERMES_HOME", home); // local mode in a dev/test run: the isolated home
            }
            if let Some(dir) = cwd {
                cmd.current_dir(dir);
            }
            cmd.spawn()?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn install(version: &str) -> Install {
        Install { kind: Kind::Dev { exe: "x".into(), args: vec![], cwd: None }, version: version.into(), user_data_dirs: vec![] }
    }
    fn compat() -> Value {
        json!({"backend_release": "v0.22.6",
               "releases": [{"release": "v0.22.6", "state": "supported"}, {"release": "v0.20.0", "state": "blocked"}],
               "desktop_builds": [{"platform": "windows", "package_version": "2026.7300.1.0", "release": "v0.22.6"},
                                  {"platform": "windows", "package_version": "2025.100.1.0", "release": "v0.20.0"}]})
    }

    #[test]
    fn parses_reg_query_build_number() {
        let out = "\r\nHKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\r\n    CurrentBuildNumber    REG_SZ    19045\r\n\r\n";
        assert_eq!(parse_build(out), Some(19045));
        assert_eq!(parse_build("garbage"), None);
        assert!(19045 < MSIX_MIN_BUILD && 22621 >= MSIX_MIN_BUILD);
    }

    #[test]
    fn known_build_takes_its_release_state() {
        assert_eq!(policy(&install("2026.7300.1.0"), &compat()).state, "supported");
        assert_eq!(policy(&install("2025.100.1.0"), &compat()).state, "blocked");
    }

    #[test]
    fn unknown_and_dev_builds_are_untested() {
        let unknown = policy(&install("2026.9999.1.0"), &compat());
        assert_eq!(unknown.state, "untested");
        assert!(unknown.message.contains("2026.9999.1.0"), "the partner must see the exact version");
        assert!(unknown.message.contains("You can still continue"));
        let dev = policy(&install("dev"), &compat());
        assert_eq!(dev.state, "untested");
        assert!(dev.message.contains("development build"));
    }
}
