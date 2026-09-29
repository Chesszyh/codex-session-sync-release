use codex_app_server_protocol::{ThreadHistoryBuilder, project_rollout_line};
use codex_rollout::parse_rollout_line_bytes;
use serde_json::json;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Write};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    let path = args.get(1).ok_or("expected rollout path or - for stdin")?;
    let source: Box<dyn io::Read> = if path == "-" {
        Box::new(io::stdin())
    } else if path.ends_with(".zst") {
        Box::new(zstd::stream::read::Decoder::new(File::open(path)?)?)
    } else {
        Box::new(File::open(path)?)
    };
    let mut reader = BufReader::new(source);
    let mut output = io::BufWriter::new(io::stdout().lock());
    let mut bytes = Vec::new();
    let mut offset = args.get(2).map_or(Ok(0), |v| v.parse::<u64>())?;
    let mut legacy =
        (args.get(3).map(String::as_str) == Some("legacy")).then(ThreadHistoryBuilder::new);
    loop {
        bytes.clear();
        let mut len = 0usize;
        let mut oversized = false;
        let mut complete = false;
        loop {
            let available = reader.fill_buf()?;
            if available.is_empty() {
                break;
            }
            let count = available
                .iter()
                .position(|b| *b == b'\n')
                .map_or(available.len(), |i| i + 1);
            complete = available[count - 1] == b'\n';
            len += count;
            if len <= 64 * 1024 * 1024 {
                bytes.extend_from_slice(&available[..count]);
            } else {
                oversized = true;
                bytes.clear();
            }
            reader.consume(count);
            if complete {
                break;
            }
        }
        if len == 0 {
            break;
        }
        let end = offset + len as u64;
        if !complete {
            writeln!(
                output,
                "{}",
                json!({"kind":"incomplete_tail", "start":offset.to_string(), "end":end.to_string()})
            )?;
            break;
        }
        if oversized {
            writeln!(
                output,
                "{}",
                json!({"kind":"record_too_large", "start":offset.to_string(), "end":end.to_string()})
            )?;
            offset = end;
            continue;
        }
        match parse_rollout_line_bytes(&bytes) {
            Ok(line) => {
                let context = if legacy.is_some() {
                    match &line.item {
                        codex_rollout::RolloutItem::ResponseItem(item) => {
                            Some(serde_json::to_value(&item.item)?)
                        }
                        _ => None,
                    }
                } else {
                    None
                };
                let changes = match legacy.as_mut() {
                    Some(builder) => builder.handle_rollout_item_with_changes(&line.item),
                    None => project_rollout_line(&line),
                };
                let turns: Vec<_> = changes.changed_turns.into_iter().map(|t| json!({"id":t.turn_id,"status":t.status,"error":t.error,"startedAt":t.started_at,"completedAt":t.completed_at,"durationMs":t.duration_ms})).collect();
                let items: Vec<_> = changes.changed_items.into_iter().map(|i| json!({"turnId":i.turn_id,"item":i.item,"startedAtMs":i.started_at_ms,"completedAtMs":i.completed_at_ms})).collect();
                writeln!(
                    output,
                    "{}",
                    json!({"kind":"record","start":offset.to_string(),"end":end.to_string(),"ordinal":line.ordinal.map(|v|v.to_string()),"turns":turns,"items":items,"removedTurns":changes.removed_turn_ids,"context":context})
                )?;
            }
            Err(error) => writeln!(
                output,
                "{}",
                json!({"kind":"decode_error","start":offset.to_string(),"end":end.to_string(),"error":error.to_string()})
            )?,
        }
        offset = end;
    }
    output.flush()?;
    Ok(())
}
