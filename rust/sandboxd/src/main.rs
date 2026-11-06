mod api;
mod capabilities;
mod config;
mod docker;
mod firecracker;
mod state;
mod vsock;
mod workspace;

use anyhow::{Context, Result};
use axum::{middleware, routing::{get, post}, Router};
use config::Config;
use state::AppState;
use std::net::SocketAddr;
use tracing::info;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(std::env::var("RUST_LOG").unwrap_or_else(|_| "info".into()))
        .init();
    let cfg = Config::from_env()?;
    if !std::path::Path::new("/dev/kvm").exists() {
        info!("KVM device is not present; VM start will fail closed");
    }
    let state = AppState::load(cfg.clone()).context("load sandbox state")?;
    state.docker.recover_on_startup().await.context("recover Docker Workers after daemon restart")?;
    let protected = Router::new()
        .route("/v1/capabilities", get(api::host_capabilities))
        .route("/v1/containers", post(api::create_container))
        .route("/v1/containers/:id", get(api::get_container).delete(api::destroy_container))
        .route("/v1/containers/:id/start", post(api::start_container))
        .route("/v1/containers/:id/stop", post(api::stop_container))
        .route("/v1/containers/:id/exec", post(api::exec_container))
        .route("/v1/vms", post(api::create_vm))
        .route("/v1/vms/:id", get(api::get_vm).delete(api::destroy_vm))
        .route("/v1/vms/:id/start", post(api::start_vm))
        .route("/v1/vms/:id/stop", post(api::stop_vm))
        .route("/v1/vms/:id/guest/health", get(api::guest_health))
        .route("/v1/vms/:id/guest/exec", post(api::guest_exec))
        .route("/v1/vms/:id/guest/cognitive/tasks", post(api::cognitive_submit))
        .route("/v1/vms/:id/guest/cognitive/tasks/:task_id", get(api::cognitive_get).delete(api::cognitive_cancel))
        .route("/v1/vms/:id/workspace/sync", post(api::workspace_sync))
        .route("/v1/vms/:id/workspace/commit", post(api::workspace_commit))
        .with_state(state.clone())
        .route_layer(middleware::from_fn_with_state(cfg.api_token.clone(), api::authenticate));
    let app = Router::new().route("/healthz", get(api::health)).merge(protected);
    let addr: SocketAddr = cfg.listen_addr.parse().context("invalid listen address")?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!(%addr, "cos-sandboxd listening");
    axum::serve(listener, app).with_graceful_shutdown(shutdown_signal()).await?;
    state.stop_all().await;
    Ok(())
}
async fn shutdown_signal() { let _ = tokio::signal::ctrl_c().await; }
