package execution

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

func TestCapabilityRegistryInvalidationAndRecovery(t *testing.T) {
	now := time.Unix(1000, 0)
	fetchErr := error(nil)
	host := Snapshot{ObservationScope: "host", Evidence: []string{"first"}, Docker: DockerCapability{Available: true, RuncAvailable: true}}
	registry := NewCapabilityRegistry(time.Minute, func(context.Context) (Snapshot, error) { return host, fetchErr })
	registry.now = func() time.Time { return now }
	allowed := func() bool {
		return Resolve(ModeStrict, TaskPolicy{Tier: TierTrustedInternal}, registry.CurrentSnapshot()).Allowed
	}
	if allowed() {
		t.Fatal("uninitialized registry authorized execution")
	}
	if err := registry.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !allowed() {
		t.Fatal("fresh Host snapshot did not authorize Docker")
	}
	copy := registry.CurrentSnapshot()
	copy.Evidence[0] = "changed"
	host.Evidence[0] = "fetcher changed"
	if registry.CurrentSnapshot().Evidence[0] != "first" {
		t.Fatal("snapshot was mutated externally")
	}
	now = now.Add(time.Minute)
	if allowed() {
		t.Fatal("expired snapshot authorized execution")
	}
	if err := registry.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !allowed() {
		t.Fatal("refreshed snapshot did not restore execution")
	}
	fetchErr = errors.New("sensitive upstream error")
	if err := registry.Refresh(context.Background()); !errors.Is(err, fetchErr) {
		t.Fatalf("refresh error = %v", err)
	}
	if allowed() {
		t.Fatal("failed refresh retained previous execution authorization")
	}
	if registry.CurrentSnapshot().Evidence[0] == fetchErr.Error() {
		t.Fatal("public snapshot exposes upstream error")
	}
	fetchErr = nil
	if err := registry.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !allowed() {
		t.Fatal("recovery did not restore execution")
	}
	host.ObservationScope = "process"
	if err := registry.Refresh(context.Background()); err == nil {
		t.Fatal("accepted a process-scope snapshot")
	}
	if allowed() {
		t.Fatal("process-scope response retained authorization")
	}
}

func TestCapabilityRegistryConcurrentReadsAndRefreshes(t *testing.T) {
	r := NewCapabilityRegistry(time.Minute, func(context.Context) (Snapshot, error) {
		return Snapshot{ObservationScope: "host", Evidence: []string{"host"}}, nil
	})
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				if err := r.Refresh(context.Background()); err != nil {
					t.Error(err)
				}
				s := r.CurrentSnapshot()
				if s.ObservationScope != "host" {
					t.Errorf("unexpected snapshot: %+v", s)
				}
				s.Evidence[0] = "caller mutation"
			}
		}()
	}
	wg.Wait()
	if r.CurrentSnapshot().Evidence[0] != "host" {
		t.Fatal("reader mutated shared snapshot")
	}
}

func TestCapabilityRegistryCancelledRefreshAndShutdown(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r := NewCapabilityRegistry(time.Minute, func(context.Context) (Snapshot, error) { return Snapshot{ObservationScope: "host"}, nil })
	if err := r.Refresh(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("refresh error = %v", err)
	}
	if r.CurrentSnapshot().ObservationScope == "host" {
		t.Fatal("cancelled refresh published readiness")
	}
	done := make(chan struct{})
	go func() { r.Run(ctx, time.Hour, time.Second, nil); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("refresh loop did not shut down")
	}
	if err := NewCapabilityRegistry(time.Minute, nil).Refresh(context.Background()); err == nil {
		t.Fatal("missing fetcher accepted")
	}
}

func TestCapabilityRegistryPeriodicRefresh(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	observations := make(chan struct{}, 2)
	r := NewCapabilityRegistry(time.Minute, func(context.Context) (Snapshot, error) {
		select {
		case observations <- struct{}{}:
		default:
		}
		return Snapshot{ObservationScope: "host"}, nil
	})
	done := make(chan struct{})
	go func() { r.Run(ctx, time.Millisecond, time.Second, nil); close(done) }()
	for i := 0; i < 2; i++ {
		select {
		case <-observations:
		case <-time.After(2 * time.Second):
			t.Fatal("periodic refresh did not run")
		}
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("periodic loop did not stop")
	}
}

type failingCapabilityJournal struct{}

func (failingCapabilityJournal) SaveCapabilitySnapshot(context.Context, Snapshot) (uint64, error) {
	return 0, errors.New("database unavailable")
}

func TestCapabilityPersistenceFailureDisablesExecution(t *testing.T) {
	r := NewPersistentCapabilityRegistry(time.Minute, func(context.Context) (Snapshot, error) {
		return Snapshot{ObservationScope: "host", Docker: DockerCapability{Available: true, RuncAvailable: true}}, nil
	}, failingCapabilityJournal{})
	if err := r.Refresh(context.Background()); err == nil {
		t.Fatal("ignored journal error")
	}
	if Resolve(ModeStrict, TaskPolicy{Tier: TierTrustedInternal}, r.CurrentSnapshot()).Allowed {
		t.Fatal("unpersisted snapshot authorized execution")
	}
}
