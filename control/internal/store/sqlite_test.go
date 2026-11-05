package store

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
)

func TestMigrateLegacyM1DatabaseWithoutDataLoss(t *testing.T) {
	path:=filepath.Join(t.TempDir(),"legacy.db")
	db,err:=sql.Open("sqlite",path);if err!=nil{t.Fatal(err)}
	legacy:=`CREATE TABLE agents (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL, external_identity TEXT UNIQUE, memory_id TEXT NOT NULL, session_id TEXT NOT NULL, workspace_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_active_at TEXT NOT NULL);
CREATE TABLE tasks (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), parent_task_id TEXT, request_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(agent_id,request_id));
CREATE TABLE event_inbox (event_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), task_id TEXT NOT NULL REFERENCES tasks(id), source TEXT NOT NULL, received_at TEXT NOT NULL);`
	if _,err=db.Exec(legacy);err!=nil{t.Fatal(err)}
	if err=db.Close();err!=nil{t.Fatal(err)}
	st,err:=Open(path);if err!=nil{t.Fatalf("upgrade legacy database: %v",err)};defer st.Close()
	ctx:=context.Background()
	agent,err:=st.CreateAgent(ctx,"tenant-upgrade","supervisor");if err!=nil{t.Fatal(err)}
	task,duplicate,err:=st.IngestFeishuEvent(ctx,"legacy-event","ou_legacy","oc_chat","hello")
	if err!=nil{t.Fatalf("ingest after migration: %v",err)}
	if duplicate||task.AgentID==""{t.Fatalf("unexpected migrated event: task=%+v duplicate=%v",task,duplicate)}
	if _,err=st.GetRuntime(ctx,agent.ID);err!=nil{t.Fatalf("runtime table unavailable after migration: %v",err)}
	var errorCode string
	if err=st.db.QueryRowContext(ctx,"SELECT error_code FROM tasks WHERE id=?",task.ID).Scan(&errorCode);err!=nil{t.Fatalf("tasks.error_code was not added: %v",err)}
}
