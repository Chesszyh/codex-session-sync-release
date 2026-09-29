use codex_thread_store::{LocalThreadStore, LocalThreadStoreConfig};
use codex_utils_absolute_path::AbsolutePathBuf;
use std::path::PathBuf;
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        return Err("expected isolated home, sqlite root, rollout path".into());
    }
    let home = PathBuf::from(&args[1]).canonicalize()?;
    let sqlite_home = PathBuf::from(&args[2]).canonicalize()?;
    let path = PathBuf::from(&args[3]).canonicalize()?;
    if !path.starts_with(&home) {
        return Err("rollout is outside isolated home".into());
    }
    let sqlite = codex_state::SqliteConfig::from_sqlite_home(AbsolutePathBuf::from_absolute_path(
        &sqlite_home,
    )?);
    let state = codex_state::StateRuntime::init(sqlite.clone(), "openai".to_string()).await?;
    codex_rollout::state_db::reconcile_rollout(
        Some(&state),
        &path,
        "openai",
        None,
        &[],
        None,
        None,
    )
    .await;
    let meta = codex_rollout::read_session_meta_line(&path).await?;
    let store = LocalThreadStore::new(
        LocalThreadStoreConfig {
            codex_home: home,
            sqlite,
            default_model_provider_id: "openai".to_string(),
        },
        Some(state.clone()),
    );
    store.replica_materialize(meta.meta.id, &path).await?;
    state.close().await;
    println!("{{\"materialized\":true}}");
    Ok(())
}
