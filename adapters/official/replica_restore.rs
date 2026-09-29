use chrono::{DateTime, Utc};
use codex_protocol::{ThreadId, protocol::SessionSource};
use codex_state::{StateRuntime, ThreadMetadataBuilder, ThreadSection};
use codex_thread_store::{LocalThreadStore, LocalThreadStoreConfig};
use codex_utils_absolute_path::AbsolutePathBuf;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

type Error = Box<dyn std::error::Error>;

fn optional<T: DeserializeOwned>(row: &Value, field: &str) -> Result<Option<T>, Error> {
    match row.get(field).filter(|value| !value.is_null()) {
        Some(value) => Ok(Some(serde_json::from_value(value.clone())?)),
        None => Ok(None),
    }
}

fn enum_value<T: DeserializeOwned>(row: &Value, field: &str) -> Result<Option<T>, Error> {
    let Some(value) = row.get(field).filter(|value| !value.is_null()) else {
        return Ok(None);
    };
    let decoded = value
        .as_str()
        .and_then(|text| serde_json::from_str::<Value>(text).ok())
        .unwrap_or_else(|| value.clone());
    Ok(Some(serde_json::from_value(decoded)?))
}

fn date(row: &Value, seconds: &str, millis: &str) -> Result<DateTime<Utc>, Error> {
    let value = row
        .get(millis)
        .and_then(Value::as_i64)
        .or_else(|| row.get(seconds).and_then(Value::as_i64).map(|v| v * 1000))
        .ok_or("missing timestamp")?;
    DateTime::from_timestamp_millis(value).ok_or_else(|| "invalid timestamp".into())
}

fn rows<'a>(plan: &'a Value, table: &str) -> &'a [Value] {
    plan["tables"][table]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

async fn import_projection(
    store: &LocalThreadStore,
    id: ThreadId,
    path: &Path,
    staging: &Path,
) -> Result<(), Error> {
    let mut source = BufReader::new(File::open(path)?);
    let mut prefix = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(staging)?;
    let mut record = Vec::new();
    let mut pending = 0;
    loop {
        record.clear();
        let count = source.read_until(b'\n', &mut record)?;
        if count == 0 {
            break;
        }
        prefix.write_all(&record)?;
        pending += count;
        // The official importer buffers the unread suffix; publish bounded, complete prefixes.
        if pending >= 8 * 1024 * 1024 {
            prefix.flush()?;
            store.replica_materialize(id, staging).await?;
            pending = 0;
        }
    }
    prefix.flush()?;
    if pending > 0 {
        store.replica_materialize(id, staging).await?;
    }
    drop(prefix);
    std::fs::remove_file(staging)?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    let path = PathBuf::from(
        std::env::args()
            .nth(1)
            .ok_or("expected restore plan path")?,
    )
    .canonicalize()?;
    let plan: Value = serde_json::from_slice(&std::fs::read(&path)?)?;
    let root = path.parent().ok_or("missing output root")?;
    let home = root.join("home").canonicalize()?;
    let sqlite_home = root.join("sqlite").canonicalize()?;
    if home.parent() != Some(root)
        || sqlite_home.parent() != Some(root)
        || sqlite_home.join("state_5.sqlite").exists()
    {
        return Err("restore requires fresh sibling home and sqlite directories".into());
    }
    for rollout in plan["rollouts"].as_array().ok_or("missing rollout list")? {
        let target = PathBuf::from(rollout["path"].as_str().ok_or("missing rollout path")?)
            .canonicalize()?;
        if !target.starts_with(&home) {
            return Err("rollout outside isolated home".into());
        }
    }
    let sqlite = codex_state::SqliteConfig::from_sqlite_home(AbsolutePathBuf::from_absolute_path(
        &sqlite_home,
    )?);
    let state = StateRuntime::init(sqlite.clone(), "openai".to_string()).await?;
    let mut sections: HashMap<String, ThreadSection> = HashMap::new();
    for row in rows(&plan, "thread_sections") {
        let source_id = row["id"].as_str().ok_or("section id missing")?;
        let appearance = enum_value(row, "appearance")?;
        let section = if source_id == codex_state::PINNED_THREAD_SECTION_ID {
            ThreadSection {
                id: source_id.to_string(),
                name: row["name"].as_str().unwrap_or("Pinned").to_string(),
                appearance,
            }
        } else {
            state
                .create_thread_section(
                    row["name"].as_str().ok_or("section name missing")?,
                    appearance,
                )
                .await?
        };
        sections.insert(source_id.to_string(), section);
    }
    let mut projects = HashMap::new();
    for row in rows(&plan, "projects") {
        let source_id = row["id"].as_str().ok_or("project id missing")?;
        let roots = rows(&plan, "project_roots")
            .iter()
            .filter(|r| r["project_id"].as_str() == Some(source_id))
            .map(|r| codex_state::ProjectRoot {
                path: r["path"].as_str().unwrap_or_default().to_string(),
            })
            .collect();
        let metadata: BTreeMap<String, String> = enum_value(row, "metadata")?.unwrap_or_default();
        let created = state
            .create_project(
                row["name"]
                    .as_str()
                    .ok_or("project name missing")?
                    .to_string(),
                roots,
                metadata,
                &[],
                &format!("replica-import:{source_id}"),
            )
            .await?;
        projects.insert(source_id.to_string(), created.project.id);
    }
    for thread in plan["threads"].as_array().ok_or("thread list missing")? {
        let row = &thread["metadata"];
        let id = ThreadId::from_string(row["id"].as_str().ok_or("thread id missing")?)?;
        let rollout = PathBuf::from(thread["path"].as_str().ok_or("selected path missing")?);
        if !rollout.canonicalize()?.starts_with(&home) {
            return Err("selected head outside isolated home".into());
        }
        let created_at = date(row, "created_at", "created_at_ms")?;
        let provider = row["model_provider"].as_str().ok_or("provider missing")?;
        let mut metadata =
            ThreadMetadataBuilder::new(id, rollout, created_at, SessionSource::Cli).build(provider);
        metadata.updated_at = date(row, "updated_at", "updated_at_ms")?;
        metadata.recency_at =
            date(row, "recency_at", "recency_at_ms").unwrap_or(metadata.updated_at);
        metadata.source = row["source"].as_str().unwrap_or("cli").to_string();
        metadata.history_mode = enum_value(row, "history_mode")?.ok_or("history mode missing")?;
        metadata.originator = optional(row, "originator")?;
        metadata.thread_source = enum_value(row, "thread_source")?;
        metadata.agent_nickname = optional(row, "agent_nickname")?;
        metadata.agent_role = optional(row, "agent_role")?;
        metadata.agent_path = optional(row, "agent_path")?;
        metadata.model = optional(row, "model")?;
        metadata.reasoning_effort = enum_value(row, "reasoning_effort")?;
        metadata.cwd = PathBuf::from(row["cwd"].as_str().ok_or("cwd missing")?);
        metadata.cli_version = row["cli_version"].as_str().unwrap_or_default().to_string();
        metadata.title = row["title"].as_str().unwrap_or_default().to_string();
        metadata.name = optional(row, "name")?;
        metadata.preview = optional(row, "preview")?;
        metadata.sandbox_policy = row["sandbox_policy"]
            .as_str()
            .ok_or("sandbox policy missing")?
            .to_string();
        metadata.approval_mode = row["approval_mode"]
            .as_str()
            .ok_or("approval mode missing")?
            .to_string();
        metadata.tokens_used = row["tokens_used"].as_i64().unwrap_or(0);
        metadata.first_user_message = optional(row, "first_user_message")?;
        metadata.archived_at = if row["archived"].as_i64() == Some(1) {
            Some(date(row, "archived_at", "archived_at_ms")?)
        } else {
            None
        };
        metadata.section = row["thread_section_id"]
            .as_str()
            .map(|id| sections.get(id).cloned().ok_or("missing section"))
            .transpose()?;
        metadata.section_position = optional(row, "section_position")?;
        metadata.section_entered_at = row["section_entered_at_ms"]
            .as_i64()
            .and_then(DateTime::from_timestamp_millis);
        metadata.project_id = row["project_id"]
            .as_str()
            .map(|id| projects.get(id).cloned().ok_or("missing project"))
            .transpose()?;
        metadata.daybreak_enabled = row["daybreak_enabled"].as_i64().map(|v| v != 0);
        metadata.git_sha = optional(row, "git_sha")?;
        metadata.git_branch = optional(row, "git_branch")?;
        metadata.git_origin_url = optional(row, "git_origin_url")?;
        state.upsert_thread(&metadata).await?;
        if let Some(mode) = row["memory_mode"].as_str() {
            state.set_thread_memory_mode(id, mode).await?;
        }
    }
    for attachment in rows(&plan, "thread_attachments") {
        let id = ThreadId::from_string(
            attachment["thread_id"]
                .as_str()
                .ok_or("attachment thread missing")?,
        )?;
        let payload: Value =
            enum_value(attachment, "payload")?.ok_or("attachment payload missing")?;
        state
            .add_thread_attachment(
                id,
                attachment["attachment_type"]
                    .as_str()
                    .ok_or("attachment type missing")?,
                attachment["identity_key"]
                    .as_str()
                    .ok_or("attachment identity missing")?,
                &payload,
            )
            .await?;
    }
    let store = LocalThreadStore::new(
        LocalThreadStoreConfig {
            codex_home: home.clone(),
            sqlite,
            default_model_provider_id: "openai".to_string(),
        },
        Some(state.clone()),
    );
    for (index, rollout) in plan["rollouts"].as_array().unwrap().iter().enumerate() {
        let id =
            ThreadId::from_string(rollout["rollout_id"].as_str().ok_or("rollout id missing")?)?;
        import_projection(
            &store,
            id,
            Path::new(rollout["path"].as_str().unwrap()),
            &home.join(format!(".replica-projection-{index}")),
        )
        .await?;
    }
    // The source sidecar already supplies authoritative metadata; startup backfill would
    // replace it with older rollout-derived policy and timestamps.
    state.mark_backfill_complete(None).await?;
    state.close().await;
    let section_ids: HashMap<_, _> = sections.iter().map(|(from, to)| (from, &to.id)).collect();
    println!(
        "{}",
        json!({"imported":true,"sections":section_ids,"projects":projects})
    );
    Ok(())
}
