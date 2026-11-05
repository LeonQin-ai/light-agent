package dispatcher
import("context";"errors";"log/slog";"time";"github.com/LeonQin-ai/light-agent/control/internal/model";"github.com/LeonQin-ai/light-agent/control/internal/store")
type Dispatcher struct{s store.Store;l *slog.Logger}
func New(s store.Store,l *slog.Logger)*Dispatcher{return &Dispatcher{s:s,l:l}}
func(d *Dispatcher)Run(ctx context.Context){tick:=time.NewTicker(250*time.Millisecond);defer tick.Stop();for{t,e:=d.s.ClaimNextTask(ctx);if e==nil{_ = d.s.SetAgentState(ctx,t.AgentID,model.AgentActive);_ = d.s.CompleteTask(ctx,t.ID,"M1 Mock Coordinator: durable task dispatch verified. No LLM, Cognitive OS runtime or command execution is configured.");continue};if !errors.Is(e,store.ErrNotFound)&&ctx.Err()==nil{d.l.Error("claim task","error",e)};select{case<-ctx.Done():return;case<-tick.C:}}}
