package dispatcher

import(
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/LeonQin-ai/light-agent/control/internal/model"
	"github.com/LeonQin-ai/light-agent/control/internal/store"
)

type Dispatcher struct{store store.Store;logger *slog.Logger}
func New(st store.Store,logger *slog.Logger)*Dispatcher{return &Dispatcher{store:st,logger:logger}}

// Run is deliberately non-executing. It verifies durable task dispatch only;
// it never invokes an LLM, a Host shell or a Guest command.
func(d *Dispatcher)Run(ctx context.Context){
	ticker:=time.NewTicker(250*time.Millisecond);defer ticker.Stop()
	for{
		task,err:=d.store.ClaimNextTask(ctx)
		if err==nil{
			if stateErr:=d.store.SetAgentState(ctx,task.AgentID,model.AgentActive);stateErr!=nil{
				d.logger.Error("mark agent active","task_id",task.ID,"error",stateErr)
			}
			if completeErr:=d.store.CompleteTask(ctx,task.ID,"M1 Mock Coordinator: durable task dispatch verified. No LLM, Cognitive OS runtime or command execution is configured.");completeErr!=nil{
				d.logger.Error("complete mock task","task_id",task.ID,"error",completeErr)
			}else{
				d.logger.Info("mock task completed","task_id",task.ID,"agent_id",task.AgentID)
			}
			continue
		}
		if !errors.Is(err,store.ErrNotFound)&&ctx.Err()==nil{d.logger.Error("claim queued task","error",err)}
		select{case<-ctx.Done():return;case<-ticker.C:}
	}
}
