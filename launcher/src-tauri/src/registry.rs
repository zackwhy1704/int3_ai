//! Hermes Desktop's connection registry (`connections.json`), merged safely.
//!
//! Shape: stock Hermes v2 registry, `REGISTRY_VERSION = 2` (apps/desktop/electron/
//! connection-registry.ts:40 @v0.22.6, byte-identical to v0.21.7), normalized by Hermes's own
//! `normalizeRegistry` (:1206). The token envelope `{encoding:'plain', value}` is what Hermes
//! itself writes under its default secret policy (main.ts:8066-8070 @v0.22.6).
//!
//! Rules (ported from deploy/hermes/connect-desktop.ps1, stricter):
//!   * we only ever add/remove entries whose id starts with `cb-` (ours);
//!   * `local`, every other connection, `quarantined`, and unknown keys are kept verbatim;
//!   * a file we cannot read as a v2 registry is REFUSED, never overwritten (connect-desktop
//!     started a fresh registry instead; that would drop the user's own connections);
//!   * output is UTF-8 without a BOM (Node's JSON.parse rejects one), written atomically.

use serde_json::{json, Map, Value};
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::Path;

pub const REGISTRY_VERSION: u64 = 2;
pub const OUR_PREFIX: &str = "cb-";
pub const FILE_NAME: &str = "connections.json";

#[derive(Debug, PartialEq)]
pub enum RegistryError {
    /// Not JSON, or JSON that is not an object / has no connection list.
    Unreadable(String),
    /// A registry version this launcher was not built against.
    UnsupportedVersion(String),
}

impl fmt::Display for RegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RegistryError::Unreadable(why) => write!(f, "Hermes's connections.json is unreadable ({why}); refusing to overwrite it"),
            RegistryError::UnsupportedVersion(v) => write!(f, "Hermes's connections.json is version {v}, this launcher writes only version {REGISTRY_VERSION}; refusing to touch it"),
        }
    }
}

/// The one remote entry we own for a signed-in user.
pub fn our_entry(id: &str, label: &str, url: &str, token: &str) -> Value {
    json!({
        "id": id,
        "kind": "remote",
        "label": label,
        "url": url,
        "authMode": "token",
        "token": { "encoding": "plain", "value": token },
    })
}

fn local_entry() -> Value {
    json!({ "id": "local", "kind": "local", "label": "This device" })
}

fn is_ours(entry: &Value) -> bool {
    entry.get("id").and_then(Value::as_str).map_or(false, |id| id.starts_with(OUR_PREFIX))
}

/// Parse an existing file into its top-level object, refusing anything we would not write safely.
fn parse(existing: &str) -> Result<Map<String, Value>, RegistryError> {
    let text = existing.strip_prefix('\u{feff}').unwrap_or(existing);
    let value: Value = serde_json::from_str(text).map_err(|e| RegistryError::Unreadable(e.to_string()))?;
    let Value::Object(map) = value else {
        return Err(RegistryError::Unreadable("top level is not an object".into()));
    };
    match map.get("version") {
        Some(Value::Number(n)) if n.as_u64() == Some(REGISTRY_VERSION) => {}
        Some(other) => return Err(RegistryError::UnsupportedVersion(other.to_string())),
        None => return Err(RegistryError::UnsupportedVersion("missing".into())),
    }
    match map.get("connections") {
        Some(Value::Array(_)) | None => Ok(map),
        Some(_) => Err(RegistryError::Unreadable("connections is not a list".into())),
    }
}

fn render(map: Map<String, Value>) -> String {
    let mut out = serde_json::to_string_pretty(&Value::Object(map)).expect("a JSON map always serializes");
    out.push('\n');
    out
}

/// Registry after a launch: every `cb-` entry (a previous user on this machine, or a stale token
/// of this one) is dropped, `entry` is added, and it becomes the primary + last-used connection.
pub fn merge_launch(existing: Option<&str>, entry: Value) -> Result<String, RegistryError> {
    let id = entry.get("id").and_then(Value::as_str).unwrap_or_default().to_string();
    let mut map = match existing {
        Some(text) => parse(text)?,
        None => Map::new(),
    };
    let mut connections: Vec<Value> = match map.remove("connections") {
        Some(Value::Array(list)) => list.into_iter().filter(|c| !is_ours(c)).collect(),
        _ => Vec::new(),
    };
    if !connections.iter().any(|c| c.get("kind").and_then(Value::as_str) == Some("local")) {
        connections.insert(0, local_entry());
    }
    connections.push(entry);
    map.insert("version".into(), json!(REGISTRY_VERSION));
    map.insert("primary".into(), json!(id));
    match map.get("launchMode").and_then(Value::as_str) {
        Some("primary") | Some("last-used") => {}
        _ => {
            map.insert("launchMode".into(), json!("primary"));
        }
    }
    map.insert("lastUsed".into(), json!(id));
    map.insert("connections".into(), Value::Array(connections));
    Ok(render(map))
}

/// Registry after sign-out: our entries are removed and anything that pointed at them falls back
/// to `local`. `Ok(None)` when there is no file (nothing to clean).
pub fn merge_signout(existing: Option<&str>) -> Result<Option<String>, RegistryError> {
    let Some(text) = existing else { return Ok(None) };
    let mut map = parse(text)?;
    if let Some(Value::Array(list)) = map.remove("connections") {
        map.insert("connections".into(), Value::Array(list.into_iter().filter(|c| !is_ours(c)).collect()));
    }
    for key in ["primary", "lastUsed"] {
        if map.get(key).and_then(Value::as_str).map_or(false, |v| v.starts_with(OUR_PREFIX)) {
            map.insert(key.into(), json!("local"));
        }
    }
    Ok(Some(render(map)))
}

/// Read `dir/connections.json` if it exists.
pub fn read(dir: &Path) -> std::io::Result<Option<String>> {
    match fs::read(dir.join(FILE_NAME)) {
        Ok(bytes) => Ok(Some(String::from_utf8_lossy(&bytes).into_owned())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

/// Atomic write: a temp file in the same folder, flushed, then renamed over the original
/// (std::fs::rename replaces an existing file on Windows too).
pub fn write_atomic(dir: &Path, contents: &str) -> std::io::Result<()> {
    fs::create_dir_all(dir)?;
    let tmp = dir.join(format!("{FILE_NAME}.cb-tmp"));
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(contents.as_bytes())?;
        f.sync_all()?;
    }
    fs::rename(&tmp, dir.join(FILE_NAME))
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "cb-brindlewood-priya";

    fn ours() -> Value {
        our_entry(ID, "Company Brain - priya@brindlewood", "http://127.0.0.1:9432", "cb_dt_test")
    }
    fn parsed(s: &str) -> Value {
        serde_json::from_str(s).unwrap()
    }
    fn ids(v: &Value) -> Vec<String> {
        v["connections"].as_array().unwrap().iter().map(|c| c["id"].as_str().unwrap().to_string()).collect()
    }

    #[test]
    fn fresh_machine_gets_local_plus_ours_as_primary() {
        let v = parsed(&merge_launch(None, ours()).unwrap());
        assert_eq!(v["version"], 2);
        assert_eq!(v["primary"], ID);
        assert_eq!(v["lastUsed"], ID);
        assert_eq!(v["launchMode"], "primary");
        assert_eq!(ids(&v), vec!["local", ID]);
        assert_eq!(v["connections"][1]["token"], json!({"encoding": "plain", "value": "cb_dt_test"}));
    }

    #[test]
    fn keeps_local_other_connections_quarantine_and_unknown_keys() {
        let existing = r#"{"version":2,"primary":"local","launchMode":"last-used","lastUsed":"home",
          "futureKey":{"x":1},
          "connections":[{"id":"local","kind":"local","label":"My PC"},
                         {"id":"home","kind":"remote","label":"Home server","url":"https://h.example","authMode":"oauth"}],
          "quarantined":[{"reason":"entry-malformed","entry":"junk"}]}"#;
        let v = parsed(&merge_launch(Some(existing), ours()).unwrap());
        assert_eq!(ids(&v), vec!["local", "home", ID]);
        assert_eq!(v["connections"][0]["label"], "My PC");
        assert_eq!(v["connections"][1]["authMode"], "oauth");
        assert_eq!(v["futureKey"], json!({"x": 1}));
        assert_eq!(v["quarantined"][0]["entry"], "junk");
        assert_eq!(v["launchMode"], "last-used", "a valid user choice is kept");
        assert_eq!(v["primary"], ID);
    }

    #[test]
    fn switching_user_removes_every_previous_cb_entry() {
        let first = merge_launch(None, our_entry("cb-brindlewood-ada", "Company Brain - ada@brindlewood", "http://127.0.0.1:9433", "cb_dt_ada")).unwrap();
        let v = parsed(&merge_launch(Some(&first), ours()).unwrap());
        assert_eq!(ids(&v), vec!["local", ID]);
        assert!(!serde_json::to_string(&v).unwrap().contains("cb_dt_ada"), "the previous user's token must be gone");
    }

    #[test]
    fn relaunch_is_idempotent() {
        let once = merge_launch(None, ours()).unwrap();
        let twice = merge_launch(Some(&once), ours()).unwrap();
        assert_eq!(once, twice);
    }

    #[test]
    fn local_entry_is_added_when_missing() {
        let existing = r#"{"version":2,"primary":"x","launchMode":"primary","lastUsed":"x","connections":[{"id":"x","kind":"remote","label":"X","url":"https://x.example"}]}"#;
        let v = parsed(&merge_launch(Some(existing), ours()).unwrap());
        assert_eq!(ids(&v), vec!["local", "x", ID]);
    }

    #[test]
    fn refuses_corrupt_json_and_non_objects() {
        assert!(matches!(merge_launch(Some("{not json"), ours()), Err(RegistryError::Unreadable(_))));
        assert!(matches!(merge_launch(Some("[1,2]"), ours()), Err(RegistryError::Unreadable(_))));
        assert!(matches!(merge_launch(Some(r#"{"version":2,"connections":{}}"#), ours()), Err(RegistryError::Unreadable(_))));
        assert!(matches!(merge_signout(Some("{not json")), Err(RegistryError::Unreadable(_))));
    }

    #[test]
    fn refuses_other_or_missing_versions() {
        assert!(matches!(merge_launch(Some(r#"{"version":3,"connections":[]}"#), ours()), Err(RegistryError::UnsupportedVersion(_))));
        assert!(matches!(merge_launch(Some(r#"{"version":"2","connections":[]}"#), ours()), Err(RegistryError::UnsupportedVersion(_))));
        assert!(matches!(merge_launch(Some(r#"{"connections":[]}"#), ours()), Err(RegistryError::UnsupportedVersion(_))));
    }

    #[test]
    fn accepts_a_bom_but_never_writes_one() {
        let existing = format!("\u{feff}{}", merge_launch(None, ours()).unwrap());
        let out = merge_launch(Some(&existing), ours()).unwrap();
        assert!(!out.starts_with('\u{feff}'));
        assert!(out.starts_with('{'));
    }

    #[test]
    fn signout_removes_ours_and_points_back_to_local() {
        let launched = merge_launch(None, ours()).unwrap();
        let v = parsed(&merge_signout(Some(&launched)).unwrap().unwrap());
        assert_eq!(ids(&v), vec!["local"]);
        assert_eq!(v["primary"], "local");
        assert_eq!(v["lastUsed"], "local");
        assert!(!serde_json::to_string(&v).unwrap().contains("cb_dt_test"));
        assert_eq!(merge_signout(None).unwrap(), None);
    }

    #[test]
    fn signout_leaves_a_non_cb_primary_alone() {
        let existing = r#"{"version":2,"primary":"home","launchMode":"primary","lastUsed":"home","connections":[{"id":"local","kind":"local","label":"L"},{"id":"home","kind":"remote","label":"H","url":"https://h.example"}]}"#;
        let v = parsed(&merge_signout(Some(existing)).unwrap().unwrap());
        assert_eq!(v["primary"], "home");
        assert_eq!(ids(&v), vec!["local", "home"]);
    }

    #[test]
    fn atomic_write_round_trips_without_bom() {
        let dir = std::env::temp_dir().join(format!("cb-registry-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let text = merge_launch(None, ours()).unwrap();
        write_atomic(&dir, &text).unwrap();
        let bytes = fs::read(dir.join(FILE_NAME)).unwrap();
        assert_ne!(&bytes[..3], b"\xEF\xBB\xBF");
        assert_eq!(read(&dir).unwrap().as_deref(), Some(text.as_str()));
        assert!(!dir.join(format!("{FILE_NAME}.cb-tmp")).exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    /// Emits the registries our merges produce so the contract test can feed them to Hermes's
    /// own normalizeRegistry (launcher/contract/registry-contract.ts). Only when asked.
    #[test]
    fn emit_contract_fixtures() {
        let Ok(out) = std::env::var("CB_CONTRACT_OUT") else { return };
        let out = Path::new(&out);
        fs::create_dir_all(out).unwrap();
        let fresh = merge_launch(None, ours()).unwrap();
        let mixed = merge_launch(Some(r#"{"version":2,"primary":"local","launchMode":"last-used","lastUsed":"home",
          "connections":[{"id":"local","kind":"local","label":"My PC"},
                         {"id":"home","kind":"remote","label":"Home server","url":"https://h.example","authMode":"token","token":{"encoding":"plain","value":"x"}},
                         {"id":"cb-brindlewood-ada","kind":"remote","label":"Company Brain - ada@brindlewood","url":"http://127.0.0.1:9433","authMode":"token","token":{"encoding":"plain","value":"cb_dt_ada"}}],
          "quarantined":[{"reason":"entry-malformed","entry":"junk"}]}"#), ours()).unwrap();
        let signed_out = merge_signout(Some(&mixed)).unwrap().unwrap();
        for (name, text) in [("launch-fresh", fresh), ("launch-mixed", mixed), ("signout", signed_out)] {
            fs::write(out.join(format!("{name}.json")), text).unwrap();
        }
    }
}
