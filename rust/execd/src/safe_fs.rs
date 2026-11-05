use anyhow::{anyhow, Result};
use base64::Engine;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{fs, io::{Read, Write}, os::fd::{AsRawFd, FromRawFd}, path::Path};
use walkdir::WalkDir;

#[derive(Deserialize)]
struct ManifestEntry { path: String, sha256: String }

pub fn write_workspace(relative:&str,content_b64:&str)->Result<()>{
    validate(relative)?;
    let bytes=base64::engine::general_purpose::STANDARD.decode(content_b64)?;
    if bytes.len()>4*1024*1024{return Err(anyhow!("workspace file exceeds 4 MiB"));}
    let root=open_dir("/workspace")?;
    let parts:Vec<&str>=relative.split('/').collect();
    let(filename,dirs)=parts.split_last().ok_or_else(||anyhow!("invalid path"))?;
    let parent=open_dir_chain(root.as_raw_fd(),dirs)?;
    let temp=format!(".cos-{}-{}.tmp",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos());
    let temp_c=std::ffi::CString::new(temp.as_str())?;
    let how=OpenHow{flags:(libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_CLOEXEC) as u64,mode:0o640,resolve:RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS|RESOLVE_NO_SYMLINKS};
    let fd=unsafe{libc::syscall(libc::SYS_openat2,parent.as_raw_fd(),temp_c.as_ptr(),&how,std::mem::size_of::<OpenHow>()) as i32};
    if fd<0{return Err(std::io::Error::last_os_error().into());}
    let mut file=unsafe{fs::File::from_raw_fd(fd)};
    file.write_all(&bytes)?;file.sync_all()?;
    let uid=std::env::var("COSEXECD_UID").unwrap_or_else(|_|"10000".into()).parse::<u32>().unwrap_or(10000);
    let gid=std::env::var("COSEXECD_GID").unwrap_or_else(|_|"10000".into()).parse::<u32>().unwrap_or(10000);
    if unsafe{libc::fchown(file.as_raw_fd(),uid,gid)}<0{return Err(std::io::Error::last_os_error().into());}
    let name_c=std::ffi::CString::new(*filename)?;
    let rc=unsafe{libc::syscall(libc::SYS_renameat2,parent.as_raw_fd(),temp_c.as_ptr(),parent.as_raw_fd(),name_c.as_ptr(),0u32)};
    if rc<0{let e=std::io::Error::last_os_error();let _=unsafe{libc::unlinkat(parent.as_raw_fd(),temp_c.as_ptr(),0)};return Err(e.into());}
    Ok(())
}

pub fn store_manifest(manifest_json:&str)->Result<()>{
    let entries:Vec<ManifestEntry>=serde_json::from_str(manifest_json)?;
    if entries.len()>5000{return Err(anyhow!("manifest exceeds 5000 entries"));}
    for entry in &entries{validate(&entry.path)?;if entry.sha256.len()!=64{return Err(anyhow!("invalid manifest hash"));}}
    fs::create_dir_all("/var/lib/light-agent")?;
    let tmp="/var/lib/light-agent/base-manifest.json.tmp";
    fs::write(tmp,manifest_json)?;fs::rename(tmp,"/var/lib/light-agent/base-manifest.json")?;Ok(())
}

pub fn diff_workspace()->Result<serde_json::Value>{
    let manifest_text=fs::read_to_string("/var/lib/light-agent/base-manifest.json").unwrap_or_else(|_|"[]".into());
    let base:Vec<ManifestEntry>=serde_json::from_str(&manifest_text)?;
    let mut base_map=std::collections::HashMap::new();for entry in base{base_map.insert(entry.path,entry.sha256);}
    let root=Path::new("/workspace");let mut seen=std::collections::HashSet::new();let mut changes=Vec::new();let mut total=0usize;
    for item in WalkDir::new(root).follow_links(false).into_iter(){
        let item=item?;if item.path()==root{continue;}
        let meta=fs::symlink_metadata(item.path())?;
        if meta.file_type().is_symlink(){return Err(anyhow!("workspace contains symlink: {}",item.path().display()));}
        if meta.is_dir(){continue;}if !meta.is_file(){return Err(anyhow!("workspace contains special file"));}
        let path=item.path().strip_prefix(root)?.to_string_lossy().replace('\\',"/");
        if path.starts_with(".light-agent-"){continue;}validate(&path)?;
        if meta.len()>4*1024*1024{return Err(anyhow!("workspace file exceeds 4 MiB"));}
        let mut bytes=Vec::new();fs::File::open(item.path())?.take(4*1024*1024+1).read_to_end(&mut bytes)?;
        if bytes.len()>4*1024*1024{return Err(anyhow!("workspace file grew beyond limit"));}
        let hash=hex::encode(Sha256::digest(&bytes));seen.insert(path.clone());
        if base_map.get(&path).map(|v|v.as_str())!=Some(hash.as_str()){
            total+=bytes.len();if total>32*1024*1024{return Err(anyhow!("workspace diff exceeds 32 MiB"));}
            changes.push(serde_json::json!({"path":path,"base_sha256":base_map.get(&path),"content_base64":base64::engine::general_purpose::STANDARD.encode(bytes),"deleted":false}));
        }
    }
    for(path,hash)in base_map{if !seen.contains(&path){changes.push(serde_json::json!({"path":path,"base_sha256":hash,"content_base64":"","deleted":true}));}}
    if changes.len()>5000{return Err(anyhow!("workspace diff exceeds 5000 files"));}
    Ok(serde_json::json!({"changes":changes}))
}

fn validate(path:&str)->Result<()>{
    if path.is_empty()||path.len()>1024||path.starts_with('/')||path.contains('\0')||path.contains('\\'){return Err(anyhow!("invalid relative path"));}
    for part in Path::new(path).components(){if !matches!(part,std::path::Component::Normal(_)){return Err(anyhow!("path traversal rejected"));}}
    Ok(())
}
fn open_dir(path:&str)->Result<fs::File>{
    let c=std::ffi::CString::new(path)?;let fd=unsafe{libc::open(c.as_ptr(),libc::O_PATH|libc::O_DIRECTORY|libc::O_CLOEXEC)};
    if fd<0{return Err(std::io::Error::last_os_error().into());}Ok(unsafe{fs::File::from_raw_fd(fd)})
}
fn open_dir_chain(root_fd:i32,parts:&[&str])->Result<fs::File>{
    let dup=unsafe{libc::fcntl(root_fd,libc::F_DUPFD_CLOEXEC,0)};if dup<0{return Err(std::io::Error::last_os_error().into());}
    let mut current=unsafe{fs::File::from_raw_fd(dup)};
    let uid=std::env::var("COSEXECD_UID").unwrap_or_else(|_|"10000".into()).parse::<u32>().unwrap_or(10000);
    let gid=std::env::var("COSEXECD_GID").unwrap_or_else(|_|"10000".into()).parse::<u32>().unwrap_or(10000);
    for part in parts{
        let name=std::ffi::CString::new(*part)?;
        if unsafe{libc::mkdirat(current.as_raw_fd(),name.as_ptr(),0o750)}<0{let e=std::io::Error::last_os_error();if e.kind()!=std::io::ErrorKind::AlreadyExists{return Err(e.into());}}
        let how=OpenHow{flags:(libc::O_PATH|libc::O_DIRECTORY|libc::O_CLOEXEC) as u64,mode:0,resolve:RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS|RESOLVE_NO_SYMLINKS};
        let fd=unsafe{libc::syscall(libc::SYS_openat2,current.as_raw_fd(),name.as_ptr(),&how,std::mem::size_of::<OpenHow>()) as i32};
        if fd<0{return Err(std::io::Error::last_os_error().into());}
        let next=unsafe{fs::File::from_raw_fd(fd)};
        if unsafe{libc::fchown(next.as_raw_fd(),uid,gid)}<0{return Err(std::io::Error::last_os_error().into());}
        current=next;
    }
    Ok(current)
}
#[repr(C)]struct OpenHow{flags:u64,mode:u64,resolve:u64}
const RESOLVE_NO_MAGICLINKS:u64=0x02;const RESOLVE_NO_SYMLINKS:u64=0x04;const RESOLVE_BENEATH:u64=0x08;

#[cfg(test)]
mod tests{use super::*;#[test]fn rejects_parent_paths(){assert!(validate("../etc/passwd").is_err());}#[test]fn rejects_absolute_paths(){assert!(validate("/etc/passwd").is_err());}#[test]fn accepts_relative_paths(){assert!(validate("src/main.c").is_ok());}}
