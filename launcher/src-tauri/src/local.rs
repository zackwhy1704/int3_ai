//! Local mode (M1, docs/LOCAL_MODE_DESIGN.md): stock Hermes runs its own local backend on this PC;
//! the int3 brain is reached as a remote MCP server with a short-lived brain token.
//!
//! The launcher never edits Hermes code. It applies the server's policy with Hermes's own CLI
//! (`hermes config set`, which routes and validates keys: hermes_cli/config.py:3114-3205 @v0.23.1),
//! writes two lines of `.env` (the brain token and HERMES_WRITE_SAFE_ROOT, documented in
//! website/docs/guides/secure-hermes-on-a-work-machine.md:170-174), keeps an int3 block in SOUL.md,
//! then reads config.yaml back and REPORTS what it found. The server judges the report; a brain token
//! is minted only after a good verdict, and a later bad report makes the brain refuse.
//!
//! Detection, not prevention: the user owns %LOCALAPPDATA%\hermes.

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub const TOKEN_KEY: &str = "INT3_BRAIN_TOKEN";
pub const SAFE_ROOT_KEY: &str = "HERMES_WRITE_SAFE_ROOT";

/// HERMES_HOME as Store Desktop resolves it: %LOCALAPPDATA%\hermes (apps/desktop/electron/data-paths.mjs
/// :26-33 @v0.23.1; the MSIX writes it unvirtualized, electron-builder.config.cjs:276-278).
/// CB_LAUNCHER_HERMES_HOME isolates tests and dev runs.
pub fn hermes_home() -> PathBuf {
    if let Ok(h) = std::env::var("CB_LAUNCHER_HERMES_HOME") {
        return PathBuf::from(h);
    }
    PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_default()).join("hermes")
}

/// The `hermes` CLI: CB_LAUNCHER_HERMES_CLI (dev), else the MSIX execution alias
/// (website/docs/user-guide/windows-native.md:62), else whatever `hermes` is on PATH.
pub fn hermes_cli() -> PathBuf {
    if let Ok(c) = std::env::var("CB_LAUNCHER_HERMES_CLI") {
        return PathBuf::from(c);
    }
    let alias = PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_default())
        .join("Microsoft").join("WindowsApps").join("hermes.exe");
    if alias.exists() { alias } else { PathBuf::from("hermes") }
}

pub fn workspace(dirname: &str) -> PathBuf {
    if let Ok(w) = std::env::var("CB_LAUNCHER_WORKSPACE") {
        return PathBuf::from(w);
    }
    PathBuf::from(std::env::var("USERPROFILE").unwrap_or_default()).join(dirname)
}

fn hidden(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

/// The value text `hermes config set` parses: JSON is valid YAML flow, so lists and mappings round-trip
/// (config.py:3135-3147); scalars are written bare.
pub fn config_literal(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::Null => "null".into(),
        other => other.to_string(),
    }
}

pub fn config_set(key: &str, value: &Value) -> Result<(), String> {
    let out = hidden(
        Command::new(hermes_cli())
            .args(["config", "set", key, &config_literal(value)])
            .env("HERMES_HOME", hermes_home()),
    )
    .output()
    .map_err(|e| format!("could not run Hermes's config command: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        let err = String::from_utf8_lossy(&out.stderr);
        let last = err.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("").trim();
        Err(format!("Hermes refused the setting {key}: {last}"))
    }
}

pub fn hermes_version() -> String {
    hidden(Command::new(hermes_cli()).arg("--version").env("HERMES_HOME", hermes_home()))
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).lines().next().unwrap_or("").trim().chars().take(60).collect())
        .unwrap_or_default()
}

/// `.env` with `key` set (Some) or removed (None); every other line untouched.
pub fn env_with(text: &str, key: &str, value: Option<&str>) -> String {
    let mut out: Vec<String> = text
        .lines()
        .filter(|l| {
            let t = l.trim_start();
            let t = t.strip_prefix("export ").unwrap_or(t);
            !(t.starts_with(key) && t[key.len()..].trim_start().starts_with('='))
        })
        .map(str::to_string)
        .collect();
    if let Some(v) = value {
        out.push(format!("{key}={v}"));
    }
    let mut s = out.join("\n");
    s.push('\n');
    s
}

pub fn env_get(text: &str, key: &str) -> Option<String> {
    text.lines().rev().find_map(|l| {
        let t = l.trim_start();
        let t = t.strip_prefix("export ").unwrap_or(t);
        let rest = t.strip_prefix(key)?.trim_start().strip_prefix('=')?;
        Some(rest.trim().trim_matches('"').trim_matches('\'').to_string())
    })
}

/// SOUL.md with the int3 block (between the markers) replaced or appended; the user's own text kept.
pub fn soul_with(existing: &str, begin: &str, end: &str, block: &str) -> String {
    let managed = format!("{begin}\n{block}\n{end}");
    if let (Some(b), Some(e)) = (existing.find(begin), existing.find(end)) {
        if b < e {
            return format!("{}{}{}", &existing[..b], managed, &existing[e + end.len()..]);
        }
    }
    let base = existing.trim_end();
    if base.is_empty() { format!("{managed}\n") } else { format!("{base}\n\n{managed}\n") }
}

/// True when `path` (already canonical) is inside a cloud-synced folder: a OneDrive root named by the
/// environment, or a well-known sync client's folder anywhere on the path.
pub fn is_synced(path: &Path, onedrive_roots: &[PathBuf]) -> bool {
    let lower = path.to_string_lossy().to_lowercase().replace('/', "\\");
    if onedrive_roots.iter().filter(|r| !r.as_os_str().is_empty()).any(|r| {
        let root = r.to_string_lossy().to_lowercase().replace('/', "\\");
        let root = root.trim_end_matches('\\');
        lower == root || lower.starts_with(&format!("{root}\\"))
    }) {
        return true;
    }
    ["\\onedrive", "\\dropbox", "\\google drive", "\\googledrive", "\\icloud drive", "\\iclouddrive", "\\box\\"]
        .iter()
        .any(|m| lower.contains(m))
}

fn onedrive_roots() -> Vec<PathBuf> {
    ["OneDrive", "OneDriveCommercial", "OneDriveConsumer"]
        .iter()
        .filter_map(|k| std::env::var(k).ok())
        .filter(|v| !v.trim().is_empty())
        .map(|v| std::fs::canonicalize(&v).unwrap_or_else(|_| PathBuf::from(v)))
        .collect()
}

/// Create the workspace and refuse it if it resolves into a synced folder, or is itself a link
/// (a junction could point anywhere).
pub fn prepare_workspace(dir: &Path) -> Result<(PathBuf, bool), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let meta = std::fs::symlink_metadata(dir).map_err(|e| e.to_string())?;
    #[cfg(windows)]
    let is_link = {
        use std::os::windows::fs::MetadataExt;
        meta.file_attributes() & 0x400 != 0 // FILE_ATTRIBUTE_REPARSE_POINT (symlink or junction)
    };
    #[cfg(not(windows))]
    let is_link = meta.file_type().is_symlink();
    let canonical = std::fs::canonicalize(dir).map_err(|e| e.to_string())?;
    let display = PathBuf::from(canonical.to_string_lossy().trim_start_matches(r"\\?\").to_string());
    Ok((display.clone(), is_link || is_synced(&display, &onedrive_roots())))
}

fn get<'a>(v: &'a Value, path: &[&str]) -> Option<&'a Value> {
    path.iter().try_fold(v, |acc, k| acc.get(*k))
}

/// The report the server judges (app/launcher/local_policy.py::evaluate): read back from config.yaml,
/// .env and the plugins folder, never from what we meant to write.
pub fn build_report(config_yaml: &str, env_text: &str, plugins: Vec<String>, mcp_name: &str,
                    workspace_synced: bool) -> Value {
    let cfg: Value = serde_yaml::from_str::<serde_yaml::Value>(config_yaml)
        .ok()
        .and_then(|y| serde_json::to_value(y).ok())
        .unwrap_or(Value::Null);
    let list = |p: &[&str]| -> Value {
        get(&cfg, p).and_then(Value::as_array).map(|a| Value::Array(a.clone())).unwrap_or(Value::Null)
    };
    let servers: Vec<Value> = get(&cfg, &["mcp_servers"])
        .and_then(Value::as_object)
        .map(|m| m.keys().map(|k| Value::String(k.clone())).collect())
        .unwrap_or_default();
    json!({
        "disabled_toolsets": list(&["agent", "disabled_toolsets"]),
        "approvals_mode": get(&cfg, &["approvals", "mode"]).cloned().unwrap_or(Value::Null),
        "cron_mode": get(&cfg, &["approvals", "cron_mode"]).cloned().unwrap_or(Value::Null),
        "memory_enabled": get(&cfg, &["memory", "memory_enabled"]).cloned().unwrap_or(Value::Null),
        "user_profile_enabled": get(&cfg, &["memory", "user_profile_enabled"]).cloned().unwrap_or(Value::Null),
        "mcp_servers": servers,
        "mcp_url": get(&cfg, &["mcp_servers", mcp_name, "url"]).cloned().unwrap_or(Value::Null),
        "plugins": plugins,
        "write_safe_root": env_get(env_text, SAFE_ROOT_KEY).unwrap_or_default(),
        "workspace_synced": workspace_synced,
    })
}

pub fn installed_plugins(home: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(home.join("plugins"))
        .map(|rd| {
            rd.filter_map(Result::ok)
                .filter(|e| e.path().is_dir())
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    names
}

pub fn read_text(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

pub fn write_atomic(path: &Path, contents: &str) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("int3-tmp");
    std::fs::write(&tmp, contents)?;
    std::fs::rename(&tmp, path)
}

/// Apply the server's policy: every config key through Hermes's own CLI, then SOUL.md and .env.
/// Returns the canonical workspace path and whether it is in a synced folder.
pub fn apply(policy: &Value) -> Result<(PathBuf, bool), String> {
    let home = hermes_home();
    let config: &Map<String, Value> = policy["config"].as_object().ok_or("bad policy from the server")?;
    for (key, value) in config {
        config_set(key, value)?;
    }
    let soul = &policy["soul"];
    let soul_path = home.join("SOUL.md");
    let merged = soul_with(
        &read_text(&soul_path),
        soul["begin"].as_str().unwrap_or_default(),
        soul["end"].as_str().unwrap_or_default(),
        soul["block"].as_str().unwrap_or_default(),
    );
    write_atomic(&soul_path, &merged).map_err(|e| format!("cannot update SOUL.md: {e}"))?;
    let (ws, synced) = prepare_workspace(&workspace(policy["workspace_dirname"].as_str().unwrap_or("int3-workspace")))?;
    let env_path = home.join(".env");
    let env = env_with(&read_text(&env_path), SAFE_ROOT_KEY, Some(&ws.to_string_lossy()));
    write_atomic(&env_path, &env).map_err(|e| format!("cannot update Hermes's .env: {e}"))?;
    Ok((ws, synced))
}

pub fn report(policy_mcp_name: &str, workspace_synced: bool) -> Value {
    let home = hermes_home();
    build_report(
        &read_text(&home.join("config.yaml")),
        &read_text(&home.join(".env")),
        installed_plugins(&home),
        policy_mcp_name,
        workspace_synced,
    )
}

/// Write (Some) or remove (None) the brain token in Hermes's .env. Never logged.
pub fn set_token(token: Option<&str>) -> Result<(), String> {
    let env_path = hermes_home().join(".env");
    let text = read_text(&env_path);
    if token.is_none() && env_get(&text, TOKEN_KEY).is_none() {
        return Ok(());
    }
    write_atomic(&env_path, &env_with(&text, TOKEN_KEY, token)).map_err(|e| format!("cannot update Hermes's .env: {e}"))
}

/// Newest modification time of what the report reads: config.yaml, .env and the plugins folder.
pub fn watched_stamp() -> Option<std::time::SystemTime> {
    let home = hermes_home();
    [home.join("config.yaml"), home.join(".env"), home.join("plugins")]
        .iter()
        .filter_map(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok())
        .max()
}

/// Hermes's Providers settings (apps/desktop/src/app/settings/index.tsx:123,364 and the generic
/// `hermes://open/<path>` route, apps/desktop/src/lib/hermes-open-target.ts:1-29 @v0.23.1). A dev
/// build registers `hermes-dev://` instead (apps/desktop/electron/main.ts:17353).
pub fn providers_link() -> String {
    let scheme = std::env::var("CB_LAUNCHER_HERMES_SCHEME").unwrap_or_else(|_| "hermes".into());
    format!("{scheme}://open/settings?tab=providers")
}

#[cfg(test)]
mod tests {
    use super::*;

    const CFG: &str = "agent:\n  disabled_toolsets:\n    - terminal\n    - web\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\napprovals:\n  mode: manual\n  cron_mode: deny\nmcp_servers:\n  int3_brain:\n    url: https://b.example/mcp\n    headers:\n      Authorization: Bearer ${INT3_BRAIN_TOKEN}\n";

    #[test]
    fn report_reads_back_what_is_on_disk() {
        let r = build_report(CFG, "X=1\nHERMES_WRITE_SAFE_ROOT=C:\\Users\\p\\int3-workspace\n", vec![], "int3_brain", false);
        assert_eq!(r["disabled_toolsets"], json!(["terminal", "web"]));
        assert_eq!(r["approvals_mode"], "manual");
        assert_eq!(r["memory_enabled"], false);
        assert_eq!(r["mcp_servers"], json!(["int3_brain"]));
        assert_eq!(r["mcp_url"], "https://b.example/mcp");
        assert_eq!(r["write_safe_root"], "C:\\Users\\p\\int3-workspace");
        assert_eq!(r["workspace_synced"], false);
    }

    #[test]
    fn unreadable_config_reports_nulls_not_defaults() {
        let r = build_report("{not yaml", "", vec!["x".into()], "int3_brain", false);
        assert!(r["disabled_toolsets"].is_null() && r["approvals_mode"].is_null());
        assert_eq!(r["plugins"], json!(["x"]));
    }

    #[test]
    fn env_edits_touch_only_our_key() {
        let t = "OPENAI_API_KEY=k\nINT3_BRAIN_TOKEN=old\nexport FOO=bar\n";
        let set = env_with(t, TOKEN_KEY, Some("new"));
        assert_eq!(env_get(&set, TOKEN_KEY).as_deref(), Some("new"));
        assert!(set.contains("OPENAI_API_KEY=k") && set.contains("export FOO=bar"));
        assert_eq!(set.matches("INT3_BRAIN_TOKEN").count(), 1);
        let removed = env_with(&set, TOKEN_KEY, None);
        assert!(env_get(&removed, TOKEN_KEY).is_none() && removed.contains("OPENAI_API_KEY=k"));
        // a key that merely starts with ours is not ours
        assert!(env_with("INT3_BRAIN_TOKEN_OLD=x\n", TOKEN_KEY, None).contains("INT3_BRAIN_TOKEN_OLD=x"));
    }

    #[test]
    fn soul_block_is_replaced_not_duplicated_and_user_text_kept() {
        let first = soul_with("I am Hermes.", "<b>", "<e>", "v1");
        assert_eq!(first, "I am Hermes.\n\n<b>\nv1\n<e>\n");
        let second = soul_with(&first, "<b>", "<e>", "v2");
        assert_eq!(second.matches("<b>").count(), 1);
        assert!(second.contains("v2") && !second.contains("v1") && second.starts_with("I am Hermes."));
        assert_eq!(soul_with("", "<b>", "<e>", "x"), "<b>\nx\n<e>\n");
    }

    #[test]
    fn synced_folders_are_detected() {
        let od = vec![PathBuf::from(r"C:\Users\p\OneDrive - Acme")];
        assert!(is_synced(Path::new(r"C:\Users\p\OneDrive - Acme\int3-workspace"), &od));
        assert!(is_synced(Path::new(r"c:\users\p\onedrive - acme"), &od));
        assert!(is_synced(Path::new(r"D:\Dropbox\int3-workspace"), &[]));
        assert!(is_synced(Path::new(r"C:\Users\p\OneDrive\x"), &[]));
        assert!(!is_synced(Path::new(r"C:\Users\p\int3-workspace"), &od));
    }

    #[test]
    fn literals_for_hermes_config_set() {
        assert_eq!(config_literal(&json!(false)), "false");
        assert_eq!(config_literal(&json!(0)), "0");
        assert_eq!(config_literal(&json!("manual")), "manual");
        assert_eq!(config_literal(&json!(["terminal", "web"])), r#"["terminal","web"]"#);
        assert_eq!(config_literal(&json!({"url": "u"})), r#"{"url":"u"}"#);
    }

    #[test]
    fn providers_link_targets_the_providers_tab() {
        assert!(providers_link().ends_with("://open/settings?tab=providers"));
    }
}
