use crate::protocol::{ExecRequest,ExecResponse};
use anyhow::{anyhow,Context,Result};
use std::{fs,io::Read,os::unix::process::CommandExt,process::{Command,Stdio},thread,time::{Duration,Instant}};

const MAX_TIMEOUT_MS:u64=300_000;const MAX_MEMORY:u64=2*1024*1024*1024;const MAX_OUTPUT:usize=4*1024*1024;
pub fn execute(req:ExecRequest)->Result<ExecResponse>{
    if !req.executable.starts_with('/')||req.executable.contains('\0'){return Err(anyhow!("executable must be an absolute path"));}
    if req.argv.len()>128||req.argv.iter().any(|s|s.len()>8192||s.contains('\0')){return Err(anyhow!("invalid argv"));}
    let timeout_ms=req.timeout_ms.clamp(100,MAX_TIMEOUT_MS);let memory=req.memory_bytes.clamp(64*1024*1024,MAX_MEMORY);
    let output_limit=req.max_output_bytes.clamp(1024,MAX_OUTPUT);let cpu=req.cpu_quota_micros.clamp(1000,1_000_000);let pids=req.max_pids.clamp(16,256);
    let cwd=if req.cwd.is_empty(){"/workspace".to_string()}else{if req.cwd.starts_with('/')||req.cwd.split('/').any(|p|p=="..")||req.cwd.contains('\0'){return Err(anyhow!("cwd must be a safe relative path"));}format!("/workspace/{}",req.cwd)};
    let container_mode=std::env::var("COSEXECD_CONTAINER_MODE").ok().as_deref()==Some("1");
    if container_mode && req.network { return Err(anyhow!("network access is disabled in Docker Worker mode")); }
    let cgroup=if container_mode { None } else { prepare_cgroup(memory,pids,cpu)? };
    let network=req.network;
    let uid=std::env::var("COSEXECD_UID").unwrap_or_else(|_|"10000".into()).parse::<u32>().unwrap_or(10000);
    let gid=std::env::var("COSEXECD_GID").unwrap_or_else(|_|"10000".into()).parse::<u32>().unwrap_or(10000);
    let start=Instant::now();let mut command=Command::new(&req.executable);
    command.args(&req.argv).current_dir(cwd).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    command.env_clear().env("PATH","/usr/local/bin:/usr/bin:/bin").env("HOME","/tmp").env("TMPDIR","/tmp").env("LANG","C.UTF-8");
    unsafe{
        command.process_group(0);
        command.pre_exec(move||{
            set_limit(libc::RLIMIT_CORE,0)?;set_limit(libc::RLIMIT_FSIZE,64*1024*1024)?;set_limit(libc::RLIMIT_NOFILE,128)?;
            set_limit(libc::RLIMIT_NPROC,256)?;set_limit(libc::RLIMIT_AS,memory)?;
            set_limit(libc::RLIMIT_CPU,(timeout_ms.saturating_add(999)/1000).saturating_add(1))?;
            if !container_mode {
                if libc::unshare(libc::CLONE_NEWNS|libc::CLONE_NEWIPC|libc::CLONE_NEWUTS)!=0{return Err(std::io::Error::last_os_error());}
                if libc::mount(std::ptr::null(),b"/\0".as_ptr() as *const i8,std::ptr::null(),(libc::MS_REC|libc::MS_PRIVATE) as libc::c_ulong,std::ptr::null())!=0{return Err(std::io::Error::last_os_error());}
                if !network&&libc::unshare(libc::CLONE_NEWNET)!=0{return Err(std::io::Error::last_os_error());}
            }
            if libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0)!=0{return Err(std::io::Error::last_os_error());}
            install_seccomp()?;
            if !container_mode && (libc::setgroups(0,std::ptr::null())!=0||libc::setgid(gid)!=0||libc::setuid(uid)!=0){return Err(std::io::Error::last_os_error());}
            Ok(())
        });
    }
    let mut child=command.spawn().with_context(||format!("spawn {}",req.executable))?;
    let pid=child.id() as i32;
    if let Some(path)=&cgroup{if let Err(e)=fs::write(path.join("cgroup.procs"),pid.to_string()){unsafe{libc::kill(-pid,libc::SIGKILL);}let _=child.wait();let _=fs::remove_dir(path);return Err(e).context("attach task to cgroup");}}
    let stdout=child.stdout.take().unwrap();let stderr=child.stderr.take().unwrap();
    let out_thread=thread::spawn(move||read_bounded(stdout,output_limit));let err_thread=thread::spawn(move||read_bounded(stderr,output_limit));
    let deadline=start+Duration::from_millis(timeout_ms);let mut timed_out=false;
    let status=loop{if let Some(status)=child.try_wait()?{break Some(status);}if Instant::now()>=deadline{timed_out=true;unsafe{libc::kill(-pid,libc::SIGKILL);}let _=child.kill();break child.wait().ok();}thread::sleep(Duration::from_millis(20));};
    let stdout=out_thread.join().unwrap_or_default();let stderr=err_thread.join().unwrap_or_default();
    if let Some(path)=cgroup{let _=fs::remove_dir(path);}
    Ok(ExecResponse{exit_code:status.and_then(|s|s.code()),timed_out,stdout:String::from_utf8_lossy(&stdout).into_owned(),stderr:String::from_utf8_lossy(&stderr).into_owned(),duration_ms:start.elapsed().as_millis()})
}
fn prepare_cgroup(memory:u64,pids:u64,cpu:u64)->Result<Option<std::path::PathBuf>>{
    let root=match std::env::var("COSEXECD_CGROUP_ROOT"){Ok(v)if !v.is_empty()=>std::path::PathBuf::from(v),_=>return Ok(None)};
    let name=format!("task-{}-{}",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos());
    let path=root.join(name);fs::create_dir(&path).context("create task cgroup")?;
    let setup=(||->std::io::Result<()>{fs::write(path.join("memory.max"),memory.to_string())?;fs::write(path.join("pids.max"),pids.to_string())?;fs::write(path.join("cpu.max"),format!("{cpu} 100000"))?;Ok(())})();
    if let Err(e)=setup{let _=fs::remove_dir(&path);return Err(e).context("configure task cgroup");}
    Ok(Some(path))
}
fn read_bounded<R:Read>(mut reader:R,limit:usize)->Vec<u8>{let mut out=Vec::new();let mut buf=[0u8;8192];loop{match reader.read(&mut buf){Ok(0)|Err(_)=>break,Ok(n)=>{let keep=limit.saturating_sub(out.len()).min(n);out.extend_from_slice(&buf[..keep]);}}}out}
fn set_limit(resource:u32,value:u64)->std::io::Result<()>{let lim=libc::rlimit{rlim_cur:value as libc::rlim_t,rlim_max:value as libc::rlim_t};if unsafe{libc::setrlimit(resource,&lim)}!=0{Err(std::io::Error::last_os_error())}else{Ok(())}}
#[cfg(target_os="linux")]
fn install_seccomp()->std::io::Result<()>{
    let denied:&[i64]=&[libc::SYS_mount,libc::SYS_umount2,libc::SYS_pivot_root,libc::SYS_ptrace,libc::SYS_bpf,libc::SYS_kexec_load,libc::SYS_init_module,libc::SYS_finit_module,libc::SYS_delete_module,libc::SYS_reboot,libc::SYS_swapon,libc::SYS_swapoff,libc::SYS_open_by_handle_at,libc::SYS_userfaultfd,libc::SYS_perf_event_open,libc::SYS_keyctl,libc::SYS_add_key,libc::SYS_request_key,libc::SYS_setns];
    let mut filter=Vec::<libc::sock_filter>::with_capacity(denied.len()*2+2);
    filter.push(libc::sock_filter{code:0x20,jt:0,jf:0,k:0});
    for nr in denied{filter.push(libc::sock_filter{code:0x15,jt:0,jf:1,k:*nr as u32});filter.push(libc::sock_filter{code:0x06,jt:0,jf:0,k:0x00050000|(libc::EPERM as u32)});}
    filter.push(libc::sock_filter{code:0x06,jt:0,jf:0,k:0x7fff0000});
    let program=libc::sock_fprog{len:filter.len() as u16,filter:filter.as_mut_ptr()};
    if unsafe{libc::prctl(libc::PR_SET_SECCOMP,libc::SECCOMP_MODE_FILTER,&program)}!=0{Err(std::io::Error::last_os_error())}else{Ok(())}
}
