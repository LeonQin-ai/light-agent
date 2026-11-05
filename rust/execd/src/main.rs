mod cognitive;
mod executor;
mod protocol;
mod safe_fs;
mod vsock;

use anyhow::Result;
use std::env;

fn main() -> Result<()> {
    let port=env::args().skip_while(|a|a!="--port").nth(1).unwrap_or_else(||"5000".into()).parse::<u32>()?;
    println!("cos-execd listening on AF_VSOCK port {port}");
    vsock::serve(port)
}
