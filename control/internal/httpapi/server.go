package httpapi

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/LeonQin-ai/light-agent/control/internal/lifecycle"
	"github.com/LeonQin-ai/light-agent/control/internal/model"
	"github.com/LeonQin-ai/light-agent/control/internal/store"
)

type Server struct {
	store store.Store
	manager *lifecycle.Manager
	apiToken string
	mux *http.ServeMux
}

func New(st store.Store, manager *lifecycle.Manager, feishuWebhook http.Handler, apiToken string) *Server {
	s:=&Server{store:st,manager:manager,apiToken:apiToken,mux:http.NewServeMux()}
	s.mux.HandleFunc("GET /healthz",s.health)
	s.mux.HandleFunc("POST /v1/agents",s.createAgent)
	s.mux.HandleFunc("GET /v1/agents/{id}",s.getAgent)
	s.mux.HandleFunc("GET /v1/agents/{id}/runtime",s.getRuntime)
	s.mux.HandleFunc("POST /v1/agents/{id}/wake",s.wakeAgent)
	s.mux.HandleFunc("POST /v1/agents/{id}/sleep",s.sleepAgent)
	s.mux.HandleFunc("POST /v1/agents/{id}/messages",s.submitMessage)
	s.mux.HandleFunc("GET /v1/tasks/{id}",s.getTask)
	s.mux.HandleFunc("POST /v1/tasks/{id}/cancel",s.cancelTask)
	if feishuWebhook!=nil { s.mux.Handle("POST /webhooks/feishu",feishuWebhook) }
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter,r *http.Request) {
	if strings.HasPrefix(r.URL.Path,"/v1/") && s.apiToken!="" {
		authorization:=r.Header.Get("Authorization")
		if !strings.HasPrefix(authorization,"Bearer ") {
			writeError(w,http.StatusUnauthorized,"unauthorized","valid bearer token required")
			return
		}
		provided:=strings.TrimSpace(strings.TrimPrefix(authorization,"Bearer "))
		if len(provided)!=len(s.apiToken)||subtle.ConstantTimeCompare([]byte(provided),[]byte(s.apiToken))!=1 {
			writeError(w,http.StatusUnauthorized,"unauthorized","valid bearer token required")
			return
		}
	}
	s.mux.ServeHTTP(w,r)
}

func(s *Server)health(w http.ResponseWriter,_ *http.Request){writeJSON(w,200,map[string]string{"status":"ok"})}

func(s *Server)createAgent(w http.ResponseWriter,r *http.Request){
	var req struct{TenantID string `json:"tenant_id"`;Role string `json:"role"`}
	if err:=decodeJSON(w,r,&req);err!=nil{writeError(w,400,"invalid_request",err.Error());return}
	agent,err:=s.store.CreateAgent(r.Context(),strings.TrimSpace(req.TenantID),strings.TrimSpace(req.Role))
	if err!=nil{writeStoreError(w,err);return}
	writeJSON(w,201,agent)
}
func(s *Server)getAgent(w http.ResponseWriter,r *http.Request){
	agent,err:=s.store.GetAgent(r.Context(),r.PathValue("id"));if err!=nil{writeStoreError(w,err);return}
	writeJSON(w,200,agent)
}
func(s *Server)getRuntime(w http.ResponseWriter,r *http.Request){
	runtime,err:=s.store.GetRuntime(r.Context(),r.PathValue("id"));if err!=nil{writeStoreError(w,err);return}
	writeJSON(w,200,runtime)
}
func(s *Server)wakeAgent(w http.ResponseWriter,r *http.Request){
	if s.manager==nil{writeError(w,503,"runtime_driver_unavailable","no trusted SuperAgent runtime driver is configured");return}
	runtime,err:=s.manager.Wake(r.Context(),r.PathValue("id"));if err!=nil{writeLifecycleError(w,err);return}
	status:=http.StatusOK
	if runtime.State==model.RuntimeStarting{status=http.StatusAccepted}
	writeJSON(w,status,runtime)
}
func(s *Server)sleepAgent(w http.ResponseWriter,r *http.Request){
	if s.manager==nil{writeError(w,503,"runtime_driver_unavailable","no trusted SuperAgent runtime driver is configured");return}
	runtime,err:=s.manager.Sleep(r.Context(),r.PathValue("id"));if err!=nil{writeLifecycleError(w,err);return}
	status:=http.StatusOK
	if runtime.State==model.RuntimeStopping{status=http.StatusAccepted}
	writeJSON(w,status,runtime)
}
func(s *Server)submitMessage(w http.ResponseWriter,r *http.Request){
	var req struct{RequestID string `json:"request_id"`;Content string `json:"content"`}
	if err:=decodeJSON(w,r,&req);err!=nil{writeError(w,400,"invalid_request",err.Error());return}
	req.Content=strings.TrimSpace(req.Content);if req.Content==""{writeError(w,400,"invalid_request","content is required");return}
	if req.RequestID==""{req.RequestID=r.Header.Get("Idempotency-Key")}
	payload,_:=json.Marshal(map[string]string{"source":"api","content":req.Content})
	task,err:=s.store.CreateTask(r.Context(),r.PathValue("id"),req.RequestID,string(payload))
	if err!=nil{writeStoreError(w,err);return}
	writeJSON(w,202,task)
}
func(s *Server)getTask(w http.ResponseWriter,r *http.Request){task,err:=s.store.GetTask(r.Context(),r.PathValue("id"));if err!=nil{writeStoreError(w,err);return};writeJSON(w,200,task)}
func(s *Server)cancelTask(w http.ResponseWriter,r *http.Request){task,err:=s.store.CancelTask(r.Context(),r.PathValue("id"));if err!=nil{writeStoreError(w,err);return};writeJSON(w,200,task)}

func decodeJSON(w http.ResponseWriter,r *http.Request,dst any)error{
	r.Body=http.MaxBytesReader(w,r.Body,1<<20);dec:=json.NewDecoder(r.Body);dec.DisallowUnknownFields()
	if err:=dec.Decode(dst);err!=nil{return err}
	if err:=dec.Decode(&struct{}{});!errors.Is(err,io.EOF){return errors.New("request must contain exactly one JSON object")}
	return nil
}
func writeStoreError(w http.ResponseWriter,err error){
	switch{case errors.Is(err,store.ErrNotFound):writeError(w,404,"not_found","resource not found")
	case errors.Is(err,store.ErrConflict):writeError(w,409,"state_conflict","resource state changed")
	default:writeError(w,500,"internal_error","internal storage error")}
}
func writeLifecycleError(w http.ResponseWriter,err error){
	if errors.Is(err,lifecycle.ErrDriverUnavailable){writeError(w,503,"runtime_driver_unavailable",err.Error());return}
	writeStoreError(w,err)
}
func writeError(w http.ResponseWriter,status int,code,message string){writeJSON(w,status,map[string]string{"error":code,"message":message})}
func writeJSON(w http.ResponseWriter,status int,value any){w.Header().Set("Content-Type","application/json");w.WriteHeader(status);_=json.NewEncoder(w).Encode(value)}
func HTTPServer(addr string,handler http.Handler)*http.Server{return &http.Server{Addr:addr,Handler:handler,ReadHeaderTimeout:5*time.Second,ReadTimeout:15*time.Second,WriteTimeout:30*time.Second,IdleTimeout:60*time.Second}}
