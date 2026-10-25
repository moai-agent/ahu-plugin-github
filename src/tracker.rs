use crate::{Adapter, Error, Result, validation::*};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const OP: &str = "<!-- ahu-operation:";
const LINK: &str = "<!-- ahu-task-link:v1:";
const PROGRESS: &str = "<!-- ahu-task-progress:v1:";
const EVIDENCE: &str = "<!-- ahu-task-evidence:v1:";
fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
fn task_hash(value: &Value) -> Result<String> {
    let task = string(value)?;
    if task.len() != 36
        || !task.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_hexdigit()
            }
        })
    {
        return Err(Error::Invalid);
    }
    Ok(digest(&task.to_ascii_lowercase()))
}
fn reserved(body: &str) -> bool {
    [OP, LINK, PROGRESS, EVIDENCE]
        .iter()
        .any(|prefix| body.contains(prefix))
}
// Preserve the line boundaries accepted by previously posted marker comments.
// Empty segments (including between CR and LF) cannot match a marker.
fn marker_lines(body: &str) -> impl Iterator<Item = &str> {
    body.split([
        '\n', '\r', '\u{000b}', '\u{000c}', '\u{001c}', '\u{001d}', '\u{001e}', '\u{0085}',
        '\u{2028}', '\u{2029}',
    ])
}

fn body(row: &Value) -> &str {
    row["body"].as_str().unwrap_or("")
}
fn count(rows: &[Value], tag: &str) -> usize {
    rows.iter().filter(|r| body(r).contains(tag)).count()
}
fn linked(rows: &[Value], tag: &str) -> Result<()> {
    match count(rows, tag) {
        0 => Err(Error::Denied),
        1 => Ok(()),
        _ => Err(Error::Unknown),
    }
}
fn revision(value: &Value) -> Result<u64> {
    value.as_u64().ok_or(Error::Invalid)
}
fn replay(snapshot: &Value, changes: &Value) -> Result<()> {
    revision(&snapshot["revision"])?;
    lifecycle(&snapshot["lifecycle"])?;
    if !snapshot["cancellation_requested"].is_boolean() {
        return Err(Error::Invalid);
    }
    let changes = changes.as_array().ok_or(Error::Invalid)?;
    if changes.len() > 128 {
        return Err(Error::Invalid);
    }
    let mut prior: Option<u64> = None;
    for change in changes {
        keys(
            change,
            &["revision", "kind", "lifecycle", "cancellation_requested"],
        )?;
        let current = revision(&change["revision"])?;
        if current == 0
            || prior.is_some_and(|n| n.checked_add(1) != Some(current))
            || ![
                "admitted",
                "submitted",
                "cancellation_requested",
                "input_accepted",
                "effect_claimed",
                "effect_observed",
                "reconciled",
            ]
            .contains(&string(&change["kind"])?)
            || !change["cancellation_requested"].is_boolean()
        {
            return Err(Error::Invalid);
        }
        lifecycle(&change["lifecycle"])?;
        prior = Some(current);
    }
    if let Some(last) = changes.last() {
        for key in ["revision", "lifecycle", "cancellation_requested"] {
            if last[key] != snapshot[key] {
                return Err(Error::Invalid);
            }
        }
    }
    Ok(())
}
fn envelope(mut data: Value) -> Result<(Value, bool, bool)> {
    if data["kind"] != "ahu.task-observations" {
        return Ok((data, false, false));
    }
    keys(
        &data,
        &["kind", "version", "capabilities", "observations", "data"],
    )?;
    if data["version"].as_u64() != Some(1) {
        return Err(Error::Invalid);
    }
    let observations = data["observations"].as_array().ok_or(Error::Invalid)?;
    if observations.len() != 1 {
        return Err(Error::Invalid);
    }
    let observation = &observations[0];
    let is_replay = observation["operation"] == "replay";
    if is_replay {
        keys(observation, &["operation", "snapshot", "changes"])?;
    } else {
        keys(observation, &["operation", "snapshot"])?;
        if observation["operation"] != "read" {
            return Err(Error::Invalid);
        }
    }
    let snapshot = &observation["snapshot"];
    keys(
        snapshot,
        &[
            "version",
            "task",
            "revision",
            "lifecycle",
            "cancellation_requested",
        ],
    )?;
    if snapshot["version"].as_u64() != Some(1) {
        return Err(Error::Invalid);
    }
    let mut payload = data["data"].as_object().ok_or(Error::Invalid)?.clone();
    let action = payload
        .get("action")
        .and_then(Value::as_str)
        .ok_or(Error::Invalid)?
        .to_owned();
    if is_replay != (action == "sync_replay") {
        return Err(Error::Invalid);
    }
    payload.insert("task".into(), snapshot["task"].clone());
    if ["sync_task", "task_evidence"].contains(&action.as_str()) {
        payload.insert("revision".into(), snapshot["revision"].clone());
        if action == "sync_task" {
            payload.insert("lifecycle".into(), snapshot["lifecycle"].clone());
        }
    }
    if is_replay {
        payload.insert("snapshot".into(), snapshot.clone());
        payload.insert("changes".into(), observation["changes"].clone());
    }
    data = Value::Object(payload);
    Ok((data, true, is_replay))
}
struct Target<'a> {
    repo: &'a str,
    number: u64,
    expected: &'a str,
    visibility: &'a str,
}
impl Adapter {
    fn visibility(&mut self, target: &Target<'_>) -> Result<()> {
        if !["private", "public"].contains(&target.visibility) {
            return Err(Error::Invalid);
        }
        let value = self.gh(&[
            "repo",
            "view",
            target.repo,
            "--json",
            "nameWithOwner,visibility",
        ])?;
        let name = value["nameWithOwner"].as_str().ok_or(Error::Failed)?;
        if !name.eq_ignore_ascii_case(target.repo) {
            return Err(Error::Failed);
        }
        if !value["visibility"]
            .as_str()
            .is_some_and(|v| v.eq_ignore_ascii_case(target.visibility))
        {
            return Err(Error::Denied);
        }
        Ok(())
    }
    fn actor(&mut self) -> Result<String> {
        let value = self.gh(&["api", "user"])?;
        let actor = value["login"].as_str().ok_or(Error::Failed)?;
        // GitHub logins are ASCII. Reject malformed provider identities.
        if actor.is_empty() || !actor.is_ascii() {
            return Err(Error::Failed);
        }
        Ok(actor.to_ascii_lowercase())
    }
    fn comments(
        &mut self,
        target: &Target<'_>,
        filters: &[&str],
        actor: &str,
    ) -> Result<Vec<Value>> {
        let conditions = filters
            .iter()
            .map(|tag| format!("($body | contains({}))", json!(tag)))
            .collect::<Vec<_>>()
            .join(" or ");
        let query = format!(
            "[.[] | select((.body | strings) as $body | ({conditions})) | {{body: .body, author: .user.login}}]"
        );
        let value = self.gh(&[
            "api",
            "--paginate",
            "--jq",
            &query,
            &format!("repos/{}/issues/{}/comments", target.repo, target.number),
        ])?;
        let rows = value.as_array().ok_or(Error::Failed)?;
        if rows.iter().any(|r| !r.is_object()) {
            return Err(Error::Failed);
        }
        Ok(rows
            .iter()
            .filter(|row| {
                row["author"]
                    .as_str()
                    .is_some_and(|a| a.eq_ignore_ascii_case(actor))
            })
            .cloned()
            .collect())
    }
    fn post(&mut self, target: &Target<'_>, text: &str, tag: &str, actor: &str) -> Result<()> {
        self.write(&[
            "issue",
            "comment",
            &target.number.to_string(),
            "--repo",
            target.repo,
            "--body",
            text,
        ])?;
        let rows = self
            .comments(target, &[tag], actor)
            .map_err(|_| Error::Unknown)?;
        if count(&rows, tag) != 1 {
            return Err(Error::Unknown);
        }
        Ok(())
    }
    fn sync(
        &mut self,
        target: &Target<'_>,
        hash: &str,
        revision: u64,
        state: &str,
    ) -> Result<Value> {
        self.visibility(target)?;
        let link = format!("{LINK}{hash} -->");
        let prefix = format!("{PROGRESS}{hash}:");
        let tag = format!("{prefix}{revision} -->");
        let actor = self.actor()?;
        let rows = self.comments(target, &[&link, &prefix], &actor)?;
        linked(&rows, &link)?;
        let prior = count(&rows, &tag);
        if prior > 1 {
            return Err(Error::Unknown);
        }
        if prior == 1 {
            return Ok(
                json!({"effect":"already_applied","issue":target.number,"revision":revision}),
            );
        }
        for row in &rows {
            for line in marker_lines(body(row)) {
                if let Some(raw) = line
                    .strip_prefix(&prefix)
                    .and_then(|s| s.strip_suffix(" -->"))
                    && !raw.is_empty()
                    && raw.len() <= 20
                    && raw.bytes().all(|b| b.is_ascii_digit())
                    && raw.parse::<u64>().is_ok_and(|prior| prior > revision)
                {
                    return Err(Error::Stale);
                }
            }
        }
        fresh(&self.issue(target.repo, target.number)?, target.expected)?;
        self.post(
            target,
            &format!("Ahu task status: {state} (revision {revision}).\n\n{tag}"),
            &tag,
            &actor,
        )?;
        Ok(json!({"effect":"confirmed","issue":target.number,"revision":revision}))
    }
    pub(crate) fn tracker(&mut self, input: Value) -> Result<Value> {
        let (data, observed, observed_replay) = envelope(input)?;
        let action = string(&data["action"])?;
        if action == "issue" {
            return self.readonly(&data);
        }
        let repo = repository(&data["repository"])?;
        if ["issues", "search"].contains(&action) {
            if action == "issues" {
                keys(&data, &["action", "repository", "state"])?;
            } else {
                keys(&data, &["action", "repository", "state", "query"])?;
            }
            let state = string(&data["state"])?;
            if !["open", "closed", "all"].contains(&state) {
                return Err(Error::Invalid);
            }
            let mut args = vec!["issue", "list", "--repo", repo, "--state", state];
            if action == "search" {
                let query = string(&data["query"])?;
                if query.trim().is_empty()
                    || query.chars().count() > 256
                    || query.chars().any(|c| c < ' ')
                {
                    return Err(Error::Invalid);
                }
                args.extend(["--search", query]);
            }
            args.extend([
                "--limit",
                "50",
                "--json",
                "number,title,state,url,updatedAt",
            ]);
            let rows = self.gh(&args)?;
            if rows.as_array().is_none_or(|r| r.len() > 50) {
                return Err(Error::Failed);
            }
            return Ok(json!({"provider":self.provider(),"action":action,"issues":rows}));
        }
        let mut fields = vec![
            "action",
            "repository",
            "number",
            "expected_updated_at",
            "expected_visibility",
        ];
        match action {
            "comment" => fields.extend(["operation_id", "body"]),
            "close" => fields.push("operation_id"),
            "link_task" if observed => fields.push("task"),
            "sync_task" if observed => fields.extend(["task", "revision", "lifecycle"]),
            "sync_replay" if observed_replay => fields.extend(["task", "snapshot", "changes"]),
            "task_evidence" if observed => {
                fields.extend(["task", "revision", "kind", "body", "operation_id"])
            }
            _ => return Err(Error::Invalid),
        }
        keys(&data, &fields)?;
        let target = Target {
            repo,
            number: number(&data["number"], true)?,
            expected: string(&data["expected_updated_at"])?,
            visibility: string(&data["expected_visibility"])?,
        };
        let mut result = match action {
            "comment" | "close" => self.ordinary(&target, action, &data)?,
            "link_task" => {
                let tag = format!("{LINK}{} -->", task_hash(&data["task"])?);
                self.visibility(&target)?;
                let current = self.issue(repo, target.number)?;
                let actor = self.actor()?;
                let rows = self.comments(&target, &[&tag], &actor)?;
                let effect = match count(&rows, &tag) {
                    1 => "already_linked",
                    0 => {
                        fresh(&current, target.expected)?;
                        self.post(&target, &format!("Ahu task linked.\n\n{tag}"), &tag, &actor)?;
                        "linked"
                    }
                    _ => return Err(Error::Unknown),
                };
                json!({"effect":effect,"issue":target.number})
            }
            "sync_task" => self.sync(
                &target,
                &task_hash(&data["task"])?,
                revision(&data["revision"])?,
                lifecycle(&data["lifecycle"])?,
            )?,
            "task_evidence" => self.evidence(&target, &data)?,
            "sync_replay" => {
                let hash = task_hash(&data["task"])?;
                let snapshot = &data["snapshot"];
                replay(snapshot, &data["changes"])?;
                let changes = data["changes"].as_array().ok_or(Error::Invalid)?;
                self.visibility(&target)?;
                let actor = self.actor()?;
                let link = format!("{LINK}{hash} -->");
                let rows =
                    self.comments(&target, &[&link, &format!("{PROGRESS}{hash}:")], &actor)?;
                linked(&rows, &link)?;
                let mut current = self.issue(repo, target.number)?;
                fresh(&current, target.expected)?;
                let mut confirmed = false;
                for change in changes {
                    let expected = current["updatedAt"].as_str().ok_or(Error::Stale)?;
                    let step = Target { expected, ..target };
                    let effect = self.sync(
                        &step,
                        &hash,
                        revision(&change["revision"])?,
                        lifecycle(&change["lifecycle"])?,
                    )?;
                    confirmed |= effect["effect"] == "confirmed";
                    current = self.issue(repo, target.number)?;
                    if current["state"] != "OPEN" || !current["updatedAt"].is_string() {
                        return Err(Error::Stale);
                    }
                }
                json!({"effect":if confirmed {"confirmed"} else {"already_applied"},"issue":target.number,"revision":snapshot["revision"],"changes":changes.len()})
            }
            _ => return Err(Error::Invalid),
        };
        result["provider"] = self.provider().into();
        result["action"] = action.into();
        Ok(result)
    }
    fn ordinary(&mut self, target: &Target<'_>, action: &str, data: &Value) -> Result<Value> {
        let operation = operation(&data["operation_id"])?;
        let tag = format!("{OP}v1:{} -->", digest(operation));
        let text = if action == "comment" {
            let body = string(&data["body"])?;
            let text = format!("{}\n\n{tag}", body.trim_end());
            if body.trim().is_empty() || reserved(body) || text.len() > 16000 {
                return Err(Error::Invalid);
            }
            Some(text)
        } else {
            None
        };
        self.visibility(target)?;
        let effect;
        if let Some(text) = text {
            let actor = self.actor()?;
            let rows = self.comments(target, &[&tag], &actor)?;
            effect = match count(&rows, &tag) {
                1 => "already_applied",
                0 => {
                    fresh(&self.issue(target.repo, target.number)?, target.expected)?;
                    self.post(target, &text, &tag, &actor)?;
                    "confirmed"
                }
                _ => return Err(Error::Unknown),
            };
        } else {
            let current = self.issue(target.repo, target.number)?;
            if current["state"] == "CLOSED" {
                effect = "already_applied";
            } else {
                fresh(&current, target.expected)?;
                self.write(&[
                    "issue",
                    "close",
                    &target.number.to_string(),
                    "--repo",
                    target.repo,
                    "--reason",
                    "completed",
                ])?;
                let after = self
                    .issue(target.repo, target.number)
                    .map_err(|_| Error::Unknown)?;
                if after["state"] != "CLOSED" {
                    return Err(Error::Unknown);
                }
                effect = "confirmed";
            }
        }
        let mut result = json!({"operation_id":operation,"effect":effect,"issue":target.number});
        if action == "close" {
            result["state"] = "CLOSED".into();
        }
        Ok(result)
    }
    fn evidence(&mut self, target: &Target<'_>, data: &Value) -> Result<Value> {
        let hash = task_hash(&data["task"])?;
        let revision = revision(&data["revision"])?;
        let kind = string(&data["kind"])?;
        let body_text = string(&data["body"])?;
        if !["progress", "review", "result"].contains(&kind)
            || body_text.trim().is_empty()
            || body_text.len() > 8000
            || reserved(body_text)
            || body_text.chars().any(|c| c < ' ' && !"\n\r\t".contains(c))
        {
            return Err(Error::Invalid);
        }
        let operation = operation(&data["operation_id"])?;
        let prefix = format!("{EVIDENCE}{hash}:{kind}:{}:", digest(operation));
        let tag = format!("{prefix}{} -->", digest(body_text));
        let link = format!("{LINK}{hash} -->");
        self.visibility(target)?;
        let actor = self.actor()?;
        let rows = self.comments(target, &[&link, &prefix], &actor)?;
        linked(&rows, &link)?;
        let matches: Vec<_> = rows
            .iter()
            .filter(|r| {
                marker_lines(body(r)).any(|l| l.starts_with(&prefix) && l.ends_with(" -->"))
            })
            .collect();
        let exact = matches.iter().filter(|r| body(r).contains(&tag)).count();
        let effect = if exact == 1 && matches.len() == 1 {
            "already_applied"
        } else if !matches.is_empty() {
            "conflict"
        } else {
            fresh(&self.issue(target.repo, target.number)?, target.expected)?;
            let text = format!(
                "Ahu task {kind} evidence (caller-supplied; not verified by Ahu, revision {revision}):\n\n{}\n\n{tag}",
                body_text.trim_end()
            );
            self.post(target, &text, &tag, &actor)?;
            "confirmed"
        };
        Ok(json!({"effect":effect,"issue":target.number,"revision":revision}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn uuid_hash_is_canonical_and_reserved_prefixes_are_rejected() {
        let upper = json!("ABCDEF00-0000-4000-8000-000000000001");
        assert_eq!(
            task_hash(&upper),
            task_hash(&json!(upper.as_str().unwrap().to_lowercase()))
        );
        for invalid in [
            "not-a-task",
            "00000000_0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-00000000000g",
        ] {
            assert!(task_hash(&json!(invalid)).is_err());
        }
        for prefix in [OP, LINK, PROGRESS, EVIDENCE] {
            assert!(reserved(&format!("note {prefix}")));
        }
    }
    #[test]
    fn replay_rejects_overflow_gaps_booleans_and_snapshot_disagreement() {
        let snapshot =
            json!({"revision":u64::MAX,"lifecycle":"running","cancellation_requested":false});
        let change = json!({"revision":u64::MAX,"kind":"submitted","lifecycle":"running","cancellation_requested":false});
        assert!(replay(&snapshot, &json!([change])).is_ok());
        assert!(replay(&snapshot, &json!([change, change])).is_err());
        for revision in [json!(true), json!(-1), json!(1.0)] {
            let mut invalid = snapshot.clone();
            invalid["revision"] = revision;
            assert!(replay(&invalid, &json!([])).is_err());
        }
        let mut mismatch = change.clone();
        mismatch["cancellation_requested"] = true.into();
        assert!(replay(&snapshot, &json!([mismatch])).is_err());
        assert!(replay(&snapshot, &json!(vec![change; 129])).is_err());
    }
}
