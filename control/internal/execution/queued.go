package execution

import (
	"fmt"
	"strings"
)

const TrustedExecutionKind = "trusted_execution"

// QueuedExecution is created only by the operator execution admission endpoint.
// Message/Feishu content is never decoded as this envelope.
type QueuedExecution struct {
	Kind        string   `json:"kind"`
	Mode        Mode     `json:"mode"`
	CPUMillis   uint32   `json:"cpu_millis"`
	MemoryBytes uint64   `json:"memory_bytes"`
	PidsLimit   uint64   `json:"pids_limit"`
	Command     ExecSpec `json:"command"`
}

func (q *QueuedExecution) Normalize() error {
	if q.Kind != TrustedExecutionKind {
		return fmt.Errorf("unsupported task kind")
	}
	if q.Mode == "" {
		q.Mode = ModeStrict
	}
	if q.Mode != ModeStrict && q.Mode != ModeCompatible && q.Mode != ModeAvailability {
		return fmt.Errorf("invalid execution mode")
	}
	if q.CPUMillis == 0 {
		q.CPUMillis = 1000
	}
	if q.MemoryBytes == 0 {
		q.MemoryBytes = 1024 * 1024 * 1024
	}
	if q.PidsLimit == 0 {
		q.PidsLimit = 128
	}
	if q.CPUMillis < 100 || q.MemoryBytes < 128*1024*1024 || q.PidsLimit < 16 {
		return fmt.Errorf("Worker resource limits are below the supported minimum")
	}
	c := &q.Command
	if c.TimeoutMS == 0 {
		c.TimeoutMS = 15000
	}
	if c.MemoryBytes == 0 {
		c.MemoryBytes = q.MemoryBytes
	}
	if c.MaxOutputBytes == 0 {
		c.MaxOutputBytes = 1024 * 1024
	}
	if !strings.HasPrefix(c.Executable, "/") || len(c.Executable) > 4096 || strings.ContainsRune(c.Executable, 0) || len(c.Argv) > 128 {
		return fmt.Errorf("absolute executable and at most 128 arguments are required")
	}
	for _, arg := range c.Argv {
		if len(arg) > 8192 || strings.ContainsRune(arg, 0) {
			return fmt.Errorf("invalid command argument")
		}
	}
	if c.Network {
		return fmt.Errorf("network-enabled Docker tasks are not supported")
	}
	if c.TimeoutMS < 100 || c.TimeoutMS > 300000 || c.MaxOutputBytes > 1024*1024 || c.MemoryBytes < 64*1024*1024 || c.MemoryBytes > 2*1024*1024*1024 || c.MemoryBytes > q.MemoryBytes {
		return fmt.Errorf("command limits are outside the supported range")
	}
	if strings.HasPrefix(c.CWD, "/") || strings.ContainsRune(c.CWD, 0) {
		return fmt.Errorf("cwd must be relative to the Worker workspace")
	}
	for _, part := range strings.Split(c.CWD, "/") {
		if part == ".." {
			return fmt.Errorf("cwd must stay inside the Worker workspace")
		}
	}
	return nil
}
