package execution

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

// CapabilitySource supplies the current authoritative execution capabilities.
type CapabilitySource interface {
	CurrentSnapshot() Snapshot
}

// CapabilityJournal durably assigns a monotonic observation version. Historical
// snapshots are for audit only and must never authorize execution after restart.
type CapabilityJournal interface {
	SaveCapabilitySnapshot(context.Context, Snapshot) (uint64, error)
}

// CapabilityRegistry keeps a bounded-age snapshot. A failed refresh invalidates
// readiness immediately; it never retains an earlier execution authorization.
type CapabilityRegistry struct {
	mu        sync.RWMutex
	refreshMu sync.Mutex
	snapshot  Snapshot
	updated   time.Time
	maxAge    time.Duration
	fetch     func(context.Context) (Snapshot, error)
	now       func() time.Time
	journal   CapabilityJournal
}

func NewCapabilityRegistry(maxAge time.Duration, fetch func(context.Context) (Snapshot, error)) *CapabilityRegistry {
	return &CapabilityRegistry{maxAge: maxAge, fetch: fetch, now: time.Now}
}

func NewPersistentCapabilityRegistry(maxAge time.Duration, fetch func(context.Context) (Snapshot, error), journal CapabilityJournal) *CapabilityRegistry {
	r := NewCapabilityRegistry(maxAge, fetch)
	r.journal = journal
	return r
}

func unavailableSnapshot(reason string) Snapshot {
	return Snapshot{Platform: PlatformUnknown, ObservationScope: "unavailable", ProbeVersion: "registry-v1", Evidence: []string{reason}}
}

func (r *CapabilityRegistry) CurrentSnapshot() Snapshot {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.updated.IsZero() || r.maxAge <= 0 || r.now().Sub(r.updated) >= r.maxAge {
		return unavailableSnapshot("authoritative Host capability snapshot is missing or expired")
	}
	snapshot := r.snapshot
	// Callers cannot mutate the registry through the slice in a value copy.
	snapshot.Evidence = append([]string(nil), snapshot.Evidence...)
	return snapshot
}

func (r *CapabilityRegistry) Refresh(ctx context.Context) error {
	// Serializing refreshes prevents a delayed older response from replacing a
	// newer observation. Fetchers must honor the supplied context.
	r.refreshMu.Lock()
	defer r.refreshMu.Unlock()
	var snapshot Snapshot
	var err error
	if r.fetch == nil {
		err = fmt.Errorf("Host capability fetcher is not configured")
	} else {
		snapshot, err = r.fetch(ctx)
	}
	if err == nil && snapshot.ObservationScope != "host" {
		err = fmt.Errorf("authoritative Host capabilities are required")
	}
	if err == nil {
		err = ctx.Err()
	}
	if err != nil {
		// Do not put upstream errors (which may contain connection details) in
		// the public capability response.
		snapshot = unavailableSnapshot("authoritative Host capability refresh failed")
		// Revoke readiness before waiting for the durable journal write.
		r.mu.Lock()
		r.snapshot = snapshot
		r.updated = r.now()
		r.mu.Unlock()
	}
	snapshot.Evidence = append([]string(nil), snapshot.Evidence...)
	snapshot.RegistryVersion = 0
	if r.journal != nil {
		// A cancelled/failed probe must still leave a durable negative observation.
		persistCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
		version, persistErr := r.journal.SaveCapabilitySnapshot(persistCtx, snapshot)
		cancel()
		if persistErr != nil {
			err = errors.Join(err, fmt.Errorf("persist Host capability observation: %w", persistErr))
			snapshot = unavailableSnapshot("Host capability observation could not be persisted")
		} else {
			snapshot.RegistryVersion = version
		}
	}
	r.mu.Lock()
	r.snapshot = snapshot
	r.updated = r.now()
	r.mu.Unlock()
	return err
}

// Run refreshes until shutdown. Call Refresh once before serving requests.
func (r *CapabilityRegistry) Run(ctx context.Context, interval, timeout time.Duration, onError func(error)) {
	if interval <= 0 || timeout <= 0 {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			probeCtx, cancel := context.WithTimeout(ctx, timeout)
			err := r.Refresh(probeCtx)
			cancel()
			if err != nil && ctx.Err() == nil && onError != nil {
				onError(err)
			}
		}
	}
}
