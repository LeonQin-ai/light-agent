package execution

import (
 "context"
 "encoding/json"
 "net/http"
 "net/http/httptest"
 "testing"
 "strings"
)
func TestDockerWorkerClientLifecycleProtocol(t *testing.T){
 var calls []string
 server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){
  if r.Header.Get("Authorization")!="Bearer token"{t.Errorf("missing auth")}
  calls=append(calls,r.Method+" "+r.URL.Path)
  w.Header().Set("Content-Type","application/json")
  switch r.URL.Path {
  case "/v1/containers": w.WriteHeader(http.StatusCreated);_,_=w.Write([]byte(`{"container_id":"abc123","state":"CREATED"}`))
  case "/v1/containers/abc123/exec": _,_=w.Write([]byte(`{"stdout":"ok"}`))
  default: w.WriteHeader(http.StatusOK)
  }
 }))
 defer server.Close()
 c,err:=NewDockerWorkerClient(server.URL,"token");if err!=nil{t.Fatal(err)}
 ctx:=context.Background()
 w,err:=c.Create(ctx,WorkerSpec{WorkerID:"w1",WorkspaceID:"task1"});if err!=nil{t.Fatal(err)}
 if err=c.Start(ctx,w.ContainerID);err!=nil{t.Fatal(err)}
 out,err:=c.Exec(ctx,w.ContainerID,ExecSpec{Executable:"/bin/true"});if err!=nil{t.Fatal(err)}
 var got map[string]string;if err=json.Unmarshal(out,&got);err!=nil||got["stdout"]!="ok"{t.Fatalf("unexpected output %s %v",out,err)}
 if err=c.Stop(ctx,w.ContainerID);err!=nil{t.Fatal(err)}
 if err=c.Destroy(ctx,w.ContainerID);err!=nil{t.Fatal(err)}
 if len(calls)!=5{t.Fatalf("expected 5 calls, got %v",calls)}
}
func TestDockerWorkerClientRejectsRemotePlainHTTP(t *testing.T){
 if _,err:=NewDockerWorkerClient("http://10.0.0.2:9090","token");err==nil{t.Fatal("expected non-loopback HTTP rejection")}
}

func TestDockerWorkerClientReportsStructuredManagerErrors(t *testing.T) {
 server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){
  w.WriteHeader(http.StatusInternalServerError)
  _,_=w.Write([]byte(`{"error":"container mount denied"}`))
 }))
 defer server.Close()
 client,err:=NewDockerWorkerClient(server.URL,"private-token")
 if err!=nil {t.Fatal(err)}
 err=client.Start(context.Background(),"worker")
 if err==nil||!strings.Contains(err.Error(),"container mount denied")||strings.Contains(err.Error(),"private-token") {t.Fatalf("unexpected error: %v",err)}
}
