use crate::config::{validate_workspace_id, Config};
use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    ffi::CString,
    fs,
    os::unix::ffi::OsStrExt,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{io::AsyncWriteExt, process::Command, time::timeout};
use uuid::Uuid;
use walkdir::WalkDir;

const WORKER_UID: u32 = 65532;
const WORKER_GID: u32 = 65532;
const MAX_EXEC_TIMEOUT_MS: u64 = 300_000;
const MAX_EXEC_OUTPUT: usize = 1024 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ContainerRecord {
    pub container_id: String,
    pub docker_id: String,
    pub docker_name: String,
    pub worker_id: String,
    pub workspace_id: String,
    pub workspace_root: String,
    pub image_digest: String,
    pub state: String,
    pub cpu_millis: u32,
    pub memory_bytes: u64,
    pub pids_limit: u64,
    pub last_error: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Deserialize)]
pub struct CreateContainerSpec {
    pub worker_id: String,
    pub workspace_id: String,
    #[serde(default)]
    pub cpu_millis: u32,
    #[serde(default)]
    pub memory_bytes: u64,
    #[serde(default)]
    pub pids_limit: u64,
}

#[derive(Debug, Deserialize)]
pub struct ContainerExecRequest {
    pub executable: String,
    #[serde(default)]
    pub argv: Vec<String>,
    #[serde(default)]
    pub cwd: String,
    #[serde(default = "default_timeout")]
    pub timeout_ms: u64,
    #[serde(default = "default_memory")]
    pub memory_bytes: u64,
    #[serde(default = "default_output")]
    pub max_output_bytes: usize,
    #[serde(default = "default_cpu")]
    pub cpu_quota_micros: u64,
    #[serde(default = "default_pids")]
    pub max_pids: u64,
    #[serde(default)]
    pub network: bool,
}
fn default_timeout() -> u64 { 15_000 }
fn default_memory() -> u64 { 512 * 1024 * 1024 }
fn default_output() -> usize { 1024 * 1024 }
fn default_cpu() -> u64 { 100_000 }
fn default_pids() -> u64 { 64 }

#[derive(Clone)]
pub struct DockerProvider {
    cfg: Config,
    records: Arc<Mutex<HashMap<String, ContainerRecord>>>,
    persist_lock: Arc<Mutex<()>>,
    operation_lock: Arc<tokio::sync::Mutex<()>>,
    container_locks: Arc<tokio::sync::Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>>,
    exec_locks: Arc<tokio::sync::Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>>,
}

impl DockerProvider {
    pub fn load(cfg: Config) -> Result<Self> {
        if let Some(parent) = cfg.docker_state_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let records = if cfg.docker_state_path.exists() {
            serde_json::from_slice(&fs::read(&cfg.docker_state_path)?)
                .context("parse Docker container state file")?
        } else {
            HashMap::new()
        };
        let provider = Self {
            cfg,
            records: Arc::new(Mutex::new(records)),
            persist_lock: Arc::new(Mutex::new(())),
            operation_lock: Arc::new(tokio::sync::Mutex::new(())),
            container_locks: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
            exec_locks: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        };
        Ok(provider)
    }

    fn persist(&self) -> Result<()> {
        let _persist_guard = self.persist_lock.lock().map_err(|_| anyhow!("Docker persist lock poisoned"))?;
        let snapshot = self.records.lock()
            .map_err(|_| anyhow!("Docker state lock poisoned"))?
            .clone();
        let bytes = serde_json::to_vec_pretty(&snapshot)?;
        let path = &self.cfg.docker_state_path;
        let tmp = path.with_extension("tmp");
        let mut file = fs::File::create(&tmp)?;
        use std::io::Write;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&tmp, path)
            .with_context(|| format!("replace Docker state file {}", path.display()))?;
        Ok(())
    }

    pub async fn create(&self, spec: CreateContainerSpec) -> Result<ContainerRecord> {
        let _guard = self.operation_lock.lock().await;
        validate_worker_id(&spec.worker_id)?;
        if !validate_workspace_id(&spec.workspace_id) {
            return Err(anyhow!("invalid workspace_id"));
        }
        validate_image_digest(&self.cfg.worker_image_digest)?;

        let cpu_millis = if spec.cpu_millis == 0 { 1000 } else { spec.cpu_millis };
        let memory_bytes = if spec.memory_bytes == 0 { 1024 * 1024 * 1024 } else { spec.memory_bytes };
        let pids_limit = if spec.pids_limit == 0 { 128 } else { spec.pids_limit };
        if cpu_millis < 100 || cpu_millis > self.cfg.docker_cpu_max_millis {
            return Err(anyhow!("cpu_millis must be between 100 and {}", self.cfg.docker_cpu_max_millis));
        }
        if memory_bytes < 128 * 1024 * 1024 || memory_bytes > self.cfg.docker_memory_max_bytes {
            return Err(anyhow!("memory_bytes must be between 128 MiB and {}", self.cfg.docker_memory_max_bytes));
        }
        if pids_limit < 16 || pids_limit > self.cfg.docker_pids_max {
            return Err(anyhow!("pids_limit must be between 16 and {}", self.cfg.docker_pids_max));
        }

        {
            let records = self.records.lock().map_err(|_| anyhow!("Docker state lock poisoned"))?;
            if records.values().any(|r| r.workspace_id == spec.workspace_id) {
                return Err(anyhow!("workspace_id is already reserved by a Worker; destroy the old Worker before reuse"));
            }
        }

        let workspace_root = prepare_workspace(&self.cfg.workspace_base, &spec.workspace_id)?;
        let id = Uuid::new_v4().simple().to_string();
        let docker_name = format!("light-agent-worker-{}", &id[..16]);

        // Never pull arbitrary images from a task request. The operator pins and preloads
        // one immutable worker image; a missing image is an explicit deployment error.
        self.run_docker(
            vec!["image".into(), "inspect".into(), self.cfg.worker_image_digest.clone()],
            Duration::from_secs(15),
        ).await.context("pinned Worker image is not available locally; preload it before accepting tasks")?;

        let args = build_create_args(
            &docker_name, &id, &spec.worker_id, &spec.workspace_id,
            &workspace_root, &self.cfg.worker_image_digest,
            cpu_millis, memory_bytes, pids_limit,
        );
        let output = self.run_docker(args, Duration::from_secs(30)).await?;
        let docker_id = String::from_utf8_lossy(&output).trim().to_string();
        if !valid_docker_id(&docker_id) {
            return Err(anyhow!("Docker returned an invalid container ID"));
        }

        let record = ContainerRecord {
            container_id: id.clone(),
            docker_id: docker_id.clone(),
            docker_name,
            worker_id: spec.worker_id,
            workspace_id: spec.workspace_id,
            workspace_root: workspace_root.to_string_lossy().into_owned(),
            image_digest: self.cfg.worker_image_digest.clone(),
            state: "CREATED".into(),
            cpu_millis,
            memory_bytes,
            pids_limit,
            last_error: String::new(),
            created_at: now(),
            updated_at: now(),
        };
        self.records.lock().map_err(|_| anyhow!("Docker state lock poisoned"))?
            .insert(id, record.clone());
        if let Err(err) = self.persist() {
            let _ = self.run_docker(
                vec!["rm".into(), "--force".into(), docker_id],
                Duration::from_secs(10),
            ).await;
            return Err(err);
        }
        Ok(record)
    }

    pub fn get(&self, id: &str) -> Result<ContainerRecord> {
        self.records.lock().map_err(|_| anyhow!("Docker state lock poisoned"))?
            .get(id).cloned().ok_or_else(|| anyhow!("Worker container not found"))
    }

    pub async fn start(&self, id: &str) -> Result<ContainerRecord> {
        let lock = self.lock_for(id).await;
        let _guard = lock.lock().await;
        let mut record = self.get(id)?;
        if record.state == "RUNNING" { return Ok(record); }
        if !matches!(record.state.as_str(), "CREATED" | "STOPPED") {
            return Err(anyhow!("container cannot start from state {}", record.state));
        }
        record.state = "STARTING".into();
        record.updated_at = now();
        self.put_record(record.clone())?;

        if let Err(err) = self.run_docker(
            vec!["start".into(), record.docker_id.clone()],
            Duration::from_secs(20),
        ).await {
            record.state = "FAILED".into();
            record.last_error = format!("{err:#}");
            record.updated_at = now();
            let _ = self.put_record(record);
            return Err(err);
        }
        let status = match self.inspect_status(&record.docker_id).await {
            Ok(status) => status,
            Err(err) => {
                record.state = "FAILED".into();
                record.last_error = format!("container started but status inspection failed: {err:#}");
                record.updated_at = now();
                let _ = self.put_record(record.clone());
                return Err(err);
            }
        };
        if status != "running" {
            record.state = "FAILED".into();
            record.last_error = format!("Docker container entered unexpected state: {status}");
            record.updated_at = now();
            self.put_record(record.clone())?;
            return Err(anyhow!(record.last_error));
        }
        record.state = "RUNNING".into();
        record.last_error.clear();
        record.updated_at = now();
        self.put_record(record.clone())?;
        Ok(record)
    }

    pub async fn exec(&self, id: &str, req: ContainerExecRequest) -> Result<Value> {
        let lock = self.exec_lock_for(id).await;
        let _guard = lock.lock().await;
        let record = self.get(id)?;
        if record.state != "RUNNING" {
            return Err(anyhow!("Worker container is not RUNNING"));
        }
        validate_exec_request(&req, self.cfg.docker_timeout_max_ms)?;
        if req.memory_bytes > record.memory_bytes ||
            req.cpu_quota_micros > u64::from(record.cpu_millis) * 100 ||
            req.max_pids > record.pids_limit {
            return Err(anyhow!("exec resource request exceeds Worker container limits"));
        }
        let payload = serde_json::to_vec(&json!({
            "op": "exec",
            "request": {
                "executable": req.executable,
                "argv": req.argv,
                "cwd": req.cwd,
                "timeout_ms": req.timeout_ms,
                "memory_bytes": req.memory_bytes,
                "max_output_bytes": req.max_output_bytes,
                "cpu_quota_micros": req.cpu_quota_micros,
                "max_pids": req.max_pids,
                "network": false
            }
        }))?;

        let args = vec![
            "exec".into(), "-i".into(),
            "--user".into(), format!("{WORKER_UID}:{WORKER_GID}"),
            "--workdir".into(), "/workspace".into(),
            "--env".into(), "COSEXECD_CONTAINER_MODE=1".into(),
            "--env".into(), format!("COSEXECD_UID={WORKER_UID}").into(),
            "--env".into(), format!("COSEXECD_GID={WORKER_GID}").into(),
            record.docker_id.clone(),
            "/usr/local/bin/cos-execd".into(),
            "--stdio".into(),
        ];
        let output = self.run_docker_with_input(
            args, payload, Duration::from_millis(req.timeout_ms.saturating_add(15_000)),
        ).await?;
        let response: Value = serde_json::from_slice(&output)
            .context("cos-execd returned invalid JSON from Docker exec")?;
        if response.get("ok").and_then(Value::as_bool) != Some(true) {
            return Err(anyhow!("Worker execution failed: {}", response.get("error").and_then(Value::as_str).unwrap_or("unknown error")));
        }
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    }

    pub async fn stop(&self, id: &str) -> Result<ContainerRecord> {
        let lock = self.lock_for(id).await;
        let _guard = lock.lock().await;
        let mut record = self.get(id)?;
        if matches!(record.state.as_str(), "STOPPED" | "CREATED" | "EXITED") {
            return Ok(record);
        }
        record.state = "STOPPING".into();
        record.updated_at = now();
        self.put_record(record.clone())?;
        if let Err(err) = self.run_docker(
            vec!["stop".into(), "--time".into(), "5".into(), record.docker_id.clone()],
            Duration::from_secs(15),
        ).await {
            record.state = "FAILED".into();
            record.last_error = format!("{err:#}");
            record.updated_at = now();
            let _ = self.put_record(record);
            return Err(err);
        }
        record.state = "STOPPED".into();
        record.last_error.clear();
        record.updated_at = now();
        self.put_record(record.clone())?;
        Ok(record)
    }

    pub async fn destroy(&self, id: &str) -> Result<()> {
        let lock = self.lock_for(id).await;
        let _guard = lock.lock().await;
        let record = self.get(id)?;
        if let Err(err) = self.run_docker(
            vec!["rm".into(), "--force".into(), record.docker_id.clone()],
            Duration::from_secs(15),
        ).await {
            let message = format!("{err:#}");
            if !message.contains("No such container") && !message.contains("No such object") {
                return Err(err).context("remove Docker Worker container");
            }
        }
        self.records.lock().map_err(|_| anyhow!("Docker state lock poisoned"))?.remove(id);
        self.persist()?;
        drop(_guard);
        self.container_locks.lock().await.remove(id);
        self.exec_locks.lock().await.remove(id);
        Ok(())
    }

    pub async fn recover_on_startup(&self) -> Result<()> {
        let _guard = self.operation_lock.lock().await;
        let records: Vec<ContainerRecord> = self.records.lock()
            .map_err(|_| anyhow!("Docker state lock poisoned"))?
            .values().cloned().collect();
        for mut record in records {
            // A sandbox-manager restart invalidates every Worker. Do not start the
            // API if a previously managed container cannot be proven terminated.
            if let Err(err) = self.run_docker(
                vec!["rm".into(), "--force".into(), record.docker_id.clone()],
                Duration::from_secs(15),
            ).await {
                let message = format!("{err:#}");
                if !message.contains("No such container") && !message.contains("No such object") {
                    record.state = "FAILED".into();
                    record.last_error = format!("cleanup pending; refusing startup until Docker is reachable: {message}");
                    record.updated_at = now();
                    self.put_record(record)?;
                    return Err(anyhow!("cannot safely recover Worker containers: {message}"));
                }
            }
            record.state = "FAILED".into();
            record.last_error = "sandbox manager restarted; Worker was terminated and must be recreated".into();
            record.updated_at = now();
            self.put_record(record)?;
        }
        Ok(())
    }

    pub async fn stop_all(&self) {
        let ids: Vec<String> = self.records.lock()
            .map(|m| m.values().filter(|r| r.state == "RUNNING").map(|r| r.container_id.clone()).collect())
            .unwrap_or_default();
        for id in ids {
            let _ = self.stop(&id).await;
        }
    }

    async fn inspect_status(&self, docker_id: &str) -> Result<String> {
        let output = self.run_docker(
            vec!["inspect".into(), "--format".into(), "{{.State.Status}}".into(), docker_id.into()],
            Duration::from_secs(10),
        ).await?;
        Ok(String::from_utf8_lossy(&output).trim().to_string())
    }

    async fn run_docker(&self, args: Vec<String>, limit: Duration) -> Result<Vec<u8>> {
        let mut command = self.docker_command(&args);
        let output = timeout(limit, command.output()).await
            .context("Docker CLI operation timed out")??;
        if !output.status.success() {
            return Err(anyhow!("Docker CLI failed ({:?}): {}", output.status,
                bounded_text(&output.stderr, 4096)));
        }
        Ok(output.stdout)
    }

    async fn run_docker_with_input(&self, args: Vec<String>, input: Vec<u8>, limit: Duration) -> Result<Vec<u8>> {
        let mut command = self.docker_command(&args);
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        let mut child = command.spawn().context("spawn Docker exec")?;
        let mut stdin = child.stdin.take().ok_or_else(|| anyhow!("Docker exec stdin unavailable"))?;
        stdin.write_all(&input).await.context("write Worker exec request")?;
        stdin.write_all(b"\n").await?;
        drop(stdin);
        let output = timeout(limit, child.wait_with_output()).await
            .context("Worker execution exceeded outer timeout")??;
        if !output.status.success() {
            return Err(anyhow!("Docker exec failed ({:?}): {}", output.status,
                bounded_text(&output.stderr, 4096)));
        }
        if output.stdout.len() > (12 * MAX_EXEC_OUTPUT) + 32 * 1024 {
            return Err(anyhow!("Worker response exceeded output envelope"));
        }
        Ok(output.stdout)
    }

    fn docker_command(&self, args: &[String]) -> Command {
        let mut command = Command::new(&self.cfg.docker_bin);
        command.arg("--host")
            .arg(format!("unix://{}", self.cfg.docker_socket.display()))
            .args(args)
            .env_clear()
            .env("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            .kill_on_drop(true);
        command
    }

    async fn lock_for(&self, id: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut locks = self.container_locks.lock().await;
        locks.entry(id.to_string())
            .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
            .clone()
    }

    async fn exec_lock_for(&self, id: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut locks = self.exec_locks.lock().await;
        locks.entry(id.to_string())
            .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
            .clone()
    }

    fn put_record(&self, record: ContainerRecord) -> Result<()> {
        self.records.lock().map_err(|_| anyhow!("Docker state lock poisoned"))?
            .insert(record.container_id.clone(), record);
        self.persist()
    }
}

pub fn validate_image_digest(image: &str) -> Result<()> {
    let (name, digest) = image.rsplit_once("@sha256:")
        .ok_or_else(|| anyhow!("Worker image must be pinned by @sha256 digest"))?;
    if name.is_empty() || name.len() > 255 ||
        !name.bytes().all(|b| b.is_ascii_alphanumeric() || b".-/_:".contains(&b)) ||
        !name.as_bytes()[0].is_ascii_alphanumeric() {
        return Err(anyhow!("invalid Worker image reference"));
    }
    if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(anyhow!("Worker image SHA-256 digest must contain exactly 64 hexadecimal characters"));
    }
    Ok(())
}

fn validate_worker_id(worker_id: &str) -> Result<()> {
    if worker_id.is_empty() || worker_id.len() > 128 ||
        !worker_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.') {
        return Err(anyhow!("worker_id must be 1..128 ASCII letters, digits, '.', '_' or '-'"));
    }
    Ok(())
}

fn validate_exec_request(req: &ContainerExecRequest, max_timeout: u64) -> Result<()> {
    if !req.executable.starts_with('/') || req.executable.contains('\0') ||
        req.executable.len() > 4096 || req.argv.len() > 128 ||
        req.argv.iter().any(|s| s.len() > 8192 || s.contains('\0')) {
        return Err(anyhow!("invalid executable or argv"));
    }
    if req.network {
        return Err(anyhow!("network access is disabled for Docker Workers"));
    }
    if req.timeout_ms < 100 || req.timeout_ms > max_timeout.min(MAX_EXEC_TIMEOUT_MS) {
        return Err(anyhow!("timeout_ms is outside the configured range"));
    }
    if req.max_output_bytes == 0 || req.max_output_bytes > MAX_EXEC_OUTPUT {
        return Err(anyhow!("max_output_bytes must be between 1 and 1048576"));
    }
    if req.memory_bytes < 64 * 1024 * 1024 || req.memory_bytes > 2 * 1024 * 1024 * 1024 {
        return Err(anyhow!("memory_bytes is outside the supported range"));
    }
    if req.cwd.starts_with('/') || req.cwd.contains('\0') ||
        req.cwd.split('/').any(|p| p == "..") {
        return Err(anyhow!("cwd must be a relative path beneath /workspace"));
    }
    Ok(())
}

fn build_create_args(
    name: &str, id: &str, worker_id: &str, workspace_id: &str,
    workspace: &Path, image: &str, cpu_millis: u32, memory_bytes: u64, pids: u64,
) -> Vec<String> {
    let cpus = format!("{:.3}", cpu_millis as f64 / 1000.0);
    let mount = format!("type=bind,source={},target=/workspace", workspace.display());
    vec![
        "create".into(),
        "--name".into(), name.into(),
        "--label".into(), "com.light-agent.managed=true".into(),
        "--label".into(), format!("com.light-agent.container-id={id}"),
        "--label".into(), format!("com.light-agent.worker-id={worker_id}"),
        "--label".into(), format!("com.light-agent.workspace-id={workspace_id}"),
        "--runtime=runc".into(),
        "--init".into(),
        "--user".into(), format!("{WORKER_UID}:{WORKER_GID}"),
        "--read-only".into(),
        "--cap-drop=ALL".into(),
        "--security-opt=no-new-privileges:true".into(),
        "--pids-limit".into(), pids.to_string(),
        "--memory".into(), memory_bytes.to_string(),
        "--memory-swap".into(), memory_bytes.to_string(),
        "--cpus".into(), cpus,
        "--network=none".into(),
        "--ipc=private".into(),
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777".into(),
        "--workdir=/workspace".into(),
        "--mount".into(), mount,
        "--log-driver=local".into(),
        "--log-opt=max-size=10m".into(),
        "--log-opt=max-file=1".into(),
        "--ulimit=nofile=1024:1024".into(),
        "--ulimit=core=0:0".into(),
        "--stop-timeout=5".into(),
        "--restart=no".into(),
        "--entrypoint=/usr/bin/sleep".into(),
        image.into(),
        "infinity".into(),
    ]
}

fn prepare_workspace(base: &Path, workspace_id: &str) -> Result<PathBuf> {
    fs::create_dir_all(base)?;
    let base = fs::canonicalize(base)?;
    let requested = base.join(workspace_id);
    fs::create_dir_all(&requested)?;
    let root = fs::canonicalize(&requested)?;
    if !root.starts_with(&base) || root == base || root != requested {
        return Err(anyhow!("workspace escapes configured workspace base or is a symlink"));
    }
    if root.as_os_str().as_bytes().contains(&b',') {
        return Err(anyhow!("workspace path cannot contain a comma (Docker mount syntax)"));
    }
    let root_meta = fs::symlink_metadata(&root)?;
    if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
        return Err(anyhow!("workspace root must be a real directory"));
    }
    // The Worker runs as 65532:65532. Reject symlinks and special files before
    // changing ownership; never follow links into paths outside the workspace.
    for entry in WalkDir::new(&root).follow_links(false) {
        let entry = entry?;
        let meta = fs::symlink_metadata(entry.path())?;
        if meta.file_type().is_symlink() || !(meta.is_dir() || meta.is_file()) {
            return Err(anyhow!("workspace contains symlink or special file: {}", entry.path().display()));
        }
        let c_path = CString::new(entry.path().as_os_str().as_bytes())?;
        if unsafe { libc::chown(c_path.as_ptr(), WORKER_UID, WORKER_GID) } != 0 {
            return Err(std::io::Error::last_os_error())
                .with_context(|| format!("set Worker ownership on {}", entry.path().display()));
        }
        let mut permissions = meta.permissions();
        let mode = permissions.mode() & 0o777;
        permissions.set_mode(if meta.is_dir() { mode | 0o700 } else { mode | 0o600 });
        fs::set_permissions(entry.path(), permissions)?;
    }
    Ok(root)
}

fn valid_docker_id(id: &str) -> bool {
    (12..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_hexdigit())
}
fn bounded_text(bytes: &[u8], limit: usize) -> String {
    String::from_utf8_lossy(&bytes[..bytes.len().min(limit)]).into_owned()
}
fn now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requires_immutable_worker_image_digest() {
        assert!(validate_image_digest("registry.example/worker:latest").is_err());
        assert!(validate_image_digest("registry.example/worker@sha256:abcd").is_err());
        assert!(validate_image_digest("registry.example/worker;touch-pwned@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").is_err());
        assert!(validate_image_digest(&format!("registry.example/worker@sha256:{}", "a".repeat(64))).is_ok());
    }

    #[test]
    fn docker_create_has_required_security_flags() {
        let args = build_create_args(
            "worker-test", "abcdef", "worker-1", "workspace-1",
            Path::new("/var/lib/light-agent/workspaces/workspace-1"),
            &format!("worker@sha256:{}", "a".repeat(64)), 1000, 1024 * 1024 * 1024, 128,
        );
        for required in ["--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--network=none", "--ipc=private", "--runtime=runc", "--restart=no"] {
            assert!(args.iter().any(|a| a == required), "missing {required}");
        }
        assert!(!args.iter().any(|a| a == "--privileged"));
        assert!(!args.iter().any(|a| a.contains("docker.sock")));
        assert!(args.windows(2).any(|w| w[0] == "--memory" && w[1] == "1073741824"));
    }

    #[test]
    fn rejects_network_enablement_and_path_escape() {
        let req = ContainerExecRequest {
            executable: "/bin/true".into(), argv: vec![], cwd: "../host".into(),
            timeout_ms: 1000, memory_bytes: 64 * 1024 * 1024,
            max_output_bytes: 4096, cpu_quota_micros: 100000, max_pids: 64, network: false,
        };
        assert!(validate_exec_request(&req, MAX_EXEC_TIMEOUT_MS).is_err());
        let mut req = req;
        req.cwd = String::new();
        req.network = true;
        assert!(validate_exec_request(&req, MAX_EXEC_TIMEOUT_MS).is_err());
    }
}
