#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! int3 launcher (M1, Windows alpha). Sign in with an invite link, see your brains by
//! name, launch STOCK Hermes Desktop against your own gated backend, switch user, sign out.
//!
//! On disk this launcher keeps exactly one file, launcher.json = {"server": "<origin>"}; the
//! refresh token is in Windows Credential Manager. No brain content is stored anywhere by it.

mod api;
mod hermes;
mod registry;

use api::{ApiError, Session};
use reqwest::Method;
use serde_json::{json, Value};
use std::path::PathBuf;
use tauri::State;

/// Nous's end-user download page for Hermes Desktop, as linked from the v0.22.6 docs
/// (website/docs/user-guide/desktop.md:25 "Hermes Desktop product page"). GitHub releases carry
/// no installers; the product page links the Microsoft Store listing and the .appinstaller.
const HERMES_DOWNLOAD: &str = "https://hermes-agent.nousresearch.com/desktop";

fn data_dir() -> PathBuf {
    std::env::var("CB_LAUNCHER_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(std::env::var("APPDATA").unwrap_or_default()).join("int3"))
}

fn config_path() -> PathBuf {
    data_dir().join("launcher.json")
}

fn load_server() -> Option<String> {
    let text = std::fs::read_to_string(config_path()).ok()?;
    serde_json::from_str::<Value>(&text).ok()?["server"].as_str().map(str::to_string)
}

fn save_server(server: Option<&str>) -> Result<(), String> {
    match server {
        Some(s) => {
            std::fs::create_dir_all(data_dir()).map_err(|e| e.to_string())?;
            std::fs::write(config_path(), json!({ "server": s }).to_string()).map_err(|e| e.to_string())
        }
        None => match std::fs::remove_file(config_path()) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.to_string()),
            _ => Ok(()),
        },
    }
}

/// Errors cross to the page as {kind, message}; `signed_out` sends it back to the sign-in screen.
fn err(e: ApiError) -> Value {
    match e {
        ApiError::SignedOut => json!({"kind": "signed_out", "message": "You are signed out on this device."}),
        other => json!({"kind": "error", "message": other.to_string()}),
    }
}

fn fail(message: impl Into<String>) -> Value {
    json!({"kind": "error", "message": message.into()})
}

#[tauri::command]
fn status(session: State<'_, Session>) -> Value {
    json!({"signed_in": session.has_refresh(), "server": session.server()})
}

#[tauri::command]
async fn sign_in(session: State<'_, Session>, invite_link: String) -> Result<Value, Value> {
    let label = std::env::var("COMPUTERNAME").unwrap_or_else(|_| "Windows PC".into());
    let server = session.sign_in(&invite_link, &label).await.map_err(err)?;
    save_server(Some(&server)).map_err(fail)?;
    me(session).await
}

#[tauri::command]
async fn me(session: State<'_, Session>) -> Result<Value, Value> {
    session.authed(Method::GET, "/launcher/me", None).await.map_err(err)
}

#[tauri::command]
async fn account(session: State<'_, Session>) -> Result<Value, Value> {
    let me = session.authed(Method::GET, "/launcher/me", None).await.map_err(err)?;
    let devices = session.authed(Method::GET, "/launcher/devices", None).await.map_err(err)?;
    let compat = session.authed(Method::GET, "/launcher/compat", None).await.map_err(err)?;
    Ok(json!({"me": me, "devices": devices, "server": session.server(), "backend_release": compat["backend_release"]}))
}

#[tauri::command]
async fn revoke_device(session: State<'_, Session>, device_id: String) -> Result<Value, Value> {
    if !device_id.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
        return Err(fail("bad device id"));
    }
    session.authed(Method::POST, &format!("/launcher/devices/{device_id}/revoke"), None).await.map_err(err)
}

/// Admin only: mint a one-time key-entry link and open it in the default browser. The provider
/// key goes browser -> key intake; it never passes through the launcher.
#[tauri::command]
async fn byok_link(session: State<'_, Session>) -> Result<Value, Value> {
    let link = session.authed(Method::POST, "/launcher/byok/link", Some(json!({"provider": "anthropic"}))).await.map_err(err)?;
    let url = link["url"].as_str().unwrap_or_default();
    let parsed = url::Url::parse(url).map_err(|_| fail("the server returned a bad link"))?;
    if !matches!(parsed.scheme(), "https" | "http") {
        return Err(fail("the server returned a bad link"));
    }
    std::process::Command::new("rundll32").args(["url.dll,FileProtocolHandler", parsed.as_str()]).spawn().map_err(|e| fail(e.to_string()))?;
    Ok(json!({"opened": true, "expires_in_hours": link["expires_in_hours"], "replaces_existing_key": link["replaces_existing_key"]}))
}

/// Open Nous's download page (a constant: the page cannot choose what is opened).
#[tauri::command]
fn open_hermes_download() -> Result<(), Value> {
    std::process::Command::new("rundll32").args(["url.dll,FileProtocolHandler", HERMES_DOWNLOAD]).spawn().map_err(|e| fail(e.to_string()))?;
    Ok(())
}

#[tauri::command]
async fn hermes_status(session: State<'_, Session>) -> Result<Value, Value> {
    let Some(install) = hermes::detect() else {
        let build = hermes::os_build();
        return Ok(json!({
            "found": false,
            "download": HERMES_DOWNLOAD,
            "os_build": build,
            "os_supported": build.map_or(true, |b| b >= hermes::MSIX_MIN_BUILD),
        }));
    };
    let compat = session.authed(Method::GET, "/launcher/compat", None).await.map_err(err)?;
    let policy = hermes::policy(&install, &compat);
    Ok(json!({"found": true, "version": install.version, "policy": policy, "running": hermes::running(&install)}))
}

/// Launch: refuse early (no Hermes, blocked version, a connections.json we cannot write safely,
/// or a running Hermes the user has not agreed to restart), THEN mint the desktop token, write it
/// into every Hermes userData folder, and start Hermes.
#[tauri::command]
async fn launch(session: State<'_, Session>, restart: bool) -> Result<Value, Value> {
    let install = hermes::detect().ok_or_else(|| fail(format!("Hermes Desktop is not installed. Get it from {HERMES_DOWNLOAD}")))?;
    let compat = session.authed(Method::GET, "/launcher/compat", None).await.map_err(err)?;
    let policy = hermes::policy(&install, &compat);
    if policy.state == "blocked" {
        return Err(fail(policy.message));
    }
    let mut existing = Vec::new();
    for dir in &install.user_data_dirs {
        let text = registry::read(dir).map_err(|e| fail(format!("cannot read {}: {e}", dir.display())))?;
        registry::merge_signout(text.as_deref()).map_err(|e| fail(e.to_string()))?; // readability pre-check only
        existing.push(text);
    }
    let running = hermes::running(&install);
    if running && !restart {
        return Ok(json!({"needs_restart": true}));
    }

    let me = session.authed(Method::GET, "/launcher/me", None).await.map_err(err)?;
    let grant = session.authed(Method::POST, "/launcher/launch", None).await.map_err(err)?;
    let id = format!("cb-{}-{}", me["tenant"]["id"].as_str().unwrap_or("t"), me["user"]["id"].as_str().unwrap_or("u"));
    let url = grant["backend"]["url"].as_str().unwrap_or_default().to_string();
    let entry = registry::our_entry(&id, grant["backend"]["label"].as_str().unwrap_or("int3"), &url,
                                    grant["desktop_token"].as_str().unwrap_or_default());
    for (dir, text) in install.user_data_dirs.iter().zip(existing) {
        let merged = registry::merge_launch(text.as_deref(), entry.clone()).map_err(|e| fail(e.to_string()))?;
        registry::write_atomic(dir, &merged).map_err(|e| fail(format!("cannot write {}: {e}", dir.display())))?;
    }
    if running {
        hermes::stop(&install);
    }
    hermes::start(&install).map_err(|e| fail(format!("could not start Hermes: {e}")))?;
    Ok(json!({
        "launched": true,
        "label": grant["backend"]["label"],
        "expires_at": grant["expires_at"],
        "policy": policy,
        "backend_online": session.backend_online(&url).await,
    }))
}

/// Sign out (also how you switch user): revoke this device, remove our entries from Hermes's
/// connections.json (primary falls back to "local"), forget the server and the refresh token.
#[tauri::command]
async fn sign_out(session: State<'_, Session>) -> Result<Value, Value> {
    session.sign_out().await;
    let mut cleaned = 0;
    if let Some(install) = hermes::detect() {
        for dir in &install.user_data_dirs {
            let text = registry::read(dir).map_err(|e| fail(e.to_string()))?;
            if let Some(merged) = registry::merge_signout(text.as_deref()).map_err(|e| fail(e.to_string()))? {
                registry::write_atomic(dir, &merged).map_err(|e| fail(e.to_string()))?;
                cleaned += 1;
            }
        }
    }
    save_server(None).map_err(fail)?;
    Ok(json!({"signed_out": true, "registries_cleaned": cleaned}))
}

fn main() {
    tauri::Builder::default()
        .manage(Session::new(load_server()))
        .invoke_handler(tauri::generate_handler![status, sign_in, me, account, revoke_device, byok_link, hermes_status, open_hermes_download, launch, sign_out])
        .run(tauri::generate_context!())
        .expect("error while running the int3 launcher");
}
