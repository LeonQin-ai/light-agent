package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/LeonQin-ai/light-agent/control/internal/model"
)

func openTestStore(t *testing.T)*SQLite{
	t.Helper()
	st,err:=Open(filepath.Join(t.TempDir(),"state.db"));if err!=nil{t.Fatal(err)}
	t.Cleanup(func(){_=st.Close()})
	return st
}

func TestTaskIdempotencyDispatchAndPayloadConflict(t *testing.T){
	ctx:=context.Background();st:=openTestStore(t)
	agent,err:=st.CreateAgent(ctx,"tenant-test","supervisor");if err!=nil{t.Fatal(err)}
	first,err:=st.CreateTask(ctx,agent.ID,"request-1",`{"content":"hello"}`);if err!=nil{t.Fatal(err)}
	second,err:=st.CreateTask(ctx,agent.ID,"request-1",`{"content":"hello"}`);if err!=nil{t.Fatal(err)}
	if first.ID!=second.ID{t.Fatalf("idempotency failed: %s != %s",first.ID,second.ID)}
	if _,err=st.CreateTask(ctx,agent.ID,"request-1",`{"content":"different"}`);!errors.Is(err,ErrConflict){t.Fatalf("same key with different payload err=%v",err)}
	claimed,err:=st.ClaimNextTask(ctx);if err!=nil{t.Fatal(err)}
	if claimed.ID!=first.ID||claimed.State!=model.TaskRunning{t.Fatalf("unexpected claimed task: %+v",claimed)}
	if err=st.SetAgentState(ctx,agent.ID,model.AgentActive);err!=nil{t.Fatal(err)}
	if err=st.CompleteTask(ctx,first.ID,"mock result");err!=nil{t.Fatal(err)}
	completed,err:=st.GetTask(ctx,first.ID);if err!=nil{t.Fatal(err)}
	if completed.State!=model.TaskCompleted||completed.Result!="mock result"{t.Fatalf("unexpected completed task: %+v",completed)}
	agent,err=st.GetAgent(ctx,agent.ID);if err!=nil{t.Fatal(err)}
	if agent.State!=model.AgentIdle{t.Fatalf("agent state=%s, want IDLE",agent.State)}
}

func TestClaimEmptyQueueReturnsNotFound(t *testing.T){
	st:=openTestStore(t)
	if _,err:=st.ClaimNextTask(context.Background());!errors.Is(err,ErrNotFound){t.Fatalf("err=%v, want ErrNotFound",err)}
}

func TestCancelOnlyQueuedTasks(t *testing.T){
	ctx:=context.Background();st:=openTestStore(t)
	agent,err:=st.CreateAgent(ctx,"tenant-test","supervisor");if err!=nil{t.Fatal(err)}
	task,err:=st.CreateTask(ctx,agent.ID,"cancel-1","{}");if err!=nil{t.Fatal(err)}
	cancelled,err:=st.CancelTask(ctx,task.ID);if err!=nil{t.Fatal(err)}
	if cancelled.State!=model.TaskCancelled{t.Fatalf("state=%s",cancelled.State)}
	if _,err=st.CancelTask(ctx,task.ID);err!=nil{t.Fatalf("cancelling an already cancelled task should be idempotent: %v",err)}
	running,err:=st.CreateTask(ctx,agent.ID,"running-1","{}");if err!=nil{t.Fatal(err)}
	running,err=st.ClaimNextTask(ctx);if err!=nil{t.Fatal(err)}
	if _,err=st.CancelTask(ctx,running.ID);!errors.Is(err,ErrConflict){t.Fatalf("cancelling a running task err=%v, want conflict",err)}
}

func TestFeishuEventDeduplication(t *testing.T){
	ctx:=context.Background();st:=openTestStore(t)
	first,duplicate,err:=st.IngestFeishuEvent(ctx,"event-1","ou_user","oc_chat","hello")
	if err!=nil{t.Fatal(err)};if duplicate{t.Fatal("first event marked duplicate")}
	second,duplicate,err:=st.IngestFeishuEvent(ctx,"event-1","ou_user","oc_chat","hello")
	if err!=nil{t.Fatal(err)}
	if !duplicate||first.ID!=second.ID{t.Fatalf("event dedupe failed: first=%s second=%s duplicate=%v",first.ID,second.ID,duplicate)}
	if _,_,err=st.IngestFeishuEvent(ctx,"","ou_user","oc_chat","hello");err==nil{t.Fatal("empty event id accepted")}
}

func TestPublicAPIRejectsWorkerIdentity(t *testing.T){
	st:=openTestStore(t)
	if _,err:=st.CreateAgent(context.Background(),"tenant-test","worker");err==nil{t.Fatal("public Agent creation must not create Worker identities")}
}
