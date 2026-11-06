package execution

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestFetchHostCapabilitiesRequiresTokenAndHostScope(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/capabilities" { t.Fatalf("unexpected path: %s", r.URL.Path) }
		if r.Header.Get("Authorization") != "Bearer manager-secret" { w.WriteHeader(http.StatusUnauthorized); return }
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"observation_scope":"host","platform":"virtual_machine","kvm":{"available":false}}`))
	}))
	defer server.Close()

	snapshot, err := FetchHostCapabilities(context.Background(), server.URL, "manager-secret")
	if err != nil { t.Fatal(err) }
	if snapshot.ObservationScope != "host" || snapshot.Platform != PlatformVM {
		t.Fatalf("unexpected snapshot: %+v", snapshot)
	}
}

func TestFetchHostCapabilitiesRejectsContainerScope(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"observation_scope":"container","platform":"container"}`))
	}))
	defer server.Close()
	if _, err := FetchHostCapabilities(context.Background(), server.URL, "manager-secret"); err == nil {
		t.Fatal("expected non-host capability scope to be rejected")
	}
}
