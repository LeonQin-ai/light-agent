package execution

import "strings"

type ApprovalVerifier interface {
	VerifyGVisorDowngrade(reference string, task TaskPolicy) error
}

func Resolve(mode Mode, task TaskPolicy, host Snapshot) Decision {
	return ResolveWithApproval(mode, task, host, nil)
}

func ResolveWithApproval(mode Mode, task TaskPolicy, host Snapshot, verifier ApprovalVerifier) Decision {
	if host.ObservationScope != "host" {
		return denied("host_capabilities_unavailable", "authoritative Host capabilities are required; current probe scope is "+host.ObservationScope)
	}
	if mode != ModeStrict && mode != ModeCompatible && mode != ModeAvailability {
		return denied("invalid_execution_mode", "execution mode must be strict, compatible or availability")
	}
	switch task.Tier {
	case TierTrustedInternal:
		if host.Docker.Available && host.Docker.RuncAvailable {
			return Decision{Allowed: true, Backend: BackendDocker, Boundary: "container", Reason: "trusted internal task uses hardened Docker/runc"}
		}
		return denied("no_compliant_executor", "Docker/runc is unavailable")
	case TierConstrainedExtension:
		if !task.WASMCompatible {
			return denied("wasm_incompatible", "constrained extension is not a WASM-compatible module")
		}
		if host.WASM.Ready {
			return Decision{Allowed: true, Backend: BackendWASM, Boundary: "wasm", Reason: "WASM-compatible extension uses the configured WASM runtime"}
		}
		return denied("no_compliant_executor", "WASM runtime is unavailable or failed its version probe")
	case TierExternalUntrusted:
		if host.Firecracker.Ready {
			return Decision{Allowed: true, Backend: BackendFirecracker, Boundary: "microvm", Reason: "external code uses a preflight-validated Firecracker MicroVM"}
		}
		if mode != ModeStrict && task.AllowGVisorAlternate && host.GVisor.Ready {
			if mode == ModeAvailability {
				reference := strings.TrimSpace(task.AdminApprovalReference)
				if reference == "" {
					return denied("admin_approval_required", "availability-mode downgrade requires an administrator approval reference")
				}
				if verifier == nil {
					return denied("approval_verifier_unavailable", "administrator approval cannot be trusted because no verifier is configured")
				}
				if err := verifier.VerifyGVisorDowngrade(reference, task); err != nil {
					return denied("admin_approval_invalid", "administrator approval could not be verified")
				}
			}
			return Decision{Allowed: true, Backend: BackendGVisor, Boundary: "gvisor", Downgraded: true, Reason: "explicit policy permits gVisor as an alternate boundary; this is not MicroVM-equivalent"}
		}
		if !host.Firecracker.PrerequisitesOK && !host.KVM.Available {
			return denied("no_compliant_executor", "KVM is unavailable and no approved local alternate boundary is ready; no remote Worker Pool is configured")
		}
		if !host.Firecracker.Ready {
			return denied("no_compliant_executor", "Firecracker has not passed its isolated boot preflight")
		}
		return denied("no_compliant_executor", "no local backend satisfies the external-untrusted task policy")
	default:
		return denied("invalid_trust_tier", "task trust tier must be assigned by trusted admission policy")
	}
}

func denied(code, reason string) Decision {
	return Decision{Allowed: false, ErrorCode: code, Reason: reason}
}
