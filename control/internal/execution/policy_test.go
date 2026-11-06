package execution

import (
	"fmt"
	"testing"
)

type allowApproval struct{}
func (allowApproval) VerifyGVisorDowngrade(reference string, _ TaskPolicy) error {
	if reference != "approved-change-123" { return fmt.Errorf("approval not found") }
	return nil
}

func baseHost() Snapshot {
	return Snapshot{
		ObservationScope: "host",
		Docker: DockerCapability{Available: true, RuncAvailable: true},
		KVM: KVMCapability{Available: false, Reason: "no /dev/kvm"},
		Firecracker: FirecrackerCapability{Reason: "KVM unavailable"},
		WASM: RuntimeCapability{Available: true, Ready: true, Version: "wasmtime 1.0"},
		GVisor: RuntimeCapability{Available: true, Ready: true, Version: "runsc 1.0"},
	}
}

func TestTrustedInternalUsesDocker(t *testing.T) {
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierTrustedInternal}, baseHost())
	if !got.Allowed || got.Backend != BackendDocker || got.Downgraded {
		t.Fatalf("unexpected decision: %+v", got)
	}
}

func TestConstrainedExtensionUsesWASM(t *testing.T) {
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierConstrainedExtension, WASMCompatible: true}, baseHost())
	if !got.Allowed || got.Backend != BackendWASM {
		t.Fatalf("unexpected decision: %+v", got)
	}
}

func TestWASMIncompatibleExtensionDenied(t *testing.T) {
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierConstrainedExtension}, baseHost())
	if got.Allowed || got.ErrorCode != "wasm_incompatible" {
		t.Fatalf("unexpected decision: %+v", got)
	}
}

func TestExternalUntrustedWithoutRemoteOrKVMFailsClosed(t *testing.T) {
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierExternalUntrusted}, baseHost())
	if got.Allowed || got.ErrorCode != "no_compliant_executor" {
		t.Fatalf("unexpected decision: %+v", got)
	}
}

func TestExternalUntrustedCannotSilentlyUseGVisor(t *testing.T) {
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierExternalUntrusted}, baseHost())
	if got.Allowed {
		t.Fatalf("unexpected fallback: %+v", got)
	}
}

func TestGVisorAlternateRequiresExplicitPolicy(t *testing.T) {
	task := TaskPolicy{Tier: TierExternalUntrusted, AllowGVisorAlternate: true}
	got := Resolve(ModeCompatible, task, baseHost())
	if !got.Allowed || got.Backend != BackendGVisor || !got.Downgraded {
		t.Fatalf("unexpected decision: %+v", got)
	}
}

func TestStrictModeRejectsGVisorDowngrade(t *testing.T) {
	task := TaskPolicy{Tier: TierExternalUntrusted, AllowGVisorAlternate: true}
	got := Resolve(ModeStrict, task, baseHost())
	if got.Allowed {
		t.Fatalf("strict mode must reject downgrade: %+v", got)
	}
}

func TestAvailabilityModeRequiresApprovalReference(t *testing.T) {
	task := TaskPolicy{Tier: TierExternalUntrusted, AllowGVisorAlternate: true}
	got := Resolve(ModeAvailability, task, baseHost())
	if got.Allowed || got.ErrorCode != "admin_approval_required" {
		t.Fatalf("unexpected decision: %+v", got)
	}
	task.AdminApprovalReference = "change-123"
	got = Resolve(ModeAvailability, task, baseHost())
	if got.Allowed || got.ErrorCode != "approval_verifier_unavailable" {
		t.Fatalf("must fail closed without approval verifier: %+v", got)
	}
	task.AdminApprovalReference = "approved-change-123"
	got = ResolveWithApproval(ModeAvailability, task, baseHost(), allowApproval{})
	if !got.Allowed || !got.Downgraded {
		t.Fatalf("verified downgrade should be allowed: %+v", got)
	}
	task.AdminApprovalReference = "forged"
	got = ResolveWithApproval(ModeAvailability, task, baseHost(), allowApproval{})
	if got.Allowed || got.ErrorCode != "admin_approval_invalid" {
		t.Fatalf("forged approval must be rejected: %+v", got)
	}
}

func TestFirecrackerRequiresBootPreflight(t *testing.T) {
	host := baseHost()
	host.KVM.Available = true
	host.Firecracker.PrerequisitesOK = true
	host.Firecracker.Ready = false
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierExternalUntrusted}, host)
	if got.Allowed {
		t.Fatalf("must not select Firecracker before boot preflight: %+v", got)
	}
	host.Firecracker.Ready = true
	got = Resolve(ModeCompatible, TaskPolicy{Tier: TierExternalUntrusted}, host)
	if !got.Allowed || got.Backend != BackendFirecracker {
		t.Fatalf("expected Firecracker: %+v", got)
	}
}

func TestInvalidModeFailsClosed(t *testing.T) {
	got := Resolve(Mode("automatic-fallback"), TaskPolicy{Tier: TierExternalUntrusted, AllowGVisorAlternate: true}, baseHost())
	if got.Allowed || got.ErrorCode != "invalid_execution_mode" { t.Fatalf("unexpected decision: %+v", got) }
}

func TestResolverRejectsContainerScopedCapabilities(t *testing.T) {
	host := baseHost()
	host.ObservationScope = "container"
	got := Resolve(ModeCompatible, TaskPolicy{Tier: TierTrustedInternal}, host)
	if got.Allowed || got.ErrorCode != "host_capabilities_unavailable" {
		t.Fatalf("container-local probe must not authorize Host execution: %+v", got)
	}
}
