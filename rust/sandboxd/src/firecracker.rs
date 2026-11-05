use anyhow::{anyhow, Context, Result};
use serde_json::json;
use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::UnixStream, time::{timeout, Duration}};

pub async fn configure(socket:&str,guest_cid:u32,vcpu_count:u32,memory_mib:u64)->Result<()>{
    put(socket,"/machine-config",json!({"vcpu_count":vcpu_count,"mem_size_mib":memory_mib,"smt":false,"track_dirty_pages":false})).await?;
    put(socket,"/boot-source",json!({"kernel_image_path":"/vmlinux","boot_args":"console=ttyS0 reboot=k panic=1 pci=off"})).await?;
    put(socket,"/drives/rootfs",json!({"drive_id":"rootfs","path_on_host":"/rootfs.ext4","is_root_device":true,"is_read_only":false})).await?;
    put(socket,"/vsock",json!({"guest_cid":guest_cid,"uds_path":"/run/vsock.sock"})).await?;
    put(socket,"/actions",json!({"action_type":"InstanceStart"})).await?;
    Ok(())
}
async fn put(socket:&str,path:&str,body:serde_json::Value)->Result<()>{
    let payload=serde_json::to_vec(&body)?;
    let request=format!("PUT {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",payload.len());
    let mut stream=timeout(Duration::from_secs(3),UnixStream::connect(socket)).await.context("connect Firecracker API timed out")??;
    stream.write_all(request.as_bytes()).await?;stream.write_all(&payload).await?;
    let mut response=Vec::new();timeout(Duration::from_secs(10),stream.read_to_end(&mut response)).await.context("Firecracker API response timed out")??;
    let first=String::from_utf8_lossy(&response).lines().next().unwrap_or("").to_string();
    let status=first.split_whitespace().nth(1).unwrap_or("0").parse::<u16>().unwrap_or(0);
    if !(200..300).contains(&status){return Err(anyhow!("Firecracker API {path} failed: {first}; body={}",String::from_utf8_lossy(&response).split("\r\n\r\n").nth(1).unwrap_or("")));}
    Ok(())
}
