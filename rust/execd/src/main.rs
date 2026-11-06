mod cognitive;
mod executor;
mod protocol;
mod safe_fs;
mod vsock;

use anyhow::Result;
use std::env;

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    if args.iter().any(|arg| arg == "--stdio") { return vsock::serve_stdio(); }
    let port=args.iter().position(|arg| arg == "--port").and_then(|index| args.get(index + 1)).cloned().unwrap_or_else(||"5000".into()).parse::<u32>()?;
    println!("cos-execd listening on AF_VSOCK port {port}");
    vsock::serve(port)
}
