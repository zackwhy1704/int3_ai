//! The launcher's half of /launcher/* (backend/app/launcher/routes.py), and its device session.
//!
//! Token handling (docs/LAUNCHER_AUTH_NOTE.md; M0 token model):
//!   * refresh token (30 d, rotating, reuse revokes the device): OS credential store only;
//!   * access token (15 min): process memory only;
//!   * desktop token (12 h, per launch): handed straight to registry.rs for Hermes's
//!     connections.json and never kept here.
//! None of them is ever returned to the web page.

use serde_json::{json, Value};
use std::sync::Mutex;
use url::Url;

pub const KEYRING_SERVICE: &str = "CompanyBrainLauncher";

#[derive(Debug)]
pub enum ApiError {
    /// Signed out: no refresh token, or the server rejected it (device revoked).
    SignedOut,
    Http(u16, String),
    Network(String),
    Other(String),
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApiError::SignedOut => write!(f, "signed out"),
            ApiError::Http(code, detail) => write!(f, "server answered {code}: {detail}"),
            ApiError::Network(e) => write!(f, "cannot reach the server ({e})"),
            ApiError::Other(e) => write!(f, "{e}"),
        }
    }
}

/// `<server>/invite#<code>` (backend/app/launcher/admin.py prints this). The server is the link's
/// origin; https is required except on loopback (local stack / tests).
pub fn parse_invite(link: &str) -> Result<(String, String), String> {
    let url = Url::parse(link.trim()).map_err(|_| "That is not an invite link.".to_string())?;
    let code = url.fragment().unwrap_or_default().trim().to_string();
    if url.path().trim_end_matches('/') != "/invite" || !code.starts_with("cb_inv_") {
        return Err("That is not an invite link.".into());
    }
    let host = url.host_str().unwrap_or_default();
    let loopback = matches!(host, "127.0.0.1" | "localhost" | "[::1]");
    if url.scheme() != "https" && !(url.scheme() == "http" && loopback) {
        return Err("Invite links must use https.".into());
    }
    let server = url.origin().ascii_serialization();
    Ok((server, code))
}

pub struct Session {
    http: reqwest::Client,
    server: Mutex<Option<String>>,
    access: Mutex<Option<String>>,
    keyring_service: String,
}

impl Session {
    pub fn new(server: Option<String>) -> Self {
        Session {
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(20))
                .build()
                .expect("http client"),
            server: Mutex::new(server),
            access: Mutex::new(None),
            keyring_service: std::env::var("CB_LAUNCHER_KEYRING_SERVICE").unwrap_or_else(|_| KEYRING_SERVICE.into()),
        }
    }

    pub fn server(&self) -> Option<String> {
        self.server.lock().unwrap().clone()
    }

    fn entry(&self, server: &str) -> Result<keyring::Entry, ApiError> {
        keyring::Entry::new(&self.keyring_service, server).map_err(|e| ApiError::Other(e.to_string()))
    }

    pub fn has_refresh(&self) -> bool {
        self.server().map_or(false, |s| self.entry(&s).and_then(|e| e.get_password().map_err(|e| ApiError::Other(e.to_string()))).is_ok())
    }

    fn store_pair(&self, server: &str, pair: &Value) -> Result<(), ApiError> {
        let refresh = pair["refresh_token"].as_str().ok_or_else(|| ApiError::Other("no refresh token in reply".into()))?;
        self.entry(server)?.set_password(refresh).map_err(|e| ApiError::Other(e.to_string()))?;
        *self.access.lock().unwrap() = pair["access_token"].as_str().map(str::to_string);
        Ok(())
    }

    async fn call(&self, method: reqwest::Method, server: &str, path: &str, bearer: Option<&str>, body: Option<Value>) -> Result<Value, ApiError> {
        let mut req = self.http.request(method, format!("{server}{path}"));
        if let Some(t) = bearer {
            req = req.bearer_auth(t);
        }
        if let Some(b) = body {
            req = req.json(&b);
        }
        let resp = req.send().await.map_err(|e| ApiError::Network(e.without_url().to_string()))?;
        let status = resp.status().as_u16();
        let value: Value = resp.json().await.unwrap_or(Value::Null);
        if (200..300).contains(&status) {
            Ok(value)
        } else {
            Err(ApiError::Http(status, value["detail"].as_str().unwrap_or("").to_string()))
        }
    }

    /// Redeem an invite link: a new device session on this machine.
    pub async fn sign_in(&self, link: &str, device_label: &str) -> Result<String, ApiError> {
        let (server, code) = parse_invite(link).map_err(ApiError::Other)?;
        let pair = self
            .call(reqwest::Method::POST, &server, "/launcher/auth/invite", None,
                  Some(json!({"invite": code, "label": device_label, "platform": "windows"})))
            .await?;
        self.store_pair(&server, &pair)?;
        *self.server.lock().unwrap() = Some(server.clone());
        Ok(server)
    }

    async fn refresh(&self, server: &str) -> Result<String, ApiError> {
        let entry = self.entry(server)?;
        let refresh = entry.get_password().map_err(|_| ApiError::SignedOut)?;
        match self.call(reqwest::Method::POST, server, "/launcher/auth/refresh", None, Some(json!({"refresh_token": refresh}))).await {
            Ok(pair) => {
                self.store_pair(server, &pair)?;
                Ok(self.access.lock().unwrap().clone().unwrap_or_default())
            }
            Err(ApiError::Http(401, _)) => {
                // Revoked, expired or reused: this device is signed out for good.
                let _ = entry.delete_credential();
                *self.access.lock().unwrap() = None;
                Err(ApiError::SignedOut)
            }
            Err(e) => Err(e),
        }
    }

    /// An authenticated call; refreshes the access token once on 401.
    pub async fn authed(&self, method: reqwest::Method, path: &str, body: Option<Value>) -> Result<Value, ApiError> {
        let server = self.server().ok_or(ApiError::SignedOut)?;
        let access = self.access.lock().unwrap().clone();
        let access = match access {
            Some(a) => a,
            None => self.refresh(&server).await?,
        };
        match self.call(method.clone(), &server, path, Some(&access), body.clone()).await {
            Err(ApiError::Http(401, _)) => {
                let fresh = self.refresh(&server).await?;
                self.call(method, &server, path, Some(&fresh), body).await
            }
            other => other,
        }
    }

    /// Revoke this device on the server (best effort) and forget every local credential.
    pub async fn sign_out(&self) {
        if let Some(server) = self.server() {
            let _ = self.authed(reqwest::Method::POST, "/launcher/auth/revoke", None).await;
            if let Ok(e) = self.entry(&server) {
                let _ = e.delete_credential();
            }
        }
        *self.access.lock().unwrap() = None;
        *self.server.lock().unwrap() = None;
    }

    /// Unauthenticated reachability of the user's backend (/api/status is the one route the
    /// ingress passes without a token: ingress_auth.py).
    pub async fn backend_online(&self, url: &str) -> bool {
        self.http.get(format!("{}/api/status", url.trim_end_matches('/'))).send().await.map_or(false, |r| r.status().is_success())
    }
}

#[cfg(test)]
mod tests {
    use super::parse_invite;

    #[test]
    fn invite_link_gives_server_and_code() {
        assert_eq!(
            parse_invite("https://brain.example.com/invite#cb_inv_abc").unwrap(),
            ("https://brain.example.com".to_string(), "cb_inv_abc".to_string())
        );
        assert_eq!(parse_invite(" http://127.0.0.1:8100/invite#cb_inv_x ").unwrap().0, "http://127.0.0.1:8100");
    }

    #[test]
    fn rejects_plain_http_off_loopback_and_non_invites() {
        assert!(parse_invite("http://brain.example.com/invite#cb_inv_abc").is_err());
        assert!(parse_invite("https://brain.example.com/invite").is_err());
        assert!(parse_invite("https://brain.example.com/other#cb_inv_abc").is_err());
        assert!(parse_invite("https://brain.example.com/invite#token").is_err());
        assert!(parse_invite("cb_inv_abc").is_err());
    }
}
