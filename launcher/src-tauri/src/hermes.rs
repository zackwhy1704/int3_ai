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
///
/// Each release carries two verdicts: `state` (hosted: this desktop against our backend pin) and
/// `local_state` (local mode: stock Hermes on the PC, our brain over MCP, no backend involved). The
/// server says which mode the signed-in company uses (`mode`); a server without that field is hosted.
pub fn policy(install: &Install, compat: &Value) -> Policy {
    let local = compat["mode"] == "local";
    let backend = compat["backend_release"].as_str().unwrap_or("?");
    let build = compat["desktop_builds"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|b| b["platform"] == "windows" && b["package_version"].as_str() == Some(install.version.as_str()));
    let Some(build) = build else {
        // Warn, never block: an unknown build is what a partner gets the day Nous ships a new one.
        let what = if install.version == "dev" { "This development build of Hermes".to_string() } else { format!("Hermes {}", install.version) };
        let server = if local { String::new() } else { format!(" (server: Hermes {backend})") };
        return Policy {
            state: "untested".into(),
            message: format!("{what} has not been tested with int3 yet{server}. You can still continue. If anything looks odd, send us a screenshot."),
        };
    };
    let release = build["release"].as_str().unwrap_or("?");
    let field = if local { "local_state" } else { "state" };
    let state = compat["releases"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|r| r["release"].as_str() == Some(release))
        .and_then(|r| r[field].as_str())
        .unwrap_or("untested")
        .to_string();
    let message = match (state.as_str(), local) {
        ("supported", _) => format!("Hermes {release} is supported."),
        ("blocked", _) => format!("Hermes {release} is blocked on this server. Update Hermes Desktop, then try again."),
        (_, true) => format!("Hermes {release} has not been tested with int3 yet. You can still continue. If anything looks odd, send us a screenshot."),
        (_, false) => format!("Hermes {release} has not been tested against backend {backend}."),
    };
    Policy { state, message }
}

/// The packaged Desktop's executable names: the Store build (NousResearchInc.HermesAgent, seen 2026-11-25 as
/// 2026.7855.419.0) runs "Hermes Agent.exe"; older and sideloaded builds ran "Hermes.exe".
const PACKAGED_IMAGES: [&str; 2] = ["Hermes Agent.exe", "Hermes.exe"];

/// Does `tasklist /FO CSV /NH` output list one of the packaged Desktop's images?
fn lists_packaged_hermes(tasklist_csv: &str) -> bool {
    tasklist_csv.lines().any(|l| {
        let image = l.trim().trim_start_matches('"').split('"').next().unwrap_or("");
        PACKAGED_IMAGES.iter().any(|n| image.eq_ignore_ascii_case(n))
    })
}

/// Is a packaged Hermes running? (A dev build is not tracked.)
pub fn running(install: &Install) -> bool {
    if !matches!(install.kind, Kind::Msix { .. }) {
        return false;
    }
    hidden(Command::new("tasklist").args(["/FO", "CSV", "/NH"]))
        .output()
        .map(|o| lists_packaged_hermes(&String::from_utf8_lossy(&o.stdout)))
        .unwrap_or(false)
}

/// Close a running packaged Hermes so it re-reads its config, .env and connections.json on start. Only
/// called after the user confirmed the restart in the launcher. A graceful close (no /F), then a short wait.
pub fn stop(install: &Install) {
    if matches!(install.kind, Kind::Msix { .. }) {
        for image in PACKAGED_IMAGES {
            let _ = hidden(Command::new("taskkill").args(["/IM", image])).status();
        }
        for _ in 0..15 {
            std::thread::sleep(std::time::Duration::from_secs(1));
            if !running(install) {
                break;
            }
        }
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

    #[test]
    fn the_store_builds_process_is_recognised() {
        let store = "\"explorer.exe\",\"4\",\"Console\",\"1\",\"1,024 K\"\r\n\"Hermes Agent.exe\",\"18200\",\"Console\",\"1\",\"210,000 K\"\r\n";
        assert!(lists_packaged_hermes(store)); // Store 2026.7855.419.0
        assert!(lists_packaged_hermes("\"Hermes.exe\",\"5\",\"Console\",\"1\",\"1 K\"")); // older builds
        assert!(lists_packaged_hermes("\"hermes agent.exe\",\"5\",\"Console\",\"1\",\"1 K\""));
        assert!(!lists_packaged_hermes("\"hermes-agent.exe\",\"5\",\"Console\",\"1\",\"1 K\"")); // the CLI alias is not the Desktop
        assert!(!lists_packaged_hermes("\"python.exe\",\"5\",\"Console\",\"1\",\"1 K\"\r\nINFO: No tasks are running"));
    }
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

    /// Local and hosted verdicts are separate: the same build can be supported locally and untested hosted.
    fn compat_with_modes(mode: Option<&str>) -> Value {
        let mut c = json!({"backend_release": "v0.22.6",
               "releases": [{"release": "v0.23.4", "state": "untested", "local_state": "supported"},
                            {"release": "v0.23.1", "state": "untested", "local_state": "untested"},
                            {"release": "v0.22.6", "state": "supported", "local_state": "blocked"},
                            {"release": "v0.23.9", "state": "untested"}],
               "desktop_builds": [{"platform": "windows", "package_version": "2026.7855.419.0", "release": "v0.23.4"},
                                  {"platform": "windows", "package_version": "2026.7000.1.0", "release": "v0.23.1"},
                                  {"platform": "windows", "package_version": "2026.7300.1.0", "release": "v0.22.6"},
                                  {"platform": "windows", "package_version": "2026.8000.1.0", "release": "v0.23.9"}]});
        if let Some(m) = mode {
            c["mode"] = json!(m);
        }
        c
    }

    #[test]
    fn a_local_launch_shows_the_local_verdict_and_hosted_keeps_its_own() {
        let store = install("2026.7855.419.0");
        let local = policy(&store, &compat_with_modes(Some("local")));
        assert_eq!((local.state.as_str(), local.message.as_str()), ("supported", "Hermes v0.23.4 is supported."));
        let hosted = policy(&store, &compat_with_modes(Some("hosted")));
        assert_eq!(hosted.state, "untested");
        assert!(hosted.message.contains("against backend v0.22.6"));
        // An older server sends no mode: hosted, as before.
        assert_eq!(policy(&store, &compat_with_modes(None)).state, "untested");

        let untested = policy(&install("2026.7000.1.0"), &compat_with_modes(Some("local")));
        assert_eq!(untested.state, "untested");
        assert!(untested.message.contains("You can still continue") && !untested.message.contains("backend"),
                "local mode has no backend pin to name: {}", untested.message);
        // Each mode's "blocked" is its own.
        assert_eq!(policy(&install("2026.7300.1.0"), &compat_with_modes(Some("local"))).state, "blocked");
        assert_eq!(policy(&install("2026.7300.1.0"), &compat_with_modes(Some("hosted"))).state, "supported");
        // A row from a server without local_state: untested in local mode, never supported by accident.
        assert_eq!(policy(&install("2026.8000.1.0"), &compat_with_modes(Some("local"))).state, "untested");
        let unknown = policy(&install("2026.9999.1.0"), &compat_with_modes(Some("local")));
        assert!(unknown.message.contains("2026.9999.1.0") && !unknown.message.contains("server: Hermes"));
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
