package watcher

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
)

func TestWorkflowAuthFileCredentialRefreshKeepsStableID(t *testing.T) {
	authDir := t.TempDir()
	authFile := filepath.Join(authDir, "codex-account.json")
	writeAuth := func(token string) {
		t.Helper()
		payload := []byte(`{"type":"codex","access_token":"` + token + `"}`)
		if errWrite := os.WriteFile(authFile, payload, 0o600); errWrite != nil {
			t.Fatalf("write auth file: %v", errWrite)
		}
	}

	w, errWatcher := NewWatcher(filepath.Join(t.TempDir(), "config.yaml"), authDir, nil)
	if errWatcher != nil {
		t.Fatalf("new watcher: %v", errWatcher)
	}
	defer func() { _ = w.Stop() }()
	w.SetConfig(&config.Config{AuthDir: authDir})
	updates := make(chan AuthUpdate, 4)
	w.SetAuthUpdateQueue(updates)

	waitUpdate := func() AuthUpdate {
		t.Helper()
		select {
		case update := <-updates:
			return update
		case <-time.After(5 * time.Second):
			t.Fatal("timed out waiting for auth update")
			return AuthUpdate{}
		}
	}

	writeAuth("token-one")
	w.addOrUpdateClient(authFile)
	first := waitUpdate()
	if first.Action != AuthUpdateActionAdd || first.Auth == nil {
		t.Fatalf("first update = %#v, want add", first)
	}
	if got, _ := first.Auth.Metadata["access_token"].(string); got != "token-one" {
		t.Fatalf("first access token = %q, want token-one", got)
	}

	writeAuth("token-two")
	w.addOrUpdateClient(authFile)
	second := waitUpdate()
	if second.Action != AuthUpdateActionModify || second.Auth == nil {
		t.Fatalf("second update = %#v, want modify", second)
	}
	if second.ID != first.ID || second.Auth.ID != first.Auth.ID {
		t.Fatalf("auth ID changed across credential refresh: first=%q second=%q auth=%q", first.ID, second.ID, second.Auth.ID)
	}
	if got, _ := second.Auth.Metadata["access_token"].(string); got != "token-two" {
		t.Fatalf("second access token = %q, want token-two", got)
	}
}
