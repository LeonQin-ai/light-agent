use serde::{Deserialize,Serialize};
#[derive(Debug,Deserialize)]
pub struct ExecRequest{
    pub executable:String,
    #[serde(default)]pub argv:Vec<String>,
    #[serde(default)]pub cwd:String,
    #[serde(default="default_timeout")]pub timeout_ms:u64,
    #[serde(default="default_memory")]pub memory_bytes:u64,
    #[serde(default="default_output")]pub max_output_bytes:usize,
    #[serde(default="default_cpu")]pub cpu_quota_micros:u64,
    #[serde(default="default_pids")]pub max_pids:u64,
    #[serde(default)]pub network:bool,
}
fn default_timeout()->u64{15000}fn default_memory()->u64{512*1024*1024}fn default_output()->usize{1024*1024}fn default_cpu()->u64{100000}fn default_pids()->u64{64}
#[derive(Debug,Serialize)]
pub struct ExecResponse{pub exit_code:Option<i32>,pub timed_out:bool,pub stdout:String,pub stderr:String,pub duration_ms:u128}
