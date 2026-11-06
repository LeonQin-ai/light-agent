use crate::{config::Config, docker::DockerProvider, firecracker, vsock};
use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, fs, path::Path, process::Stdio, sync::{Arc, Mutex}};
use tokio::process::{Child, Command};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VmRecord {
    pub vm_id: String,
    pub worker_id: String,
    pub image_digest: String,
    pub state: String,
    pub kernel_path: String,
    pub rootfs_path: String,
    pub workspace_id: String,
    pub workspace_root: String,
    pub vcpu_count: u32,
    pub memory_mib: u64,
    pub guest_cid: u32,
    pub api_socket: String,
    pub vsock_path: String,
    pub jailer_pid: Option<u32>,
    pub last_error: String,
    pub created_at: String,
    pub updated_at: String,
}
#[derive(Clone)]
pub struct AppState {
    pub cfg: Config,
    pub docker: DockerProvider,
    records: Arc<Mutex<HashMap<String, VmRecord>>>,
    children: Arc<tokio::sync::Mutex<HashMap<String, Child>>>,
}
impl AppState {
    pub fn load(cfg: Config) -> Result<Self> {
        fs::create_dir_all(cfg.state_path.parent().unwrap_or(Path::new(".")))?;
        fs::create_dir_all(&cfg.jailer_root)?;
        fs::create_dir_all(&cfg.workspace_base)?;
        let mut records: HashMap<String, VmRecord> = if cfg.state_path.exists() {
            serde_json::from_slice(&fs::read(&cfg.state_path)?).context("parse VM state file")?
        } else { HashMap::new() };
        for (id, record) in records.iter_mut() {
            if record.state == "RUNNING" || record.state == "STARTING" || record.state == "STOPPING" {
                terminate_matching_jailer(record.jailer_pid, id);
                record.state = "FAILED".into();
                record.last_error = "sandbox daemon restarted; VM was terminated and must be recreated".into();
                record.updated_at = now();
            }
        }
        let docker = DockerProvider::load(cfg.clone())?;
        let state = Self { cfg, docker, records: Arc::new(Mutex::new(records)), children: Arc::new(tokio::sync::Mutex::new(HashMap::new())) };
        state.persist()?;
        Ok(state)
    }
    fn persist(&self) -> Result<()> {
        let snapshot = self.records.lock().map_err(|_| anyhow!("VM state lock poisoned"))?.clone();
        let bytes = serde_json::to_vec_pretty(&snapshot)?;
        let path = &self.cfg.state_path;
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, bytes)?;
        fs::rename(&tmp, path).with_context(|| format!("replace state file {}", path.display()))?;
        Ok(())
    }
    pub fn create_vm(&self, worker_id:String, workspace_id:String, image_digest:String, vcpu_count:u32, memory_mib:u64)->Result<VmRecord>{
        if worker_id.is_empty()||worker_id.len()>128{return Err(anyhow!("worker_id is required and must be <=128 bytes"));}
        if !crate::config::validate_workspace_id(&workspace_id){return Err(anyhow!("invalid workspace_id"));}
        if !self.cfg.kernel_path.is_file()||!self.cfg.rootfs_path.is_file(){return Err(anyhow!("configured Firecracker kernel/rootfs image does not exist"));}
        let workspace=self.cfg.workspace_base.join(&workspace_id);fs::create_dir_all(&workspace)?;
        let workspace=fs::canonicalize(workspace)?;let base=fs::canonicalize(&self.cfg.workspace_base)?;
        if !workspace.starts_with(&base){return Err(anyhow!("workspace escapes configured base"));}
        let vcpu_count=if vcpu_count==0{2}else{vcpu_count};
        let memory_mib=if memory_mib==0{1024}else{memory_mib};
        if vcpu_count==0||vcpu_count>self.cfg.vcpu_max{return Err(anyhow!("vcpu_count exceeds configured limit"));}
        if memory_mib<128||memory_mib>self.cfg.memory_mib_max{return Err(anyhow!("memory_mib exceeds configured limit"));}
        let vm_id=Uuid::new_v4().simple().to_string();
        let cid=(u32::from_be_bytes([vm_id.as_bytes()[0],vm_id.as_bytes()[1],vm_id.as_bytes()[2],vm_id.as_bytes()[3]])&0x7fff_ffff).max(3);
        let jail_root=self.cfg.jailer_root.join(&vm_id).join("root");
        let record=VmRecord{
            vm_id:vm_id.clone(),worker_id,image_digest,state:"CREATED".into(),
            kernel_path:self.cfg.kernel_path.to_string_lossy().into_owned(),rootfs_path:self.cfg.rootfs_path.to_string_lossy().into_owned(),
            workspace_id,workspace_root:workspace.to_string_lossy().into_owned(),vcpu_count,memory_mib,guest_cid:cid,
            api_socket:jail_root.join("run/fc.sock").to_string_lossy().into_owned(),
            vsock_path:jail_root.join("run/vsock.sock").to_string_lossy().into_owned(),
            jailer_pid:None,last_error:String::new(),created_at:now(),updated_at:now()
        };
        self.records.lock().map_err(|_|anyhow!("VM state lock poisoned"))?.insert(vm_id,record.clone());
        self.persist()?;Ok(record)
    }
    pub fn get_vm(&self,id:&str)->Result<VmRecord>{
        self.records.lock().map_err(|_|anyhow!("VM state lock poisoned"))?.get(id).cloned().ok_or_else(||anyhow!("VM not found"))
    }
    pub async fn start_vm(&self,id:&str)->Result<VmRecord>{
        let mut record=self.get_vm(id)?;
        if record.state=="RUNNING"{return Ok(record);}
        if record.state=="STARTING"||record.state=="STOPPING"{return Err(anyhow!("VM lifecycle transition already in progress"));}
        if record.state=="FAILED"{return Err(anyhow!("failed VM must be destroyed and recreated"));}
        record.state="STARTING".into();record.last_error.clear();record.updated_at=now();self.put_record(record.clone())?;
        if let Err(err)=self.start_process(&record).await{
            record.state="FAILED".into();record.last_error=format!("{err:#}");record.updated_at=now();
            let _=self.stop_process(&record.vm_id).await;let _=self.put_record(record.clone());return Err(err);
        }
        record.state="RUNNING".into();record.jailer_pid=self.children.lock().await.get(&record.vm_id).and_then(|c|c.id());record.updated_at=now();
        self.put_record(record.clone())?;Ok(record)
    }
    async fn start_process(&self,record:&VmRecord)->Result<()>{
        if !Path::new("/dev/kvm").exists(){return Err(anyhow!("/dev/kvm is unavailable"));}
        let jail_dir=self.cfg.jailer_root.join(&record.vm_id);let root=jail_dir.join("root");let run=root.join("run");fs::create_dir_all(&run)?;
        fs::copy(&self.cfg.kernel_path,root.join("vmlinux")).context("copy kernel into jail")?;
        fs::copy(&self.cfg.rootfs_path,root.join("rootfs.ext4")).context("copy rootfs into jail")?;
        let _=fs::remove_file(&record.api_socket);let _=fs::remove_file(&record.vsock_path);
        let mut command=Command::new(&self.cfg.jailer_bin);
        command.arg("--id").arg(&record.vm_id).arg("--exec-file").arg(&self.cfg.firecracker_bin)
            .arg("--uid").arg(self.cfg.jailer_uid.to_string()).arg("--gid").arg(self.cfg.jailer_gid.to_string())
            .arg("--chroot-base-dir").arg(&self.cfg.jailer_root).arg("--").arg("--api-sock").arg("/run/fc.sock")
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).kill_on_drop(false);
        let child=command.spawn().with_context(||format!("spawn jailer {}",self.cfg.jailer_bin.display()))?;
        self.children.lock().await.insert(record.vm_id.clone(),child);
        let deadline=tokio::time::Instant::now()+std::time::Duration::from_secs(15);
        while tokio::time::Instant::now()<deadline{if Path::new(&record.api_socket).exists(){break;}tokio::time::sleep(std::time::Duration::from_millis(50)).await;}
        if !Path::new(&record.api_socket).exists(){return Err(anyhow!("Firecracker API socket did not appear"));}
        firecracker::configure(&record.api_socket,record.guest_cid,record.vcpu_count,record.memory_mib).await?;
        let health_deadline=tokio::time::Instant::now()+std::time::Duration::from_secs(45);
        loop{
            if tokio::time::Instant::now()>=health_deadline{return Err(anyhow!("Guest Agent did not become ready on vsock port 5000"));}
            if let Ok(reply)=vsock::request(&record.vsock_path,5000,serde_json::json!({"op":"health"})).await{
                if reply.get("ok").and_then(|v|v.as_bool())==Some(true)
                && reply.get("result").and_then(|v|v.get("cognitive_ready")).and_then(|v|v.as_bool())==Some(true) { break; }
            }
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        }
        Ok(())
    }
    pub async fn stop_vm(&self,id:&str)->Result<VmRecord>{
        let mut record=self.get_vm(id)?;
        if record.state=="STOPPED"||record.state=="CREATED"{return Ok(record);}
        record.state="STOPPING".into();record.updated_at=now();self.put_record(record.clone())?;
        self.stop_process(id).await?;
        record.state="STOPPED".into();record.jailer_pid=None;record.updated_at=now();self.put_record(record.clone())?;Ok(record)
    }
    async fn stop_process(&self,id:&str)->Result<()>{
        if let Some(mut child)=self.children.lock().await.remove(id){child.kill().await.context("terminate jailer")?;let _=child.wait().await;}
        Ok(())
    }
    pub async fn destroy_vm(&self,id:&str)->Result<()>{
        let _record=self.get_vm(id)?;self.stop_process(id).await?;
        let jail_dir=self.cfg.jailer_root.join(id);
        if jail_dir.exists(){let base=fs::canonicalize(&self.cfg.jailer_root)?;let target=fs::canonicalize(&jail_dir)?;if !target.starts_with(&base){return Err(anyhow!("refusing to remove path outside jailer root"));}fs::remove_dir_all(target)?;}
        self.records.lock().map_err(|_|anyhow!("VM state lock poisoned"))?.remove(id);self.persist()
    }
    pub async fn guest_request(&self,id:&str,port:u32,payload:serde_json::Value)->Result<serde_json::Value>{
        let record=self.get_vm(id)?;if record.state!="RUNNING"{return Err(anyhow!("VM is not RUNNING"));}
        vsock::request(&record.vsock_path,port,payload).await
    }
    pub async fn stop_all(&self){let ids:Vec<String>=self.records.lock().map(|m|m.keys().cloned().collect()).unwrap_or_default();for id in ids{let _=self.stop_process(&id).await;} self.docker.stop_all().await;}
    fn put_record(&self,record:VmRecord)->Result<()>{self.records.lock().map_err(|_|anyhow!("VM state lock poisoned"))?.insert(record.vm_id.clone(),record);self.persist()}
}
fn now()->String{use std::time::{SystemTime,UNIX_EPOCH};SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs().to_string()}
fn terminate_matching_jailer(pid:Option<u32>,id:&str){
    let Some(pid)=pid else{return;};let cmdline=fs::read(format!("/proc/{pid}/cmdline")).unwrap_or_default();let text=String::from_utf8_lossy(&cmdline);
    if text.contains("jailer")&&text.contains(id){unsafe{libc::kill(pid as i32,libc::SIGTERM);}}
}
