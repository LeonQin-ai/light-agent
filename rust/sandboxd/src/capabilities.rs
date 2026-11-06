use serde::Serialize;
use std::{env, fs, os::fd::AsRawFd, path::Path, process::Command, time::{SystemTime, UNIX_EPOCH}};

#[derive(Serialize)]
pub struct RuntimeCapability {
    pub available: bool,
    pub ready: bool,
    pub version: Option<String>,
    pub reason: Option<String>,
}
#[derive(Serialize)]
pub struct DockerCapability {
    pub available: bool,
    pub runc_available: bool,
    pub gvisor_registered: bool,
    pub default_runtime: Option<String>,
    pub socket: String,
    pub reason: Option<String>,
}
#[derive(Serialize)]
pub struct KvmCapability {
    pub available: bool,
    pub api_version: Option<i32>,
    pub device: String,
    pub reason: Option<String>,
}
#[derive(Serialize)]
pub struct FirecrackerCapability {
    pub prerequisites_ok: bool,
    pub ready: bool,
    pub firecracker_path: Option<String>,
    pub jailer_path: Option<String>,
    pub reason: Option<String>,
}
#[derive(Serialize)]
pub struct HostCapabilitySnapshot {
    pub observation_scope: &'static str,
    pub node_id: Option<String>,
    pub platform: String,
    pub platform_evidence: Vec<String>,
    pub detected_at_unix_ms: i64,
    pub probe_version: &'static str,
    pub docker: DockerCapability,
    pub kvm: KvmCapability,
    pub firecracker: FirecrackerCapability,
    pub gvisor: RuntimeCapability,
    pub wasm: RuntimeCapability,
    pub cgroup_v2: bool,
}

pub fn detect() -> HostCapabilitySnapshot {
    let (platform, evidence) = detect_platform();
    let kvm = detect_kvm();
    let cgroup_v2 = fs::read_to_string("/proc/self/mountinfo")
        .map(|s| s.contains(" - cgroup2 "))
        .unwrap_or(false);
    let docker = detect_docker();
    let firecracker = detect_firecracker(&kvm, cgroup_v2);
    let gvisor = detect_gvisor(&docker);
    let wasm = detect_wasm();
    let detected_at_unix_ms = SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64).unwrap_or_default();
    HostCapabilitySnapshot {
        observation_scope: if platform == "container" { "container" } else if platform == "unknown" { "unknown" } else { "host" },
        node_id: env::var("HOSTNAME").ok(),
        platform,
        platform_evidence: evidence,
        detected_at_unix_ms,
        probe_version: "1",
        docker,
        kvm,
        firecracker,
        gvisor,
        wasm,
        cgroup_v2,
    }
}

fn detect_platform() -> (String, Vec<String>) {
    if let Some(value) = detect_virt("--container") {
        if value != "none" { return ("container".into(), vec![format!("systemd-detect-virt --container={value}")]); }
    }
    if Path::new("/.dockerenv").exists() || Path::new("/run/.containerenv").exists() {
        return ("container".into(), vec!["container marker file detected".into()]);
    }
    let cgroup = fs::read_to_string("/proc/1/cgroup").unwrap_or_default().to_lowercase();
    if ["docker", "kubepods", "containerd", "libpod", "lxc"].iter().any(|m| cgroup.contains(m)) {
        return ("container".into(), vec!["container marker in /proc/1/cgroup".into()]);
    }
    if let Some(value) = detect_virt("--vm") {
        if value != "none" { return ("virtual_machine".into(), vec![format!("systemd-detect-virt --vm={value}")]); }
        if detect_virt("--container").as_deref() == Some("none") {
            return ("bare_metal".into(), vec!["systemd-detect-virt reports no VM/container".into()]);
        }
    }
    if Path::new("/.dockerenv").exists() || Path::new("/run/.containerenv").exists() {
        return ("container".into(), vec!["container marker file detected".into()]);
    }
    let cgroup = fs::read_to_string("/proc/1/cgroup").unwrap_or_default().to_lowercase();
    if ["docker", "kubepods", "containerd", "libpod", "lxc"].iter().any(|m| cgroup.contains(m)) {
        return ("container".into(), vec!["container marker in /proc/1/cgroup".into()]);
    }
    let dmi = format!("{} {}", read_trim("/sys/class/dmi/id/sys_vendor"), read_trim("/sys/class/dmi/id/product_name")).to_lowercase();
    for marker in ["qemu", "kvm", "vmware", "virtualbox", "microsoft virtual machine", "xen", "amazon ec2", "google compute engine"] {
        if dmi.contains(marker) { return ("virtual_machine".into(), vec!["DMI identifies virtualized platform".into()]); }
    }
    ("unknown".into(), vec!["platform detection inconclusive".into()])
}

fn detect_virt(arg: &str) -> Option<String> {
    let output = Command::new("systemd-detect-virt").arg(arg).output().ok()?;
    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if value.is_empty() { None } else { Some(value) }
}

fn detect_kvm() -> KvmCapability {
    let device = "/dev/kvm";
    let file = match fs::OpenOptions::new().read(true).write(true).open(device) {
        Ok(f) => f,
        Err(e) => return KvmCapability { available: false, api_version: None, device: device.into(), reason: Some(format!("cannot open KVM device: {e}")) },
    };
    // KVM_GET_API_VERSION is _IO(0xAE, 0x00). Opening the device is not enough:
    // this ioctl also verifies that the current service identity can use KVM.
    let version = unsafe { libc::ioctl(file.as_raw_fd(), 0xAE00) };
    if version != 12 {
        return KvmCapability { available: false, api_version: if version >= 0 { Some(version) } else { None }, device: device.into(), reason: Some(format!("KVM_GET_API_VERSION returned {version}")) };
    }
    KvmCapability { available: true, api_version: Some(version), device: device.into(), reason: None }
}

fn detect_docker() -> DockerCapability {
    let socket = env::var("DOCKER_HOST")
        .ok().and_then(|v| v.strip_prefix("unix://").map(str::to_string))
        .unwrap_or_else(|| "/var/run/docker.sock".into());
    if !Path::new(&socket).exists() {
        return DockerCapability { available: false, runc_available: false, gvisor_registered: false, default_runtime: None, socket, reason: Some("Docker socket is unavailable".into()) };
    }
    let runtimes = Command::new("docker").args(["info", "--format", "{{json .Runtimes}}"]).output();
    let default = Command::new("docker").args(["info", "--format", "{{.DefaultRuntime}}"]).output();
    let (runtimes, default) = match (runtimes, default) {
        (Ok(r), Ok(d)) if r.status.success() && d.status.success() =>
            (String::from_utf8_lossy(&r.stdout).to_string(), String::from_utf8_lossy(&d.stdout).trim().to_string()),
        _ => return DockerCapability { available: false, runc_available: false, gvisor_registered: false, default_runtime: None, socket, reason: Some("Docker daemon info probe failed".into()) },
    };
    let runc = runtimes.contains("\"runc\"") || default.is_empty() || default == "runc";
    let runsc = runtimes.contains("\"runsc\"");
    DockerCapability { available: true, runc_available: runc, gvisor_registered: runsc, default_runtime: if default.is_empty() { None } else { Some(default) }, socket, reason: None }
}

fn detect_firecracker(kvm: &KvmCapability, cgroup_v2: bool) -> FirecrackerCapability {
    let fc = find_binary("firecracker");
    let jailer = find_binary("jailer");
    let reason = if !kvm.available { Some("KVM is unavailable".into()) }
        else if fc.is_none() { Some("firecracker binary is unavailable".into()) }
        else if jailer.is_none() { Some("jailer binary is unavailable".into()) }
        else if !cgroup_v2 { Some("cgroup v2 is unavailable".into()) }
        else { Some("static prerequisites passed; isolated Guest boot preflight is still required".into()) };
    let prerequisites_ok = kvm.available && fc.is_some() && jailer.is_some() && cgroup_v2;
    FirecrackerCapability { prerequisites_ok, ready: false, firecracker_path: fc, jailer_path: jailer, reason }
}

fn detect_gvisor(docker: &DockerCapability) -> RuntimeCapability {
    let path = find_binary("runsc");
    let Some(path) = path else { return RuntimeCapability { available: false, ready: false, version: None, reason: Some("runsc binary is unavailable".into()) }; };
    let output = Command::new(&path).arg("--version").output();
    let version = output.ok().filter(|o| o.status.success()).map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string());
    if !docker.gvisor_registered {
        return RuntimeCapability { available: true, ready: false, version, reason: Some("runsc is installed but not registered with Docker".into()) };
    }
    RuntimeCapability { available: true, ready: false, version, reason: Some("runsc is installed and registered; disposable container preflight is still required".into()) }
}

fn detect_wasm() -> RuntimeCapability {
    for name in ["wasmtime", "wasmedge"] {
        if let Some(path) = find_binary(name) {
            let output = Command::new(&path).arg("--version").output();
            let version = output.ok().filter(|o| o.status.success()).map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string());
            return RuntimeCapability { available: version.is_some(), ready: false, version, reason: Some("runtime detected; WASI capability, fuel and memory-limit preflight is still required".into()) };
        }
    }
    RuntimeCapability { available: false, ready: false, version: None, reason: Some("Wasmtime/WasmEdge binary is unavailable".into()) }
}

fn find_binary(name: &str) -> Option<String> {
    let path = env::var_os("PATH")?;
    env::split_paths(&path).map(|p| p.join(name)).find(|p| p.is_file()).map(|p| p.to_string_lossy().to_string())
}
fn read_trim(path: &str) -> String { fs::read_to_string(path).unwrap_or_default().trim().to_string() }
