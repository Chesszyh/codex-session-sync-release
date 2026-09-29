package parser

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestM0SharedFixtures(t *testing.T) {
	data, err := os.ReadFile(os.Getenv("M0_CASES"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name string `json:"name"`
		Home string `json:"home"`
		Path string `json:"path"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	results := []map[string]any{}
	for _, c := range cases {
		p := newCodexTestProvider(t, filepath.Join(c.Home, "sessions"), filepath.Join(c.Home, "archived_sessions"))
		discovered := append(p.sources.discoverSessionPaths(filepath.Join(c.Home, "sessions")), p.sources.discoverSessionPaths(filepath.Join(c.Home, "archived_sessions"))...)
		session, messages, err := p.parseSession(c.Path, "m0", true)
		text := ""
		for _, m := range messages {
			text += m.Content + "\n"
			for _, v := range m.ToolResults {
				b, _ := json.Marshal(v)
				text += string(b)
			}
			for _, v := range m.ToolCalls {
				b, _ := json.Marshal(v)
				text += string(b)
			}
		}
		row := map[string]any{"name": c.Name, "messages": len(messages), "text": text, "discovered": len(discovered)}
		if session != nil {
			row["malformed"] = session.MalformedLines
			row["truncated"] = session.IsTruncated
			row["title"] = session.SessionName
		}
		if err != nil {
			row["error"] = err.Error()
		}
		results = append(results, row)
	}
	data, err = json.MarshalIndent(results, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	out := os.Getenv("M0_OUTPUT")
	if err := os.MkdirAll(filepath.Dir(out), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(out, data, 0600); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		if c.Name != "legacy" {
			continue
		}
		original, err := os.ReadFile(c.Path)
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(t.TempDir(), filepath.Base(c.Path))
		appendLine := []byte("{\"timestamp\":\"2026-09-22T00:00:01.000Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"content\":[{\"type\":\"output_text\",\"text\":\"M0_FOLLOW\"}]}}\n")
		if err := os.WriteFile(path, append(original, appendLine...), 0600); err != nil {
			t.Fatal(err)
		}
		p := newCodexTestProvider(t, filepath.Dir(path))
		msgs, _, offset, err := p.parseSessionFrom(t.Context(), path, int64(len(original)), 3, true)
		if err != nil {
			t.Fatal(err)
		}
		offset += int64(len(original))
		repeat, _, _, err := p.parseSessionFrom(t.Context(), path, offset, 3+len(msgs), true)
		if err != nil {
			t.Fatal(err)
		}
		valid := len(msgs) == 1 && strings.Contains(msgs[0].Content, "M0_FOLLOW")
		follow, _ := json.Marshal(map[string]any{"append_found_once": valid, "repeat_count": len(repeat), "next_offset": offset})
		if err := os.WriteFile(strings.Replace(out, "-results.json", "-follow.json", 1), follow, 0600); err != nil {
			t.Fatal(err)
		}
	}
}
