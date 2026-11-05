use crate::{cognitive,executor,protocol::ExecRequest,safe_fs};
use anyhow::{anyhow,Context,Result};
use serde_json::{json,Value};
use std::{io::{BufRead,BufReader,Write},os::fd::{FromRawFd,RawFd},thread};

#[cfg(target_os="linux")]
#[repr(C)]
struct SockAddrVm{family:u16,reserved1:u16,port:u32,cid:u32,flags:u8,zero:[u8;3]}

#[cfg(target_os="linux")]
pub fn serve(port:u32)->Result<()>{
    let fd=unsafe{libc::socket(libc::AF_VSOCK,libc::SOCK_STREAM|libc::SOCK_CLOEXEC,0)};
    if fd<0{return Err(std::io::Error::last_os_error().into());}
    let addr=SockAddrVm{family:libc::AF_VSOCK as u16,reserved1:0,port,cid:u32::MAX,flags:0,zero:[0;3]};
    let rc=unsafe{libc::bind(fd,&addr as *const _ as *const libc::sockaddr,std::mem::size_of::<SockAddrVm>() as libc::socklen_t)};
    if rc<0{unsafe{libc::close(fd)};return Err(std::io::Error::last_os_error().context("bind AF_VSOCK"));}
    if unsafe{libc::listen(fd,128)}<0{unsafe{libc::close(fd)};return Err(std::io::Error::last_os_error().into());}
    loop{
        let client=unsafe{libc::accept4(fd,std::ptr::null_mut(),std::ptr::null_mut(),libc::SOCK_CLOEXEC)};
        if client<0{continue;}
        thread::spawn(move||{let _=handle(client);});
    }
}
#[cfg(not(target_os="linux"))]
pub fn serve(_port:u32)->Result<()>{Err(anyhow!("cos-execd requires Linux AF_VSOCK"))}

#[cfg(target_os="linux")]
fn handle(fd:RawFd)->Result<()>{
    let file=unsafe{std::fs::File::from_raw_fd(fd)};
    let mut reader=BufReader::new(file);
    let mut line=String::new();
    let n=reader.read_line(&mut line)?;
    if n==0||n>16*1024*1024{return Err(anyhow!("invalid request frame"));}
    let request:Value=serde_json::from_str(line.trim_end())?;
    let response=dispatch(request);
    let mut file=reader.into_inner();
    serde_json::to_writer(&mut file,&response)?;
    file.write_all(b"\n")?;
    file.flush()?;
    Ok(())
}

fn dispatch(v:Value)->Value{
    let result=(||->Result<Value>{
        match v.get("op").and_then(Value::as_str).unwrap_or(""){
            "health"=>Ok(json!({"service":"cos-execd","cognitive_ready":cognitive::is_ready()})),
            "exec"=>{
                let req:ExecRequest=serde_json::from_value(v.get("request").cloned().ok_or_else(||anyhow!("missing request"))?)?;
                Ok(serde_json::to_value(executor::execute(req)?)?)
            },
            "cognitive_submit"=>{
                let prompt=v.get("prompt").and_then(Value::as_str).ok_or_else(||anyhow!("missing prompt"))?;
                let session=v.get("session").and_then(Value::as_str).unwrap_or("default");
                cognitive::submit(prompt,session)
            },
            "cognitive_get"=>{
                let id=v.get("task_id").and_then(Value::as_str).ok_or_else(||anyhow!("missing task_id"))?;
                cognitive::get(id)
            },
            "cognitive_cancel"=>{
                let id=v.get("task_id").and_then(Value::as_str).ok_or_else(||anyhow!("missing task_id"))?;
                cognitive::cancel(id)
            },
            "workspace_put"=>{
                let path=v.get("path").and_then(Value::as_str).ok_or_else(||anyhow!("missing path"))?;
                let content=v.get("content_base64").and_then(Value::as_str).ok_or_else(||anyhow!("missing content"))?;
                safe_fs::write_workspace(path,content)?;
                Ok(json!({"written":true}))
            },
            "workspace_manifest"=>{
                let manifest=v.get("manifest_json").and_then(Value::as_str).ok_or_else(||anyhow!("missing manifest"))?;
                safe_fs::store_manifest(manifest)?;
                Ok(json!({"stored":true}))
            },
            "workspace_diff"=>safe_fs::diff_workspace(),
            other=>Err(anyhow!("unsupported operation: {other}")),
        }
    })();
    match result{Ok(value)=>json!({"ok":true,"result":value}),Err(err)=>json!({"ok":false,"error":format!("{err:#}")})}
}
