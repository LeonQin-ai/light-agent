use crate::{config::validate_workspace_id, state::{AppState, VmRecord}, workspace};
use axum::{extract::{Path, State}, http::{header::AUTHORIZATION, Request, StatusCode}, middleware::Next, response::{IntoResponse, Response}, Json};
use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Debug)]
pub struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response { (self.0, Json(json!({"error":self.1}))).into_response() }
}
impl From<anyhow::Error> for ApiError {
    fn from(e: anyhow::Error) -> Self { Self(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")) }
}

pub async fn authenticate(
    axum::extract::State(token): axum::extract::State<String>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    let supplied = request.headers().get(AUTHORIZATION).and_then(|h| h.to_str().ok()).unwrap_or("");
    let candidate = supplied.strip_prefix("Bearer ").unwrap_or("");
    if !constant_time_eq(candidate.as_bytes(), token.as_bytes()) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error":"unauthorized"}))).into_response();
    }
    next.run(request).await
}
fn constant_time_eq(a:&[u8],b:&[u8])->bool{
    if a.len()!=b.len(){return false;}
    let mut diff=0u8;for(x,y)in a.iter().zip(b){diff|=x^y;}diff==0
}

pub async fn health() -> Json<Value> { Json(json!({"status":"ok","service":"cos-sandboxd"})) }

#[derive(Deserialize)]
pub struct CreateVm {
    pub worker_id: String,
    pub workspace_id: String,
    #[serde(default)]
    pub image_digest: String,
    #[serde(default)]
    pub vcpu_count: u32,
    #[serde(default)]
    pub memory_mib: u64,
}
pub async fn create_vm(State(state):State<AppState>, Json(req):Json<CreateVm>) -> Result<(StatusCode,Json<VmRecord>),ApiError> {
    if !validate_workspace_id(&req.workspace_id) { return Err(ApiError(StatusCode::BAD_REQUEST,"invalid workspace_id".into())); }
    let record=state.create_vm(req.worker_id,req.workspace_id,req.image_digest,req.vcpu_count,req.memory_mib)?;
    Ok((StatusCode::CREATED,Json(record)))
}
pub async fn get_vm(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<VmRecord>,ApiError>{Ok(Json(state.get_vm(&id)?))}
pub async fn start_vm(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<VmRecord>,ApiError>{Ok(Json(state.start_vm(&id).await?))}
pub async fn stop_vm(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<VmRecord>,ApiError>{Ok(Json(state.stop_vm(&id).await?))}
pub async fn destroy_vm(State(state):State<AppState>,Path(id):Path<String>)->Result<StatusCode,ApiError>{state.destroy_vm(&id).await?;Ok(StatusCode::NO_CONTENT)}

#[derive(Deserialize,Serialize)]
pub struct ExecBody {
    pub executable:String,
    #[serde(default)] pub argv:Vec<String>,
    #[serde(default)] pub cwd:String,
    #[serde(default)] pub timeout_ms:u64,
    #[serde(default)] pub memory_bytes:u64,
    #[serde(default)] pub max_output_bytes:usize,
    #[serde(default)] pub network:bool,
}
pub async fn guest_exec(State(state):State<AppState>,Path(id):Path<String>,Json(req):Json<ExecBody>)->Result<Json<Value>,ApiError>{
    if !req.executable.starts_with('/') || req.executable.contains('\0') || req.argv.len()>128 {
        return Err(ApiError(StatusCode::BAD_REQUEST,"executable must be absolute and argv <=128".into()));
    }
    Ok(Json(state.guest_request(&id,5000,json!({"op":"exec","request":req})).await?))
}
pub async fn guest_health(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<Value>,ApiError>{
    Ok(Json(state.guest_request(&id,5000,json!({"op":"health"})).await?))
}
#[derive(Deserialize)]
pub struct PromptBody { pub prompt:String, #[serde(default)] pub session:String }
pub async fn cognitive_submit(State(state):State<AppState>,Path(id):Path<String>,Json(req):Json<PromptBody>)->Result<Json<Value>,ApiError>{
    if req.prompt.trim().is_empty() || req.prompt.len()>128*1024 { return Err(ApiError(StatusCode::BAD_REQUEST,"prompt must be 1..131072 bytes".into())); }
    Ok(Json(state.guest_request(&id,5000,json!({"op":"cognitive_submit","prompt":req.prompt,"session":req.session})).await?))
}
pub async fn cognitive_get(State(state):State<AppState>,Path((id,task_id)):Path<(String,String)>)->Result<Json<Value>,ApiError>{
    Ok(Json(state.guest_request(&id,5000,json!({"op":"cognitive_get","task_id":task_id})).await?))
}
pub async fn cognitive_cancel(State(state):State<AppState>,Path((id,task_id)):Path<(String,String)>)->Result<Json<Value>,ApiError>{
    Ok(Json(state.guest_request(&id,5000,json!({"op":"cognitive_cancel","task_id":task_id})).await?))
}

pub async fn workspace_sync(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<Value>,ApiError>{
    let record=state.get_vm(&id)?;
    if record.state!="RUNNING" { return Err(ApiError(StatusCode::CONFLICT,"VM is not RUNNING".into())); }
    let snapshot=workspace::collect(&record.workspace_root)?;
    let mut total=0usize;
    let mut sent=0usize;
    for entry in snapshot.entries {
        total=total.saturating_add(entry.bytes.len());
        if total>32*1024*1024 { return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE,"workspace sync exceeds 32 MiB MVP limit".into())); }
        let content=base64::engine::general_purpose::STANDARD.encode(&entry.bytes);
        let reply=state.guest_request(&id,5000,json!({"op":"workspace_put","path":entry.path,"content_base64":content})).await?;
        ensure_guest_ok(&reply)?;
        sent+=1;
    }
    let reply=state.guest_request(&id,5000,json!({"op":"workspace_manifest","manifest_json":snapshot.manifest_json})).await?;
    ensure_guest_ok(&reply)?;
    Ok(Json(json!({"synced_files":sent,"bytes":total})))
}

pub async fn workspace_commit(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<Value>,ApiError>{
    let record=state.get_vm(&id)?;
    let response=state.guest_request(&id,5000,json!({"op":"workspace_diff"})).await?;
    ensure_guest_ok(&response)?;
    let diff=response.get("result").unwrap_or(&response);
    let changes=diff.get("changes").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut committed=0usize;
    let mut conflicts=Vec::new();
    for item in changes {
        let path=item.get("path").and_then(Value::as_str).unwrap_or("");
        let expected=item.get("base_sha256").and_then(Value::as_str);
        let deleted=item.get("deleted").and_then(Value::as_bool).unwrap_or(false);
        let content=item.get("content_base64").and_then(Value::as_str).unwrap_or("");
        let result=if deleted { workspace::delete_file(&record.workspace_root,path,expected) }
                   else { workspace::commit_file(&record.workspace_root,path,expected,content) };
        match result { Ok(())=>committed+=1, Err(e)=>conflicts.push(json!({"path":path,"error":format!("{e:#}")})) }
    }
    Ok(Json(json!({"committed":committed,"conflicts":conflicts})))
}
fn ensure_guest_ok(v:&Value)->Result<(),ApiError>{
    if v.get("ok").and_then(Value::as_bool)==Some(true){Ok(())}
    else{Err(ApiError(StatusCode::BAD_GATEWAY,v.get("error").and_then(Value::as_str).unwrap_or("Guest operation failed").to_string()))}
}
