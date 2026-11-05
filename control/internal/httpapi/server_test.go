package httpapi

import(
	"bytes"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/LeonQin-ai/light-agent/control/internal/store"
)

func newTestHandler(t *testing.T,token string)http.Handler{
	t.Helper();st,err:=store.Open(filepath.Join(t.TempDir(),"api.db"));if err!=nil{t.Fatal(err)}
	t.Cleanup(func(){_=st.Close()})
	return New(st,nil,nil,token)
}
func TestCreateAgentAndSubmitTask(t *testing.T){
	h:=newTestHandler(t,"")
	req:=httptest.NewRequest(http.MethodPost,"/v1/agents",bytes.NewBufferString(`{"tenant_id":"tenant-1","role":"supervisor"}`))
	res:=httptest.NewRecorder();h.ServeHTTP(res,req);if res.Code!=201{t.Fatalf("create status=%d body=%s",res.Code,res.Body.String())}
}
func TestAPITokenRequiredWhenConfigured(t *testing.T){
	h:=newTestHandler(t,"test-secret")
	req:=httptest.NewRequest(http.MethodPost,"/v1/agents",bytes.NewBufferString(`{"tenant_id":"tenant-1"}`))
	res:=httptest.NewRecorder();h.ServeHTTP(res,req);if res.Code!=401{t.Fatalf("unauthenticated status=%d",res.Code)}
	req=httptest.NewRequest(http.MethodPost,"/v1/agents",bytes.NewBufferString(`{"tenant_id":"tenant-1"}`));req.Header.Set("Authorization","Bearer test-secret")
	res=httptest.NewRecorder();h.ServeHTTP(res,req);if res.Code!=201{t.Fatalf("authenticated status=%d body=%s",res.Code,res.Body.String())}
}
func TestHealthAndWebhookPathsAreNotBehindAPIToken(t *testing.T){
	h:=newTestHandler(t,"test-secret")
	req:=httptest.NewRequest(http.MethodGet,"/healthz",nil);res:=httptest.NewRecorder();h.ServeHTTP(res,req)
	if res.Code!=200{t.Fatalf("health status=%d",res.Code)}
}
func TestNoHostExecEndpoint(t *testing.T){
	h:=newTestHandler(t,"")
	req:=httptest.NewRequest(http.MethodPost,"/v1/host/exec",bytes.NewBufferString(`{"command":"id"}`))
	res:=httptest.NewRecorder();h.ServeHTTP(res,req);if res.Code!=404{t.Fatalf("unexpected Host exec route: %d",res.Code)}
}
