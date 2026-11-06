package execution

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type WorkerSpec struct {
	WorkerID string `json:"worker_id"`
	WorkspaceID string `json:"workspace_id"`
	CPUMillis uint32 `json:"cpu_millis"`
	MemoryBytes uint64 `json:"memory_bytes"`
	PidsLimit uint64 `json:"pids_limit"`
}
type ExecSpec struct {
	Executable string `json:"executable"`
	Argv []string `json:"argv"`
	CWD string `json:"cwd"`
	TimeoutMS uint64 `json:"timeout_ms"`
	MemoryBytes uint64 `json:"memory_bytes"`
	MaxOutputBytes uint64 `json:"max_output_bytes"`
	Network bool `json:"network"`
}
type WorkerRecord struct {
	ContainerID string `json:"container_id"`
	State string `json:"state"`
}
type DockerWorkerClient struct {
	baseURL string
	token string
	http *http.Client
}
func NewDockerWorkerClient(baseURL, token string) (*DockerWorkerClient,error) {
	u,err:=url.Parse(strings.TrimRight(baseURL,"/"))
	if err!=nil || (u.Scheme!="http"&&u.Scheme!="https") || u.Host=="" { return nil,fmt.Errorf("invalid Sandbox Manager URL") }
	if strings.TrimSpace(token)=="" { return nil,fmt.Errorf("Sandbox Manager token is required") }
	if u.Scheme=="http" {
		host:=u.Hostname()
		if host!="localhost" { return nil,fmt.Errorf("plain HTTP Sandbox Manager URL must be loopback") }
	}
	return &DockerWorkerClient{baseURL:strings.TrimRight(baseURL,"/"),token:token,http:&http.Client{Timeout:6*time.Minute,CheckRedirect:func(*http.Request,[]*http.Request)error{return http.ErrUseLastResponse}}},nil
}
func(c *DockerWorkerClient) request(ctx context.Context,method,path string,body any,out any)error{
	var data []byte
	var err error
	if body!=nil { data,err=json.Marshal(body);if err!=nil{return err} }
	req,err:=http.NewRequestWithContext(ctx,method,c.baseURL+path,bytes.NewReader(data));if err!=nil{return err}
	req.Header.Set("Authorization","Bearer "+c.token)
	if body!=nil { req.Header.Set("Content-Type","application/json") }
	resp,err:=c.http.Do(req);if err!=nil{return fmt.Errorf("Sandbox Manager request: %w",err)}
	defer resp.Body.Close()
	if resp.StatusCode<200||resp.StatusCode>=300 { return fmt.Errorf("Sandbox Manager %s %s returned HTTP %d",method,path,resp.StatusCode) }
	if out!=nil { if err:=json.NewDecoder(resp.Body).Decode(out);err!=nil{return fmt.Errorf("decode Sandbox Manager response: %w",err)} }
	return nil
}
func(c *DockerWorkerClient) Create(ctx context.Context,s WorkerSpec)(WorkerRecord,error){
	var r WorkerRecord;err:=c.request(ctx,http.MethodPost,"/v1/containers",s,&r);return r,err
}
func(c *DockerWorkerClient) Start(ctx context.Context,id string)error{return c.request(ctx,http.MethodPost,"/v1/containers/"+url.PathEscape(id)+"/start",nil,nil)}
func(c *DockerWorkerClient) Exec(ctx context.Context,id string,s ExecSpec)(json.RawMessage,error){
	var result json.RawMessage
	err:=c.request(ctx,http.MethodPost,"/v1/containers/"+url.PathEscape(id)+"/exec",s,&result)
	return result,err
}
func(c *DockerWorkerClient) Stop(ctx context.Context,id string)error{return c.request(ctx,http.MethodPost,"/v1/containers/"+url.PathEscape(id)+"/cancel",nil,nil)}
func(c *DockerWorkerClient) Destroy(ctx context.Context,id string)error{return c.request(ctx,http.MethodDelete,"/v1/containers/"+url.PathEscape(id),nil,nil)}

type RunRequest struct {
	Mode Mode
	Policy TaskPolicy
	Host Snapshot
	Worker WorkerSpec
	Command ExecSpec
}
type RunResult struct { Decision Decision `json:"decision"`; WorkerID string `json:"worker_id,omitempty"`; Result json.RawMessage `json:"result,omitempty"` }
type Scheduler struct { docker *DockerWorkerClient }
func NewScheduler(docker *DockerWorkerClient)*Scheduler{return &Scheduler{docker:docker}}
func(s *Scheduler) Run(ctx context.Context,req RunRequest)(RunResult,error){
	decision:=Resolve(req.Mode,req.Policy,req.Host)
	result:=RunResult{Decision:decision}
	if !decision.Allowed{return result,fmt.Errorf("%s: %s",decision.ErrorCode,decision.Reason)}
	if decision.Backend!=BackendDocker{return result,fmt.Errorf("backend %q is selected but no provider is wired in this build",decision.Backend)}
	if s==nil||s.docker==nil{return result,fmt.Errorf("Docker Worker provider is not configured")}
	worker,err:=s.docker.Create(ctx,req.Worker);if err!=nil{return result,fmt.Errorf("create Worker: %w",err)}
	result.WorkerID=worker.ContainerID
	defer func(){cleanupCtx,cancel:=context.WithTimeout(context.Background(),20*time.Second);defer cancel();_ = s.docker.Destroy(cleanupCtx,worker.ContainerID)}()
	if err=s.docker.Start(ctx,worker.ContainerID);err!=nil{return result,fmt.Errorf("start Worker: %w",err)}
	output,err:=s.docker.Exec(ctx,worker.ContainerID,req.Command);if err!=nil{return result,fmt.Errorf("execute Worker command: %w",err)}
	result.Result=output
	return result,nil
}
