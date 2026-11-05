use anyhow::{anyhow,Context,Result};
use serde_json::{json,Value};
use std::{env,io::{Read,Write},net::{TcpStream,ToSocketAddrs},time::Duration};

fn call(method:&str,path:&str,body:Option<Value>)->Result<Value>{
    let token=env::var("COA_GUEST_API_TOKEN").context("COA_GUEST_API_TOKEN is not set")?;
    let mut stream=TcpStream::connect_timeout(&"127.0.0.1:8080".to_socket_addrs()?.next().ok_or_else(||anyhow!("resolve loopback"))?,Duration::from_secs(3))?;
    stream.set_read_timeout(Some(Duration::from_secs(60)))?;
    let bytes=body.map(|v|serde_json::to_vec(&v)).transpose()?.unwrap_or_default();
    let head=format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",bytes.len());
    stream.write_all(head.as_bytes())?;stream.write_all(&bytes)?;
    let mut response=Vec::new();stream.read_to_end(&mut response)?;
    let text=String::from_utf8_lossy(&response);
    let (head,body)=text.split_once("\r\n\r\n").ok_or_else(||anyhow!("invalid Cognitive OS HTTP response"))?;
    let status=head.lines().next().and_then(|l|l.split_whitespace().nth(1)).unwrap_or("0").parse::<u16>()?;
    let value:Value=serde_json::from_str(body.trim())?;
    if !(200..300).contains(&status){return Err(anyhow!("Cognitive OS returned HTTP {status}: {value}"));}
    Ok(value)
}
pub fn is_ready()->bool{TcpStream::connect_timeout(&"127.0.0.1:8080".parse().unwrap(),Duration::from_millis(200)).is_ok()}
pub fn submit(prompt:&str,session:&str)->Result<Value>{call("POST","/v1/tasks",Some(json!({"prompt":prompt,"session":session})))}
pub fn get(id:&str)->Result<Value>{if !id.chars().all(|c|c.is_ascii_digit()){return Err(anyhow!("invalid task id"));}call("GET",&format!("/v1/tasks/{id}"),None)}
pub fn cancel(id:&str)->Result<Value>{if !id.chars().all(|c|c.is_ascii_digit()){return Err(anyhow!("invalid task id"));}call("DELETE",&format!("/v1/tasks/{id}"),None)}
