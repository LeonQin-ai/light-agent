//go:build linux

package execution

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

const kvmGetAPIVersion = 0xAE00
const cgroup2SuperMagic = 0x63677270

func Detect(ctx context.Context, opts ProbeOptions) Snapshot {
	now := time.Now().UTC()
	s := Snapshot{
		Platform: PlatformUnknown,
		DetectedAtUnixMS: now.UnixMilli(),
		ProbeVersion: "1",
		ObservationScope: "unknown",
		Evidence: []string{},
	}
	detectPlatform(ctx, &s)
	if s.Platform == PlatformContainer { s.ObservationScope = "container" } else if s.Platform == PlatformBareMetal || s.Platform == PlatformVM { s.ObservationScope = "host" }
	detectKVM(&s)
	detectCgroupV2(&s)
	detectDocker(ctx, opts.DockerSocket, &s)
	detectFirecracker(&s)
	detectGVisor(&s)
	detectWASM(ctx, opts.WASMRuntime, &s)
	return s
}

func detectPlatform(ctx context.Context, s *Snapshot) {
	if path, err := exec.LookPath("systemd-detect-virt"); err == nil {
		containerOut, containerErr := exec.CommandContext(ctx, path, "--container").CombinedOutput()
		containerType := strings.TrimSpace(string(containerOut))
		if containerErr == nil && containerType != "" && containerType != "none" {
			s.Platform = PlatformContainer
			s.Evidence = append(s.Evidence, "systemd-detect-virt --container="+containerType)
			return
		}
		if detectContainerMarkers(s) { return }
		vmOut, vmErr := exec.CommandContext(ctx, path, "--vm").CombinedOutput()
		vmType := strings.TrimSpace(string(vmOut))
		if vmErr == nil && vmType != "" && vmType != "none" {
			s.Platform = PlatformVM
			s.Evidence = append(s.Evidence, "systemd-detect-virt --vm="+vmType)
			return
		}
		// "none" is the expected result on bare metal. Empty output or a
		// timeout is inconclusive and must not be reported as bare metal.
		if strings.TrimSpace(string(containerOut)) == "none" && strings.TrimSpace(string(vmOut)) == "none" {
			s.Platform = PlatformBareMetal
			s.Evidence = append(s.Evidence, "systemd-detect-virt reports no VM/container")
			return
		}
		if detectContainerMarkers(s) { return }
		s.Evidence = append(s.Evidence, "systemd-detect-virt probe was inconclusive")
		return
	}
	if detectContainerMarkers(s) {
		return
	}
	vendor, _ := os.ReadFile("/sys/class/dmi/id/sys_vendor")
	product, _ := os.ReadFile("/sys/class/dmi/id/product_name")
	dmi := strings.ToLower(strings.TrimSpace(string(vendor) + " " + string(product)))
	for _, marker := range []string{"qemu", "kvm", "vmware", "virtualbox", "microsoft virtual machine", "xen", "amazon ec2", "google compute engine"} {
		if strings.Contains(dmi, marker) {
			s.Platform = PlatformVM
			s.Evidence = append(s.Evidence, "DMI identifies virtualized platform")
			return
		}
	}
	s.Evidence = append(s.Evidence, "platform could not be determined reliably")
}

func detectKVM(s *Snapshot) {
	const device = "/dev/kvm"
	f, err := os.OpenFile(device, os.O_RDWR, 0)
	if err != nil {
		s.KVM = KVMCapability{Device: device, Reason: fmt.Sprintf("cannot open KVM device: %v", err)}
		return
	}
	defer f.Close()
	version, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), uintptr(kvmGetAPIVersion), 0)
	if errno != 0 {
		s.KVM = KVMCapability{Device: device, Reason: fmt.Sprintf("KVM_GET_API_VERSION failed: %v", errno)}
		return
	}
	if int(version) != 12 {
		s.KVM = KVMCapability{Device: device, APIVersion: int(version), Reason: fmt.Sprintf("unexpected KVM API version %d", version)}
		return
	}
	s.KVM = KVMCapability{Available: true, APIVersion: int(version), Device: device}
}

func detectCgroupV2(s *Snapshot) {
	var st syscall.Statfs_t
	if err := syscall.Statfs("/sys/fs/cgroup", &st); err == nil {
		s.CgroupV2 = uint64(st.Type) == cgroup2SuperMagic
	}
}

func detectDocker(ctx context.Context, socket string, s *Snapshot) {
	if socket == "" {
		socket = "/var/run/docker.sock"
	}
	info := DockerCapability{Socket: socket}
	st, err := os.Stat(socket)
	if err != nil || st.Mode()&os.ModeSocket == 0 {
		info.Reason = "Docker socket is unavailable; Host manager must own daemon access"
		s.Docker = info
		return
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{Timeout: 500 * time.Millisecond}).DialContext(ctx, "unix", socket)
	}}
	client := &http.Client{Transport: transport, Timeout: 1200 * time.Millisecond}
	defer transport.CloseIdleConnections()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/_ping", nil)
	resp, err := client.Do(req)
	if err != nil {
		info.Reason = fmt.Sprintf("Docker daemon probe failed: %v", err)
		s.Docker = info
		return
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		info.Reason = fmt.Sprintf("Docker daemon returned HTTP %d", resp.StatusCode)
		s.Docker = info
		return
	}
	req, _ = http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/info", nil)
	resp, err = client.Do(req)
	if err != nil {
		info.Reason = fmt.Sprintf("Docker info probe failed: %v", err)
		s.Docker = info
		return
	}
	defer resp.Body.Close()
	var data struct {
		DefaultRuntime string                     `json:"DefaultRuntime"`
		Runtimes       map[string]json.RawMessage `json:"Runtimes"`
	}
	if resp.StatusCode != http.StatusOK || json.NewDecoder(resp.Body).Decode(&data) != nil {
		info.Reason = "Docker info response is invalid"
		s.Docker = info
		return
	}
	info.Available = true
	info.DefaultRuntime = data.DefaultRuntime
	_, info.RuncAvailable = data.Runtimes["runc"]
	if info.DefaultRuntime == "" || info.DefaultRuntime == "runc" {
		info.RuncAvailable = true
	}
	_, info.GVisorRegistered = data.Runtimes["runsc"]
	if !info.RuncAvailable {
		info.Reason = "Docker daemon is available but runc runtime was not reported"
	}
	s.Docker = info
}

func detectFirecracker(s *Snapshot) {
	fc, fcErr := exec.LookPath("firecracker")
	jailer, jailerErr := exec.LookPath("jailer")
	s.Firecracker.FirecrackerPath = fc
	s.Firecracker.JailerPath = jailer
	switch {
	case !s.KVM.Available:
		s.Firecracker.Reason = "KVM is unavailable"
	case fcErr != nil:
		s.Firecracker.Reason = "firecracker binary is unavailable"
	case jailerErr != nil:
		s.Firecracker.Reason = "jailer binary is unavailable"
	case !s.CgroupV2:
		s.Firecracker.Reason = "cgroup v2 is unavailable"
	default:
		s.Firecracker.PrerequisitesOK = true
		s.Firecracker.Ready = false
		s.Firecracker.Reason = "static prerequisites passed; isolated boot preflight is still required"
	}
}

func detectGVisor(s *Snapshot) {
	path, err := exec.LookPath("runsc")
	if err != nil {
		s.GVisor = RuntimeCapability{Reason: "runsc binary is unavailable"}
		return
	}
	out, err := exec.Command(path, "--version").CombinedOutput()
	if err != nil {
		s.GVisor = RuntimeCapability{Available: true, Reason: fmt.Sprintf("runsc exists but version probe failed: %v", err)}
		return
	}
	version := strings.TrimSpace(string(out))
	if !s.Docker.GVisorRegistered {
		s.GVisor = RuntimeCapability{Available: true, Version: version, Reason: "runsc is installed but not registered with the detected Docker daemon"}
		return
	}
	s.GVisor = RuntimeCapability{Available: true, Version: version, Reason: "runsc is installed and registered; disposable runtime preflight is still required"}
}

func detectWASM(ctx context.Context, runtime string, s *Snapshot) {
	if runtime == "" {
		runtime = "wasmtime"
	}
	path, err := exec.LookPath(runtime)
	if err != nil {
		s.WASM = RuntimeCapability{Reason: runtime + " binary is unavailable"}
		return
	}
	cmdCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(cmdCtx, path, "--version").CombinedOutput()
	if err != nil {
		s.WASM = RuntimeCapability{Available: true, Reason: fmt.Sprintf("%s exists but version probe failed: %v", runtime, err)}
		return
	}
	s.WASM = RuntimeCapability{Available: true, Ready: false, Version: strings.TrimSpace(string(out)), Reason: "runtime version probe passed; WASI capability, fuel and memory enforcement preflight is still required"}
}


func detectContainerMarkers(s *Snapshot) bool {
	for _, marker := range []string{"/.dockerenv", "/run/.containerenv"} {
		if _, err := os.Stat(marker); err == nil {
			s.Platform = PlatformContainer
			s.Evidence = append(s.Evidence, "container marker "+marker)
			return true
		}
	}
	if data, err := os.ReadFile("/proc/1/cgroup"); err == nil {
		value := strings.ToLower(string(data))
		for _, marker := range []string{"docker", "kubepods", "containerd", "libpod", "lxc"} {
			if strings.Contains(value, marker) {
				s.Platform = PlatformContainer
				s.Evidence = append(s.Evidence, "container marker in /proc/1/cgroup")
				return true
			}
		}
	}
	return false
}
