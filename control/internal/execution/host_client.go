package execution

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net"
	"net/url"
	"strings"
	"time"
)

// FetchHostCapabilities retrieves the authoritative snapshot from cos-sandboxd.
// The Control Plane must not infer Host capabilities from its own container view.
func FetchHostCapabilities(ctx context.Context, baseURL, token string) (Snapshot, error) {
	if strings.TrimSpace(token) == "" {
		return Snapshot{}, fmt.Errorf("sandbox manager token is required")
	}
	u, err := url.Parse(strings.TrimRight(baseURL, "/") + "/v1/capabilities")
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return Snapshot{}, fmt.Errorf("invalid sandbox manager URL")
	}
	if u.Scheme == "http" {
		host := strings.Trim(u.Hostname(), "[]")
		ip := net.ParseIP(host)
		if host != "localhost" && (ip == nil || !ip.IsLoopback()) {
			return Snapshot{}, fmt.Errorf("non-loopback Sandbox Manager URLs require HTTPS")
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil { return Snapshot{}, err }
	req.Header.Set("Authorization", "Bearer "+token)
	client := &http.Client{Timeout: 2 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil { return Snapshot{}, fmt.Errorf("query sandbox manager: %w", err) }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK { return Snapshot{}, fmt.Errorf("sandbox manager returned HTTP %d", resp.StatusCode) }
	var snapshot Snapshot
	if err := json.NewDecoder(resp.Body).Decode(&snapshot); err != nil { return Snapshot{}, fmt.Errorf("decode host capabilities: %w", err) }
	if snapshot.ObservationScope != "host" { return Snapshot{}, fmt.Errorf("sandbox manager returned non-host capability scope %q", snapshot.ObservationScope) }
	return snapshot, nil
}
