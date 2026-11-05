package model

type AgentState string
const (
 AgentIdle AgentState="IDLE"
 AgentActive AgentState="ACTIVE"
 AgentSleeping AgentState="SLEEPING"
 AgentFailed AgentState="FAILED"
)
type TaskState string
const (
 TaskQueued TaskState="QUEUED"
 TaskRunning TaskState="RUNNING"
 TaskCompleted TaskState="COMPLETED"
 TaskCancelled TaskState="CANCELLED"
)
type Agent struct {
 ID string `json:"agent_id"`
 TenantID string `json:"tenant_id"`
 Role string `json:"role"`
 State AgentState `json:"state"`
 MemoryID string `json:"memory_id"`
 SessionID string `json:"session_id"`
 WorkspaceID string `json:"workspace_id"`
 Generation uint64 `json:"generation"`
 CreatedAt string `json:"created_at"`
 UpdatedAt string `json:"updated_at"`
 LastActiveAt string `json:"last_active_at"`
}
type Task struct {
 ID string `json:"task_id"`
 AgentID string `json:"agent_id"`
 RequestID string `json:"request_id"`
 State TaskState `json:"state"`
 Payload string `json:"payload"`
 Result string `json:"result,omitempty"`
 CreatedAt string `json:"created_at"`
 UpdatedAt string `json:"updated_at"`
}
