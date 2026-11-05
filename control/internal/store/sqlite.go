package store

import (
 "context"
 "crypto/rand"
 "database/sql"
 "encoding/hex"
 "encoding/json"
 "errors"
 "fmt"
 "os"
 "path/filepath"
 "time"

 "github.com/LeonQin-ai/light-agent/control/internal/model"
 _ "modernc.org/sqlite"
)
var ErrNotFound=errors.New("not found")
type SQLite struct{ db *sql.DB }
func Open(path string)(*SQLite,error){
 if path!=":memory:" { if err:=os.MkdirAll(filepath.Dir(path),0750);err!=nil{return nil,err} }
 db,err:=sql.Open("sqlite",path);if err!=nil{return nil,err};db.SetMaxOpenConns(1)
 s:=&SQLite{db:db}
 _,err=db.Exec(`PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,role TEXT NOT NULL,state TEXT NOT NULL,memory_id TEXT NOT NULL,session_id TEXT NOT NULL,workspace_id TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_active_at TEXT NOT NULL,external_identity TEXT UNIQUE);
 CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),request_id TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(agent_id,request_id));
 CREATE TABLE IF NOT EXISTS event_inbox(event_id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,task_id TEXT NOT NULL,received_at TEXT NOT NULL);`)
 if err!=nil{db.Close();return nil,err};return s,nil
}
func(s *SQLite)CreateAgent(ctx context.Context,tenant,role string)(model.Agent,error){
 if tenant==""{return model.Agent{},errors.New("tenant_id required")};if role==""{role="supervisor"};if role!="supervisor"{return model.Agent{},errors.New("only supervisor role is public")}
 now,id:=ts(),uid("agt")
 _,err:=s.db.ExecContext(ctx,`INSERT INTO agents(id,tenant_id,role,state,memory_id,session_id,workspace_id,created_at,updated_at,last_active_at) VALUES(?,?,?,'IDLE',?,?,?,?,?,?)`,id,tenant,role,uid("mem"),uid("ses"),uid("wsp"),now,now,now)
 if err!=nil{return model.Agent{},err};return s.GetAgent(ctx,id)
}
func(s *SQLite)GetAgent(ctx context.Context,id string)(model.Agent,error){
 var a model.Agent;var state string
 err:=s.db.QueryRowContext(ctx,"SELECT id,tenant_id,role,state,memory_id,session_id,workspace_id,generation,created_at,updated_at,last_active_at FROM agents WHERE id=?",id).Scan(&a.ID,&a.TenantID,&a.Role,&state,&a.MemoryID,&a.SessionID,&a.WorkspaceID,&a.Generation,&a.CreatedAt,&a.UpdatedAt,&a.LastActiveAt)
 if errors.Is(err,sql.ErrNoRows){return a,ErrNotFound};a.State=model.AgentState(state);return a,err
}
func(s *SQLite)CreateTask(ctx context.Context,agent,request,payload string)(model.Task,error){
 if _,err:=s.GetAgent(ctx,agent);err!=nil{return model.Task{},err};if request==""{request=uid("req")};if payload==""{payload="{}"}
 now,id:=ts(),uid("tsk")
 _,err:=s.db.ExecContext(ctx,"INSERT INTO tasks(id,agent_id,request_id,state,payload,created_at,updated_at) VALUES(?,?,?,'QUEUED',?,?,?) ON CONFLICT(agent_id,request_id) DO NOTHING",id,agent,request,payload,now,now)
 if err!=nil{return model.Task{},err};return s.taskByRequest(ctx,agent,request)
}
func(s *SQLite)taskByRequest(ctx context.Context,a,r string)(model.Task,error){return scan(s.db.QueryRowContext(ctx,"SELECT id,agent_id,request_id,state,payload,result,created_at,updated_at FROM tasks WHERE agent_id=? AND request_id=?",a,r))}
func(s *SQLite)GetTask(ctx context.Context,id string)(model.Task,error){return scan(s.db.QueryRowContext(ctx,"SELECT id,agent_id,request_id,state,payload,result,created_at,updated_at FROM tasks WHERE id=?",id))}
type scanner interface{Scan(...any)error}
func scan(row scanner)(model.Task,error){var t model.Task;var state string;err:=row.Scan(&t.ID,&t.AgentID,&t.RequestID,&state,&t.Payload,&t.Result,&t.CreatedAt,&t.UpdatedAt);if errors.Is(err,sql.ErrNoRows){return t,ErrNotFound};t.State=model.TaskState(state);return t,err}
func(s *SQLite)ClaimNextTask(ctx context.Context)(model.Task,error){
 tx,err:=s.db.BeginTx(ctx,nil);if err!=nil{return model.Task{},err};defer tx.Rollback()
 var id string;err=tx.QueryRowContext(ctx,"SELECT id FROM tasks WHERE state='QUEUED' ORDER BY created_at LIMIT 1").Scan(&id);if err!=nil{return model.Task{},err}
 _,err=tx.ExecContext(ctx,"UPDATE tasks SET state='RUNNING',updated_at=? WHERE id=? AND state='QUEUED'",ts(),id);if err!=nil{return model.Task{},err}
 t,err:=scan(tx.QueryRowContext(ctx,"SELECT id,agent_id,request_id,state,payload,result,created_at,updated_at FROM tasks WHERE id=?",id));if err!=nil{return t,err};if err=tx.Commit();err!=nil{return t,err};return t,nil
}
func(s *SQLite)CompleteTask(ctx context.Context,id,result string)error{_,err:=s.db.ExecContext(ctx,"UPDATE tasks SET state='COMPLETED',result=?,updated_at=? WHERE id=? AND state='RUNNING'",result,ts(),id);return err}
func(s *SQLite)CancelTask(ctx context.Context,id string)(model.Task,error){_,err:=s.db.ExecContext(ctx,"UPDATE tasks SET state='CANCELLED',updated_at=? WHERE id=? AND state IN ('QUEUED','RUNNING')",ts(),id);if err!=nil{return model.Task{},err};return s.GetTask(ctx,id)}
func(s *SQLite)SetAgentState(ctx context.Context,id string,state model.AgentState)error{_,err:=s.db.ExecContext(ctx,"UPDATE agents SET state=?,updated_at=? WHERE id=?",state,ts(),id);return err}
func(s *SQLite)IngestFeishuEvent(ctx context.Context,event,open,chat,message string)(model.Task,bool,error){
 tx,err:=s.db.BeginTx(ctx,nil);if err!=nil{return model.Task{},false,err};defer tx.Rollback()
 var tid string
 err=tx.QueryRowContext(ctx,"SELECT task_id FROM event_inbox WHERE event_id=?",event).Scan(&tid)
 if err==nil{t,e:=scan(tx.QueryRowContext(ctx,"SELECT id,agent_id,request_id,state,payload,result,created_at,updated_at FROM tasks WHERE id=?",tid));return t,true,e}
 if !errors.Is(err,sql.ErrNoRows){return model.Task{},false,err}
 now:=ts();var aid string
 err=tx.QueryRowContext(ctx,"SELECT id FROM agents WHERE external_identity=?",open).Scan(&aid)
 if errors.Is(err,sql.ErrNoRows){aid=uid("agt");_,err=tx.ExecContext(ctx,`INSERT INTO agents(id,tenant_id,role,state,memory_id,session_id,workspace_id,created_at,updated_at,last_active_at,external_identity) VALUES(?,'feishu:p2p','supervisor','IDLE',?,?,?,?,?,?,?)`,aid,uid("mem"),uid("ses"),uid("wsp"),now,now,now,open)}
 if err!=nil{return model.Task{},false,err}
 p,_:=json.Marshal(map[string]string{"source":"feishu","open_id":open,"chat_id":chat,"content":message});tid=uid("tsk")
 _,err=tx.ExecContext(ctx,"INSERT INTO tasks(id,agent_id,request_id,state,payload,created_at,updated_at) VALUES(?,?,?,'QUEUED',?,?,?)",tid,aid,event,string(p),now,now);if err!=nil{return model.Task{},false,err}
 _,err=tx.ExecContext(ctx,"INSERT INTO event_inbox(event_id,agent_id,task_id,received_at) VALUES(?,?,?,?)",event,aid,tid,now);if err!=nil{return model.Task{},false,err}
 t,err:=scan(tx.QueryRowContext(ctx,"SELECT id,agent_id,request_id,state,payload,result,created_at,updated_at FROM tasks WHERE id=?",tid));if err!=nil{return t,false,err};if err=tx.Commit();err!=nil{return t,false,err};return t,false,nil
}
func(s *SQLite)Close()error{return s.db.Close()}
func ts()string{return time.Now().UTC().Format(time.RFC3339Nano)}
func uid(p string)string{var b [16]byte;if _,e:=rand.Read(b[:]);e!=nil{return fmt.Sprintf("%s-%d",p,time.Now().UnixNano())};return p+"_"+hex.EncodeToString(b[:])}
