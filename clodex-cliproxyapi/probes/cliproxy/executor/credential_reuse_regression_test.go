package executor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
)

func TestWorkflowSameAuthIDCredentialChangeReusesRetainedWebsocket(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	handshakes := make(chan string, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, errUpgrade := upgrader.Upgrade(w, r, nil)
		if errUpgrade != nil {
			t.Errorf("upgrade websocket: %v", errUpgrade)
			return
		}
		handshakes <- r.Header.Get("Authorization")
		defer func() { _ = conn.Close() }()
		for {
			if _, _, errRead := conn.ReadMessage(); errRead != nil {
				return
			}
		}
	}))
	defer server.Close()

	exec := NewCodexWebsocketsExecutor(&config.Config{})
	exec.store = &codexWebsocketSessionStore{sessions: make(map[string]*codexWebsocketSession)}
	const sessionID = "credential-refresh-session"
	sess := exec.getOrCreateSession(sessionID)
	defer exec.CloseExecutionSession(sessionID)

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")
	authOne := &cliproxyauth.Auth{ID: "stable-auth-id"}
	connOne, _, _, errFirst := exec.ensureUpstreamConn(context.Background(), authOne, sess, authOne.ID, wsURL, http.Header{"Authorization": []string{"Bearer token-one"}})
	if errFirst != nil {
		t.Fatalf("first websocket connection: %v", errFirst)
	}
	select {
	case got := <-handshakes:
		if got != "Bearer token-one" {
			t.Fatalf("first Authorization = %q, want token-one", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for first handshake")
	}

	authTwo := &cliproxyauth.Auth{ID: "stable-auth-id"}
	connTwo, _, _, errSecond := exec.ensureUpstreamConn(context.Background(), authTwo, sess, authTwo.ID, wsURL, http.Header{"Authorization": []string{"Bearer token-two"}})
	if errSecond != nil {
		t.Fatalf("second websocket connection: %v", errSecond)
	}
	if connTwo != connOne {
		t.Fatal("same auth ID and URL unexpectedly redialed the retained websocket")
	}
	select {
	case got := <-handshakes:
		t.Fatalf("credential-only update opened a second handshake with %q", got)
	case <-time.After(200 * time.Millisecond):
	}
}
