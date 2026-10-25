use crate::{Error, Result};
use serde_json::Value;
pub(crate) fn keys(value: &Value, expected: &[&str]) -> Result<()> {
    let object = value.as_object().ok_or(Error::Invalid)?;
    if object.len() != expected.len() || !expected.iter().all(|k| object.contains_key(*k)) {
        return Err(Error::Invalid);
    }
    Ok(())
}
pub(crate) fn string(value: &Value) -> Result<&str> {
    value.as_str().ok_or(Error::Invalid)
}
pub(crate) fn safe_char(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"._-".contains(&byte)
}
pub(crate) fn repository(value: &Value) -> Result<&str> {
    let repo = string(value)?;
    let parts: Vec<_> = repo.split('/').collect();
    if repo.len() > 200
        || parts.len() != 2
        || parts.iter().any(|p| {
            p.is_empty()
                || p.starts_with('-')
                || *p == "."
                || *p == ".."
                || !p.bytes().all(safe_char)
        })
    {
        return Err(Error::Invalid);
    }
    Ok(repo)
}
pub(crate) fn number(value: &Value, tracker: bool) -> Result<u64> {
    let n = value.as_u64().ok_or(Error::Invalid)?;
    if n == 0 || (tracker && n > i32::MAX as u64) {
        return Err(Error::Invalid);
    }
    Ok(n)
}
pub(crate) fn operation(value: &Value) -> Result<&str> {
    let value = string(value)?;
    if value.is_empty() || value.len() > 64 || !value.bytes().all(safe_char) {
        return Err(Error::Invalid);
    }
    Ok(value)
}
pub(crate) fn lifecycle(value: &Value) -> Result<&str> {
    let state = string(value)?;
    if ![
        "prepared",
        "queued",
        "starting",
        "running",
        "waiting_for_approval",
        "outcome_unknown",
        "exited",
        "failed",
        "cancelled",
    ]
    .contains(&state)
    {
        return Err(Error::Invalid);
    }
    Ok(state)
}
pub(crate) fn fresh(issue: &Value, expected: &str) -> Result<()> {
    if issue["state"] != "OPEN" || issue["updatedAt"].as_str() != Some(expected) {
        return Err(Error::Stale);
    }
    Ok(())
}
