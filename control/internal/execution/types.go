package execution

type Platform string

const (
	PlatformBareMetal Platform = "bare_metal"
	PlatformVM        Platform = "virtual_machine"
	PlatformContainer Platform = "container"
	PlatformUnknown  Platform = "unknown"
)

type Backend string

const (
	BackendDocker      Backend = "docker"
	BackendGVisor      Backend = "gvisor"
	BackendWASM        Backend = "wasm"
	BackendFirecracker Backend = "firecracker"
)

type Mode string

const (
	ModeStrict       Mode = "strict"
	ModeCompatible   Mode = "compatible"
	ModeAvailability Mode = "availability"
)

type TrustTier string

const (
	TierTrustedInternal     TrustTier = "trusted_internal"
	TierExternalUntrusted   TrustTier = "external_untrusted"
	TierConstrainedExtension TrustTier = "constrained_extension"
)

type RuntimeCapability struct {
	Available bool   `json:"available"`
	Ready     bool   `json:"ready"`
	Version   string `json:"version,omitempty"`
	Reason    string `json:"reason,omitempty"`
}

type DockerCapability struct {
	Available      bool   `json:"available"`
	RuncAvailable  bool   `json:"runc_available"`
	GVisorRegistered bool `json:"gvisor_registered"`
	DefaultRuntime string `json:"default_runtime,omitempty"`
	Socket         string `json:"socket,omitempty"`
	Reason         string `json:"reason,omitempty"`
}

type KVMCapability struct {
	Available bool   `json:"available"`
	APIVersion int    `json:"api_version,omitempty"`
	Device     string `json:"device,omitempty"`
	Reason     string `json:"reason,omitempty"`
}

type FirecrackerCapability struct {
	PrerequisitesOK bool   `json:"prerequisites_ok"`
	Ready           bool   `json:"ready"`
	FirecrackerPath string `json:"firecracker_path,omitempty"`
	JailerPath      string `json:"jailer_path,omitempty"`
	Reason          string `json:"reason,omitempty"`
}

type Snapshot struct {
	RegistryVersion uint64             `json:"registry_version,omitempty"`
	ObservationScope string                `json:"observation_scope"`
	NodeID       string                `json:"node_id,omitempty"`
	Platform     Platform              `json:"platform"`
	Evidence     []string              `json:"platform_evidence,omitempty"`
	DetectedAtUnixMS int64                `json:"detected_at_unix_ms"`
	ProbeVersion string                `json:"probe_version"`
	Docker       DockerCapability      `json:"docker"`
	KVM          KVMCapability         `json:"kvm"`
	Firecracker  FirecrackerCapability `json:"firecracker"`
	GVisor       RuntimeCapability     `json:"gvisor"`
	WASM         RuntimeCapability     `json:"wasm"`
	CgroupV2     bool                  `json:"cgroup_v2"`
}

type ProbeOptions struct {
	DockerSocket string
	WASMRuntime  string
}

// TaskPolicy is constructed by trusted admission code, never directly from Agent input.
type TaskPolicy struct {
	Tier                   TrustTier
	WASMCompatible         bool
	AllowGVisorAlternate   bool // trusted tenant/operator policy only
	AdminApprovalReference string // must be checked by ApprovalVerifier
}

type Decision struct {
	Allowed       bool   `json:"allowed"`
	Backend       Backend `json:"backend,omitempty"`
	Boundary      string `json:"boundary,omitempty"`
	Reason        string `json:"reason"`
	Downgraded    bool   `json:"downgraded"`
	ErrorCode     string `json:"error_code,omitempty"`
}
