package executor

import (
	"context"
	"net/http"
	"testing"

	internalcache "github.com/router-for-me/CLIProxyAPI/v7/internal/cache"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestWorkflowProbeUnanchoredMarkedReplayIgnoresFingerprintAndMixesClientReasoning(t *testing.T) {
	clientReasoning := validCodexReasoningEncryptedContentForTestSeed(41)
	cachedReasoning := validCodexReasoningEncryptedContentForTestSeed(42)
	body := []byte(`{"model":"gpt-5.4","input":[{"type":"reasoning","summary":[],"encrypted_content":"` + clientReasoning + `"},{"type":"message","role":"user","content":[{"type":"input_text","text":"different request"}]}]}`)
	replay := [][]byte{
		[]byte(`{"type":"` + internalcache.CodexReasoningReplayTurnType + `","request_fingerprint":"definitely-not-the-current-prefix"}`),
		[]byte(`{"type":"reasoning","summary":[],"content":null,"encrypted_content":"` + cachedReasoning + `"}`),
	}

	updated, ok := insertCodexReasoningReplayTurns(body, replay)
	if !ok {
		t.Fatal("marked unanchored replay was rejected; expected current implementation to accept it")
	}

	var reasoning []string
	for _, item := range gjson.GetBytes(updated, "input").Array() {
		if item.Get("type").String() == "reasoning" {
			reasoning = append(reasoning, item.Get("encrypted_content").String())
		}
	}
	if len(reasoning) != 2 {
		t.Fatalf("reasoning count = %d, want 2; body=%s", len(reasoning), updated)
	}
	seen := map[string]bool{reasoning[0]: true, reasoning[1]: true}
	if !seen[clientReasoning] || !seen[cachedReasoning] {
		t.Fatalf("expected both client and cached reasoning, got %#v", reasoning)
	}
}

func TestWorkflowProbeReplayFingerprintIgnoresTopLevelSemantics(t *testing.T) {
	bodyA := gjson.Parse(`{"instructions":"alpha","tools":[{"type":"function","name":"one"}],"reasoning":{"effort":"low"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"same"}]}]}`)
	bodyB := gjson.Parse(`{"instructions":"beta","tools":[{"type":"function","name":"two"}],"reasoning":{"effort":"xhigh"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"same"}]}]}`)

	itemsA := bodyA.Get("input").Array()
	itemsB := bodyB.Get("input").Array()
	gotA := codexReplayInputPrefixFingerprint(itemsA, len(itemsA))
	gotB := codexReplayInputPrefixFingerprint(itemsB, len(itemsB))
	if gotA != gotB {
		t.Fatalf("fingerprints differ unexpectedly: %s vs %s", gotA, gotB)
	}
}

func TestWorkflowProbeAssistantFingerprintSegmentationCollision(t *testing.T) {
	a := gjson.Parse(`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ab"},{"type":"output_text","text":"c"}]}`)
	b := gjson.Parse(`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"a"},{"type":"output_text","text":"bc"}]}`)
	gotA := codexReplayAssistantMessageFingerprint(a)
	gotB := codexReplayAssistantMessageFingerprint(b)
	if gotA == "" || gotA != gotB {
		t.Fatalf("expected non-empty collision, got %q vs %q", gotA, gotB)
	}
}

func TestWorkflowProbeToolIDSanitizationCollision(t *testing.T) {
	a := codexReplayComparableCallIDs("call:a")
	b := codexReplayComparableCallIDs("call/a")
	seen := make(map[string]bool, len(a))
	for _, value := range a {
		seen[value] = true
	}
	for _, value := range b {
		if seen[value] {
			return
		}
	}
	t.Fatalf("expected comparable-ID collision, got %#v vs %#v", a, b)
}

func TestWorkflowProbeClaudeHeaderOverridesBodySession(t *testing.T) {
	from := sdktranslator.FromString("claude")
	headers := http.Header{}
	headers.Set("X-Claude-Code-Session-Id", "old-session")
	req := cliproxyexecutor.Request{
		Model: "gpt-5.4",
		Payload: []byte(`{"model":"gpt-5.4","metadata":{"user_id":"{\"device_id\":\"d\",\"account_uuid\":\"\",\"session_id\":\"new-session\"}"},"messages":[{"role":"user","content":"next"}]}`),
	}
	body := []byte(`{"model":"gpt-5.4","input":[{"type":"message","role":"user","content":"next"}]}`)
	got := codexReasoningReplaySessionKey(context.Background(), from, req, cliproxyexecutor.Options{SourceFormat: from, Headers: headers}, body)
	if got != "claude:old-session:agent:main" {
		t.Fatalf("session key = %q, want header-selected old session", got)
	}
}

func TestWorkflowProbeCountTokensStringInputReturnsZero(t *testing.T) {
	enc, err := tokenizerForCodexModel("gpt-5.4")
	if err != nil {
		t.Fatalf("tokenizer: %v", err)
	}
	count, err := countCodexInputTokens(enc, []byte(`{"input":"hello"}`))
	if err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 0 {
		t.Fatalf("count = %d, want current buggy value 0", count)
	}
}
