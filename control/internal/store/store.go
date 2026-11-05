package store
import("context";"github.com/LeonQin-ai/light-agent/control/internal/model")
type Store interface{
 CreateAgent(context.Context,string,string)(model.Agent,error)
 GetAgent(context.Context,string)(model.Agent,error)
 CreateTask(context.Context,string,string,string)(model.Task,error)
 GetTask(context.Context,string)(model.Task,error)
 CancelTask(context.Context,string)(model.Task,error)
 ClaimNextTask(context.Context)(model.Task,error)
 CompleteTask(context.Context,string,string)error
 SetAgentState(context.Context,string,model.AgentState)error
 IngestFeishuEvent(context.Context,string,string,string,string)(model.Task,bool,error)
 Close()error
}
