use anyhow::{anyhow, Context, Result};
use base64::Engine;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{fs, io::{Read, Write}, os::fd::{AsRawFd, FromRawFd}, path::Path};
use uuid::Uuid;
use walkdir::WalkDir;

pub struct Entry { pub path: String, pub bytes: Vec<u8> }
pub struct Snapshot { pub entries: Vec<Entry>, pub manifest_json: String }
#[derive(Serialize)]
struct ManifestEntry { path: String, sha256: String }

pub fn collect(root: &str) -> Result<Snapshot> {
    let root = fs::canonicalize(root)?;
    let mut entries = Vec::new();
    let mut manifest = Vec::new();
    for item in WalkDir::new(&root).follow_links(false).into_iter().filter_entry(|e| {
        e.file_name() != ".git" && e.file_name() != ".light-agent"
    }) {
        let item = item?;
        if item.path() == root { continue; }
        let meta = fs::symlink_metadata(item.path())?;
        if meta.file_type().is_symlink() { return Err(anyhow!("workspace contains symlink: {}", item.path().display())); }
        if meta.is_dir() { continue; }
        if !meta.is_file() { return Err(anyhow!("workspace contains special file: {}", item.path().display())); }
        if meta.len() > 4 * 1024 * 1024 { return Err(anyhow!("workspace file exceeds 4 MiB: {}", item.path().display())); }
        let relative = item.path().strip_prefix(&root)?.to_string_lossy().replace('\\', "/");
        validate_relative(&relative)?;
        let bytes = read_beneath(&root, &relative, 4 * 1024 * 1024)?;
        let hash = hex::encode(Sha256::digest(&bytes));
        manifest.push(ManifestEntry { path: relative.clone(), sha256: hash });
        entries.push(Entry { path: relative, bytes });
        if entries.len() > 5000 { return Err(anyhow!("workspace exceeds 5000 files")); }
    }
    Ok(Snapshot { entries, manifest_json: serde_json::to_string(&manifest)? })
}

pub fn commit_file(root: &str, path: &str, expected: Option<&str>, content_b64: &str) -> Result<()> {
    validate_relative(path)?;
    if path.starts_with(".light-agent-") { return Err(anyhow!("reserved path")); }
    let bytes = base64::engine::general_purpose::STANDARD.decode(content_b64)?;
    if bytes.len() > 4 * 1024 * 1024 { return Err(anyhow!("committed file exceeds 4 MiB")); }
    let root_fd = open_root(root)?;
    let _lock = lock_workspace(root_fd.as_raw_fd())?;
    let (parent_fd, basename) = open_parent(root_fd.as_raw_fd(), path)?;
    let current = hash_at(parent_fd.as_raw_fd(), &basename)?;
    match (expected, current.as_deref()) {
        (Some(want), Some(actual)) if want == actual => {},
        (None, None) => {},
        _ => return Err(anyhow!("optimistic commit conflict")),
    }
    let tmp = format!(".light-agent-{}.tmp", Uuid::new_v4());
    let tmp_c = std::ffi::CString::new(tmp.as_str())?;
    let how = OpenHow { flags: (libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC) as u64, mode: 0o640, resolve: RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS };
    let fd = unsafe { libc::syscall(libc::SYS_openat2, parent_fd.as_raw_fd(), tmp_c.as_ptr(), &how, std::mem::size_of::<OpenHow>()) as i32 };
    if fd < 0 { return Err(std::io::Error::last_os_error().into()); }
    let file = unsafe { fs::File::from_raw_fd(fd) };
    file.write_all(&bytes)?;
    file.sync_all()?;
    let target_c = std::ffi::CString::new(basename.as_str())?;
    let flags = if expected.is_none() { RENAME_NOREPLACE } else { 0 };
    let rc = unsafe { libc::syscall(libc::SYS_renameat2, parent_fd.as_raw_fd(), tmp_c.as_ptr(), parent_fd.as_raw_fd(), target_c.as_ptr(), flags) };
    if rc < 0 {
        let err = std::io::Error::last_os_error();
        let _ = unsafe { libc::unlinkat(parent_fd.as_raw_fd(), tmp_c.as_ptr(), 0) };
        return Err(err.into());
    }
    Ok(())
}

pub fn delete_file(root: &str, path: &str, expected: Option<&str>) -> Result<()> {
    validate_relative(path)?;
    if path.starts_with(".light-agent-") { return Err(anyhow!("reserved path")); }
    let root_fd = open_root(root)?;
    let _lock = lock_workspace(root_fd.as_raw_fd())?;
    let (parent_fd, basename) = open_parent(root_fd.as_raw_fd(), path)?;
    let current = hash_at(parent_fd.as_raw_fd(), &basename)?;
    if current.as_deref() != expected { return Err(anyhow!("optimistic delete conflict")); }
    let target_c = std::ffi::CString::new(basename.as_str())?;
    if unsafe { libc::unlinkat(parent_fd.as_raw_fd(), target_c.as_ptr(), 0) } < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}

fn read_beneath(root: &Path, relative: &str, limit: usize) -> Result<Vec<u8>> {
    let root_fd = open_root(root.to_string_lossy().as_ref())?;
    let path_c = std::ffi::CString::new(relative)?;
    let how = OpenHow { flags: (libc::O_RDONLY | libc::O_CLOEXEC) as u64, mode: 0, resolve: RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS };
    let fd = unsafe { libc::syscall(libc::SYS_openat2, root_fd.as_raw_fd(), path_c.as_ptr(), &how, std::mem::size_of::<OpenHow>()) as i32 };
    if fd < 0 { return Err(std::io::Error::last_os_error().into()); }
    let mut file = unsafe { fs::File::from_raw_fd(fd) };
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > limit { return Err(anyhow!("file exceeds size limit")); }
    Ok(bytes)
}

fn open_root(root: &str) -> Result<fs::File> {
    let c = std::ffi::CString::new(root)?;
    let fd = unsafe { libc::open(c.as_ptr(), libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC) };
    if fd < 0 { return Err(std::io::Error::last_os_error().into()); }
    Ok(unsafe { fs::File::from_raw_fd(fd) })
}
fn open_parent(root_fd: i32, path: &str) -> Result<(fs::File, String)> {
    let p = Path::new(path);
    let basename = p.file_name().ok_or_else(|| anyhow!("missing filename"))?.to_string_lossy().to_string();
    let parent = p.parent().unwrap_or(Path::new(""));
    if parent.as_os_str().is_empty() {
        let fd = unsafe { libc::fcntl(root_fd, libc::F_DUPFD_CLOEXEC, 0) };
        if fd < 0 { return Err(std::io::Error::last_os_error().into()); }
        return Ok((unsafe { fs::File::from_raw_fd(fd) }, basename));
    }
    let c = std::ffi::CString::new(parent.to_string_lossy().as_bytes())?;
    let how = OpenHow { flags: (libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC) as u64, mode: 0, resolve: RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS };
    let fd = unsafe { libc::syscall(libc::SYS_openat2, root_fd, c.as_ptr(), &how, std::mem::size_of::<OpenHow>()) as i32 };
    if fd < 0 { return Err(anyhow!("open workspace parent: {}", std::io::Error::last_os_error())); }
    Ok((unsafe { fs::File::from_raw_fd(fd) }, basename))
}
fn hash_at(parent_fd: i32, name: &str) -> Result<Option<String>> {
    let c = std::ffi::CString::new(name)?;
    let how = OpenHow { flags: (libc::O_RDONLY | libc::O_CLOEXEC) as u64, mode: 0, resolve: RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS };
    let fd = unsafe { libc::syscall(libc::SYS_openat2, parent_fd, c.as_ptr(), &how, std::mem::size_of::<OpenHow>()) as i32 };
    if fd < 0 {
        let e = std::io::Error::last_os_error();
        if e.kind() == std::io::ErrorKind::NotFound { return Ok(None); }
        return Err(e.into());
    }
    let mut file = unsafe { fs::File::from_raw_fd(fd) };
    let mut bytes = Vec::new();
    file.take(4 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 4 * 1024 * 1024 { return Err(anyhow!("target file exceeds size limit")); }
    Ok(Some(hex::encode(Sha256::digest(&bytes))))
}
fn lock_workspace(root_fd: i32) -> Result<fs::File> {
    let c = std::ffi::CString::new(".light-agent-commit.lock")?;
    let how = OpenHow { flags: (libc::O_RDWR | libc::O_CREAT | libc::O_CLOEXEC) as u64, mode: 0o600, resolve: RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS };
    let fd = unsafe { libc::syscall(libc::SYS_openat2, root_fd, c.as_ptr(), &how, std::mem::size_of::<OpenHow>()) as i32 };
    if fd < 0 { return Err(std::io::Error::last_os_error().into()); }
    let file = unsafe { fs::File::from_raw_fd(fd) };
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } < 0 { return Err(std::io::Error::last_os_error().into()); }
    Ok(file)
}
fn validate_relative(path: &str) -> Result<()> {
    if path.is_empty() || path.len() > 1024 || path.starts_with('/') || path.contains('\0') || path.contains('\\') {
        return Err(anyhow!("invalid relative path"));
    }
    if path.split('/').any(|part| part == ".git" || part == ".light-agent" || part.starts_with(".light-agent-")) {
        return Err(anyhow!("reserved workspace metadata path"));
    }
    for part in Path::new(path).components() {
        if !matches!(part, std::path::Component::Normal(_)) { return Err(anyhow!("path traversal rejected")); }
    }
    Ok(())
}
#[repr(C)]
struct OpenHow { flags: u64, mode: u64, resolve: u64 }
const RESOLVE_NO_MAGICLINKS: u64 = 0x02;
const RESOLVE_NO_SYMLINKS: u64 = 0x04;
const RESOLVE_BENEATH: u64 = 0x08;
const RENAME_NOREPLACE: u32 = 1;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_parent_paths() { assert!(validate_relative("../etc/passwd").is_err()); }
    #[test]
    fn rejects_absolute_paths() { assert!(validate_relative("/etc/passwd").is_err()); }
    #[test]
    fn accepts_normal_paths() { assert!(validate_relative("src/main.c").is_ok()); }
}
