#include "action/tools.h"
#include "action/credential_adapter.h"
#include "infra/util.h"
#include "os/os_time.h"
#include "os/os_proc.h"
#include "cJSON.h"
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#ifdef _WIN32
#include <windows.h>
#endif

/* Search only the administrator/user configured installation paths. Never
 * resolve an executable or Python module from the task workspace. */
static const char *broker_python(char *path, size_t size) {
    const char *override = getenv("LIGHT_AGENT_CREDENTIAL_PYTHON");
#ifdef _WIN32
    if (override && isalpha((unsigned char)override[0]) && override[1]==':' &&
        (override[2]=='\\' || override[2]=='/')) return override;
    const char *paths=getenv("PATH");
    if (!paths || !*paths) return NULL;
    DWORD length=SearchPathA(paths,"python.exe",NULL,(DWORD)size,path,NULL);
    if (!length || length>=size) return NULL;
    if (!isalpha((unsigned char)path[0]) || path[1]!=':' || path[2]!='\\') return NULL;
    return path;
#else
    (void)path; (void)size;
    return override && override[0]=='/' ? override : "/usr/bin/python3";
#endif
}

char *credential_dispatch(const char *action, const char *root, const char *session,
                          const char *kind, const char *target, const char *user,
                          const char *secret, const char *reference,const char *workspace,const char *query) {
    char path[4096]={0};
    const char *python=broker_python(path,sizeof path);
    if (!python || !root || !session || !*session) return NULL;
    cJSON *o=cJSON_CreateObject();
    const char *keys[]={"action","root","session","kind","target","user","secret","reference","workspace","query"};
    const char *values[]={action,root,session,kind,target,user,secret,reference,workspace,query};
    for (size_t i=0;i<sizeof keys/sizeof keys[0];i++) {
        if (values[i] && !cJSON_AddStringToObject(o,keys[i],values[i])) {
            cJSON_Delete(o); return NULL;
        }
    }
    char *wire=cJSON_PrintUnformatted(o);
    cJSON_Delete(o);
    if (!wire || strlen(wire)>3500) { ssh_vault_secret_free(wire); return NULL; }
    char *argv[]={(char *)python,"-I","-c",(char *)credential_adapter,"--broker",NULL};
    proc_popen *proc=proc_popen_new_direct(argv,0);
    int sent=proc && !proc_popen_write(proc,wire,strlen(wire)) && !proc_popen_write(proc,"\n",1);
    ssh_vault_secret_free(wire);
    if (!sent) { if(proc) proc_popen_free(proc); return NULL; }
    int64_t end=time_now_ms()+110000;
    while (proc_popen_alive(proc) && time_now_ms()<end && strlen(proc_popen_buffer(proc))<65536)
        proc_popen_read(proc,50);
    char *result=NULL;
    if (!proc_popen_alive(proc)) {
        while (proc_popen_read(proc,1) && strlen(proc_popen_buffer(proc))<65536) {}
        cJSON *reply=cJSON_Parse(proc_popen_buffer(proc));
        if (cJSON_IsObject(reply)) result=cJSON_PrintUnformatted(reply);
        cJSON_Delete(reply);
    }
    proc_popen_free(proc);
    return result;
}

static tool_result *credential_execute(const tool *self,const tool_ctx *ctx,const char *args) {
    (void)self;
    cJSON *o=cJSON_Parse(args);
    const char *ref=cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o,"reference"));
    int authorized=ctx && ctx->credential_strict && ref && strlen(ref)==37 &&
        !strncmp(ref,"cred_",5) && strspn(ref+5,"0123456789abcdef")==32 &&
        ctx->task_input && strstr(ctx->task_input,ref);
    char *reply=authorized ? credential_dispatch("use",ctx->state_root,ctx->session_id,
        NULL,NULL,NULL,NULL,ref,ctx->workspace,NULL) : NULL;
    cJSON_Delete(o);
    tool_result *result=tool_result_new(reply!=NULL,reply ? reply :
        "Credential unavailable: use a reference authorized for this task in strict mode.");
    free(reply);
    return result;
}

const tool *tool_credential_use(void) {
    static const tool t={"credential_use","Use the credential reference in the user's request for its authorized HTTPS GET, Git refs/clone or user-authorized PostgreSQL query.",
        "{\"type\":\"object\",\"properties\":{\"reference\":{\"type\":\"string\"}},\"required\":[\"reference\"]}",
        1,credential_execute,NULL};
    return &t;
}
