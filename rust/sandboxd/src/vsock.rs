use anyhow::{anyhow, Context, Result};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, net::UnixStream, time::{timeout, Duration}};

pub async fn request(socket: &str, port: u32, payload: serde_json::Value) -> Result<serde_json::Value> {
    let mut stream = timeout(Duration::from_secs(3), UnixStream::connect(socket)).await
        .context("connect Firecracker vsock socket timed out")??;
    stream.write_all(format!("CONNECT {port}\n").as_bytes()).await?;
    let mut reader = BufReader::new(stream);
    let mut ack = String::new();
    timeout(Duration::from_secs(10), reader.read_line(&mut ack)).await
        .context("vsock handshake timed out")??;
    if !ack.starts_with("OK ") {
        return Err(anyhow!("Firecracker vsock handshake rejected: {}", ack.trim()));
    }
    let stream = reader.get_mut();
    let mut body = serde_json::to_vec(&payload)?;
    body.push(b'\n');
    timeout(Duration::from_secs(10), stream.write_all(&body)).await
        .context("write guest request timed out")??;
    let mut response = String::new();
    timeout(Duration::from_secs(60), reader.read_line(&mut response)).await
        .context("guest response timed out")??;
    if response.is_empty() { return Err(anyhow!("guest closed vsock connection without a response")); }
    Ok(serde_json::from_str(&response)?)
}
