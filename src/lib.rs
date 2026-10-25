//! Standalone GitHub one-shot adapters; provider code has no ahu-core dependency.
mod json;
mod process;
mod tracker;
mod validation;
use process::Process;
use serde_json::{Value, json};
use std::io::Write;
use validation::*;
const LIMIT: usize = 64 * 1024;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Error {
    Invalid,
    Failed,
    Denied,
    Stale,
    Unavailable,
    Unknown,
}
type Result<T> = std::result::Result<T, Error>;

struct Adapter {
    process: Process,
    tracker: bool,
    mutated: bool,
}
impl Adapter {
    fn provider(&self) -> &'static str {
        if self.tracker {
            "github-tracker"
        } else {
            "github-cli"
        }
    }
    fn gh(&mut self, args: &[&str]) -> Result<Value> {
        let args: Vec<_> = args.iter().map(|s| (*s).to_owned()).collect();
        let bytes = self.process.run(&args, false)?;
        if args.iter().any(|s| s == "--paginate") {
            json::pages(&bytes)
        } else {
            json::parse(&bytes)
        }
    }
    fn write(&mut self, args: &[&str]) -> Result<()> {
        let result = self.process.run(
            &args.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>(),
            true,
        );
        if result.is_ok() || result == Err(Error::Unknown) {
            self.mutated = true;
        }
        result.map(|_| ())
    }
    fn issue(&mut self, repo: &str, number: u64) -> Result<Value> {
        let value = self.gh(&[
            "issue",
            "view",
            &number.to_string(),
            "--repo",
            repo,
            "--json",
            "number,title,state,url,updatedAt",
        ])?;
        if !value.is_object() || value["number"].as_u64() != Some(number) {
            return Err(Error::Failed);
        }
        Ok(value)
    }
    fn readonly(&mut self, data: &Value) -> Result<Value> {
        let action = string(&data["action"])?;
        let repo = repository(&data["repository"])?;
        match action {
            "repository" => {
                keys(data, &["action", "repository"])?;
                let value = self.gh(&[
                    "repo",
                    "view",
                    repo,
                    "--json",
                    "nameWithOwner,url,defaultBranchRef",
                ])?;
                if !value.is_object() {
                    return Err(Error::Failed);
                }
                Ok(
                    json!({"provider": self.provider(), "action": action, "repository": value["nameWithOwner"].as_str().unwrap_or(repo), "url": value["url"].as_str(), "default_branch": value["defaultBranchRef"]["name"].as_str()}),
                )
            }
            "issue" => {
                keys(data, &["action", "repository", "number"])?;
                let number = number(&data["number"], self.tracker)?;
                let value = self.issue(repo, number)?;
                Ok(
                    json!({"provider": self.provider(), "action": action, "issue": {"number": number, "title": value["title"].as_str().unwrap_or(""), "state": value["state"].as_str().unwrap_or(""), "url": value["url"].as_str(), "updated_at": value["updatedAt"].as_str()}}),
                )
            }
            "releases" => {
                keys(data, &["action", "repository"])?;
                let value = self.gh(&[
                    "release",
                    "list",
                    "--repo",
                    repo,
                    "--limit",
                    "20",
                    "--json",
                    "tagName,name,isLatest,publishedAt",
                ])?;
                let rows = value.as_array().ok_or(Error::Failed)?;
                if rows.len() > 20 {
                    return Err(Error::Failed);
                }
                let rows = rows.iter().map(|row| Ok(json!({"tag": row["tagName"].as_str().ok_or(Error::Failed)?, "name": row["name"].as_str().unwrap_or(""), "latest": row["isLatest"] == true, "published_at": row["publishedAt"].as_str()}))).collect::<Result<Vec<_>>>()?;
                Ok(json!({"provider": self.provider(), "action": action, "releases": rows}))
            }
            _ => Err(Error::Invalid),
        }
    }
}
fn request(raw: &[u8], tracker: bool) -> Result<Value> {
    let value = json::parse(raw).map_err(|_| Error::Invalid)?;
    keys(
        &value,
        &["protocol_version", "plugin_id", "grant", "context", "input"],
    )?;
    if value["protocol_version"].as_u64() != Some(1)
        || value["plugin_id"]
            != if tracker {
                "github-tracker"
            } else {
                "github-cli"
            }
        || value["grant"] != json!({"operation":"invoke"})
    {
        return Err(Error::Invalid);
    }
    keys(&value["context"], &["invocation_id"])?;
    let invocation = string(&value["context"]["invocation_id"])?;
    if !tracker && (!invocation.is_ascii() || invocation.is_empty() || invocation.len() > 64) {
        return Err(Error::Invalid);
    }
    Ok(value["input"].clone())
}
fn input() -> Result<Vec<u8>> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    let mut raw = Vec::new();
    // Read stdin directly without a buffered reader: poll must observe all bytes.
    loop {
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return Err(Error::Invalid);
        }
        let mut poll = libc::pollfd {
            fd: 0,
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: poll points to one initialized descriptor for standard input.
        let status = unsafe { libc::poll(&mut poll, 1, remaining.as_millis().max(1) as i32) };
        if status < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        if status <= 0 {
            return Err(Error::Invalid);
        }
        let mut chunk = [0u8; 8192];
        // SAFETY: chunk is writable for its full length; fd 0 is borrowed.
        let count = unsafe { libc::read(0, chunk.as_mut_ptr().cast(), chunk.len()) };
        if count < 0 {
            return Err(Error::Invalid);
        }
        if count == 0 {
            return Ok(raw);
        }
        raw.extend_from_slice(&chunk[..count as usize]);
        if raw.len() > LIMIT {
            return Err(Error::Invalid);
        }
    }
}
fn serve(tracker: bool, fixture: Option<Process>) -> i32 {
    if std::env::args().skip(1).collect::<Vec<_>>() != ["--ahu-plugin-protocol=1"] {
        if tracker {
            return 2;
        }
        let _ = std::io::stdout()
            .write_all(b"{\"protocol_version\":1,\"status\":\"error\",\"code\":\"invalid_input\"}");
        return 0;
    }
    let mut mutated = false;
    let result = (|| {
        let data = request(&input()?, tracker)?;
        let process = match fixture {
            Some(process) => process,
            None => Process::native(tracker)?,
        };
        let mut adapter = Adapter {
            process,
            tracker,
            mutated: false,
        };
        let result = if tracker {
            adapter.tracker(data)
        } else {
            adapter.readonly(&data)
        };
        mutated = adapter.mutated;
        result
    })();
    let response = match result {
        Ok(result) => json!({"protocol_version":1,"status":"ok","result":result}),
        Err(error) if tracker && (error == Error::Unknown || mutated) => {
            json!({"protocol_version":1,"status":"ok","result":{"provider":"github-tracker","effect":"outcome_unknown","requires_reconciliation":true}})
        }
        Err(error) => {
            json!({"protocol_version":1,"status":"error","code": if !tracker {"failed"} else { match error {Error::Invalid => "invalid_input", Error::Stale | Error::Denied | Error::Unavailable => "unsupported", _ => "failed"} }})
        }
    };
    let raw = response.to_string();
    if raw.len() > LIMIT {
        return 5;
    }
    if std::io::stdout().write_all(raw.as_bytes()).is_err() {
        return 5;
    }
    0
}
/// Run the production one-shot adapter with fixed native gh discovery.
pub fn run(tracker: bool) -> i32 {
    serve(tracker, None)
}
/// Development-only process injection; never enabled in distributed binaries.
#[cfg(feature = "test-driver")]
pub fn run_fixture() -> i32 {
    use std::time::{Duration, Instant};
    let timeout = Duration::from_millis(
        std::env::var("FIXTURE_TIMEOUT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(1000),
    );
    let process = Process {
        executable: std::env::var_os("FIXTURE_GH").map(Into::into),
        home: std::env::var_os("FIXTURE_HOME")
            .unwrap_or_else(|| "/tmp".into())
            .into(),
        deadline: Instant::now() + timeout,
        timeout,
    };
    serve(
        std::env::var("FIXTURE_TRACKER").as_deref() != Ok("0"),
        Some(process),
    )
}
