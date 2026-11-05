use anyhow::{anyhow, Result};
use std::{env, path::PathBuf};

#[derive(Clone,Debug)]
pub struct Config{
    pub listen_addr:String,pub api_token:String,pub state_path:PathBuf,pub jailer_bin:PathBuf,
    pub firecracker_bin:PathBuf,pub jailer_root:PathBuf,pub kernel_path:PathBuf,pub rootfs_path:PathBuf,
    pub workspace_base:PathBuf,pub jailer_uid:u32,pub jailer_gid:u32,pub memory_mib_max:u64,pub vcpu_max:u32,
}
impl Config{
    pub fn from_env()->Result<Self>{
        let listen_addr=get("LIGHT_AGENT_SANDBOX_LISTEN_ADDR","127.0.0.1:9090");
        let api_token=env::var("LIGHT_AGENT_SANDBOX_API_TOKEN").unwrap_or_default();
        let host=listen_addr.parse::<std::net::SocketAddr>().map_err(|e|anyhow!("invalid listen address: {e}"))?;
        if !host.ip().is_loopback()&&api_token.len()<32{return Err(anyhow!("non-loopback sandbox API requires a 32+ character API token"));}
        if api_token.is_empty(){return Err(anyhow!("LIGHT_AGENT_SANDBOX_API_TOKEN must be configured"));}
        Ok(Self{
            listen_addr,api_token,state_path:PathBuf::from(get("LIGHT_AGENT_SANDBOX_STATE","/var/lib/light-agent/sandbox/vms.json")),
            jailer_bin:PathBuf::from(get("JAILER_BIN","/usr/bin/jailer")),firecracker_bin:PathBuf::from(get("FIRECRACKER_BIN","/usr/bin/firecracker")),
            jailer_root:PathBuf::from(get("JAILER_CHROOT_BASE","/var/lib/light-agent/jailer")),
            kernel_path:PathBuf::from(get("FIRECRACKER_KERNEL","/var/lib/light-agent/images/vmlinux")),
            rootfs_path:PathBuf::from(get("FIRECRACKER_ROOTFS","/var/lib/light-agent/images/guest-rootfs.ext4")),
            workspace_base:PathBuf::from(get("LIGHT_AGENT_WORKSPACE_BASE","/var/lib/light-agent/workspaces")),
            jailer_uid:get("FIRECRACKER_UID","1000").parse()?,jailer_gid:get("FIRECRACKER_GID","1000").parse()?,
            memory_mib_max:get("FIRECRACKER_MEMORY_MIB_MAX","8192").parse()?,vcpu_max:get("FIRECRACKER_VCPU_MAX","8").parse()?,
        })
    }
}
fn get(key:&str,fallback:&str)->String{env::var(key).unwrap_or_else(|_|fallback.to_string())}
pub fn validate_workspace_id(id:&str)->bool{
    !id.is_empty()&&id.len()<=96&&id.chars().all(|c|c.is_ascii_alphanumeric()||c=='-'||c=='_')&&id!="."&&id!=".."
}
