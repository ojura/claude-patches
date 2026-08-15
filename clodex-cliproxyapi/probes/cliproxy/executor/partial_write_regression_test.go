package executor

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
)

func TestWorkflowPartialWebsocketWriteIsRetriedAsFullRequest(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	var connections atomic.Int32
	partialBytes := make(chan int, 1)
	fullBytes := make(chan int, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, errUpgrade := upgrader.Upgrade(w, r, nil)
		if errUpgrade != nil {
			t.Errorf("upgrade websocket: %v", errUpgrade)
			return
		}
		connection := connections.Add(1)
		if connection == 1 {
			raw := conn.UnderlyingConn()
			if tcp, ok := raw.(*net.TCPConn); ok {
				_ = tcp.SetReadBuffer(4096)
				_ = tcp.SetLinger(0)
			}
			buf := make([]byte, 64*1024)
			n, _ := io.ReadFull(raw, buf)
			partialBytes <- n
			_ = raw.Close()
			return
		}
		defer func() { _ = conn.Close() }()
		_, payload, errRead := conn.ReadMessage()
		if errRead != nil {
			t.Errorf("read retried websocket request: %v", errRead)
			return
		}
		fullBytes <- len(payload)
		completed := []byte(`{"type":"response.completed","response":{"id":"resp-after-retry","output":[],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}`)
		if errWrite := conn.WriteMessage(websocket.TextMessage, completed); errWrite != nil {
			t.Errorf("write completed websocket message: %v", errWrite)
		}
	}))
	defer server.Close()

	exec := NewCodexWebsocketsExecutor(&config.Config{SDKConfig: config.SDKConfig{DisableImageGeneration: config.DisableImageGenerationAll}})
	exec.store = &codexWebsocketSessionStore{sessions: make(map[string]*codexWebsocketSession)}
	const sessionID = "partial-write-session"
	defer exec.CloseExecutionSession(sessionID)
	auth := &cliproxyauth.Auth{
		ID:       "auth-a",
		Provider: "codex",
		Attributes: map[string]string{
			"api_key":  "sk-test",
			"base_url": server.URL,
		},
	}
	largeInput := strings.Repeat("X", 32*1024*1024)
	req := cliproxyexecutor.Request{
		Model:   "gpt-5.6-sol",
		Payload: []byte(fmt.Sprintf(`{"model":"gpt-5.6-sol","input":"%s"}`, largeInput)),
	}
	opts := cliproxyexecutor.Options{
		SourceFormat:   sdktranslator.FromString("codex"),
		ResponseFormat: sdktranslator.FromString("codex"),
		Metadata: map[string]any{
			cliproxyexecutor.ExecutionSessionMetadataKey: sessionID,
		},
	}

	if _, errExecute := exec.Execute(context.Background(), auth, req, opts); errExecute != nil {
		t.Fatalf("Execute() after partial write = %v", errExecute)
	}
	select {
	case got := <-partialBytes:
		if got != 64*1024 {
			t.Fatalf("first connection bytes read = %d, want 65536", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for partial first transmission")
	}
	select {
	case got := <-fullBytes:
		if got < len(largeInput) {
			t.Fatalf("retried request bytes = %d, want at least %d", got, len(largeInput))
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for full retransmission")
	}
	if got := connections.Load(); got != 2 {
		t.Fatalf("upstream websocket connections = %d, want 2", got)
	}
}
