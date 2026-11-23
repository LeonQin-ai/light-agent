#include "video_seedance.h"
#include "infra/util.h"
#include "os/http.h"
#include "cJSON.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SEEDANCE_DEFAULT "https://ark.cn-beijing.volces.com/api/v3"
#define SEEDANCE_TASKS "/contents/generations/tasks"

static int fail(char **error, int status, const char *message) {
    if (error) *error = xstrdup(message);
    return status;
}

/* Base prefixes include /api/v3 and service-plan-specific paths. Pass the
 * origin and complete API path separately for every platform's HTTP backend. */
static int seedance_endpoint(const route *rt, char origin[256], char path[1024],
                            const char *task_id) {
    const char *base = rt->base_url && *rt->base_url ? rt->base_url : SEEDANCE_DEFAULT;
    const char *authority;
    if (!strncmp(base, "https://", 8)) authority = base + 8;
    else if (!strncmp(base, "http://127.0.0.1:", 17)) authority = base + 7;
    else return -1;
    const char *prefix = authority + strcspn(authority, "/");
    if (prefix == authority || prefix - base >= 256 ||
        strpbrk(authority, "@?#\\\r\n\t ")) return -1;
    snprintf(origin, 256, "%.*s", (int)(prefix - base), base);
    size_t length = strlen(prefix);
    while (length && prefix[length - 1] == '/') length--;
    if (!length) { prefix = "/api/v3"; length = strlen(prefix); }
    if (task_id) {
        if (!*task_id || strlen(task_id) >= 128) return -1;
        for (const unsigned char *c = (const unsigned char *)task_id; *c; c++)
            if (!isalnum(*c) && *c != '-' && *c != '_') return -1;
    }
    if (length >= 768) return -1;
    int n = snprintf(path, 1024, "%.*s%s%s%s", (int)length, prefix, SEEDANCE_TASKS,
                     task_id ? "/" : "", task_id ? task_id : "");
    return n < 0 || n >= 1024 ? -1 : 0;
}

static int seedance_headers(const route *rt, strmap *headers) {
    if (!rt->api_key || !*rt->api_key || strpbrk(rt->api_key, "\r\n")) return -1;
    strbuf authorization;
    strbuf_init(&authorization);
    strbuf_appendf(&authorization, "Bearer %s", rt->api_key);
    if (authorization.buf) strmap_set(headers, "Authorization", authorization.buf);
    strbuf_free(&authorization);
    return strmap_get(headers, "Authorization") ? 0 : -1;
}

static int response_error(const http_response *response, int submit, char **error) {
    if (!response)
        return fail(error, -2, "Seedance API 网络请求失败");
    int status = response->status;
    if (status >= 200 && status < 300) return 0;
    int transient = status >= 500 || status == 408 || status == 429;
    char message[128];
    snprintf(message, sizeof message, "Seedance API HTTP %d%s", status,
             status == 401 || status == 403 ? "，请检查方舟 API Key 和模型授权" : "");
    return fail(error, transient ? -2 : (submit ? -1 : -3), message);
}

static const char *field(const cJSON *o, const char *key) {
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, key));
}

int video_seedance_submit(const route *rt, const char *prompt, int seconds,
                          char task_id[128], char **error) {
    char origin[256], path[1024];
    strmap headers = {0};
    if (!rt || !rt->model || !prompt || !*prompt || (seconds != 3 && seconds != 4) ||
        seedance_endpoint(rt, origin, path, NULL) != 0 || seedance_headers(rt, &headers) != 0) {
        strmap_free(&headers);
        return fail(error, -1, "请配置 Seedance 模型、方舟 API 地址和 API Key");
    }
    cJSON *body = cJSON_CreateObject();
    cJSON *content = body ? cJSON_AddArrayToObject(body, "content") : NULL;
    cJSON *text = cJSON_CreateObject();
    int ok = content && text && cJSON_AddStringToObject(text, "type", "text") &&
        cJSON_AddStringToObject(text, "text", prompt) && cJSON_AddItemToArray(content, text);
    if (!ok) cJSON_Delete(text);
    ok = ok && cJSON_AddStringToObject(body, "model", rt->model) &&
        cJSON_AddNumberToObject(body, "duration", 4) &&
        cJSON_AddStringToObject(body, "ratio", "9:16") &&
        cJSON_AddStringToObject(body, "resolution", "480p") &&
        cJSON_AddBoolToObject(body, "generate_audio", 0);
    char *json = ok ? cJSON_PrintUnformatted(body) : NULL;
    cJSON_Delete(body);
    if (!json) {
        strmap_free(&headers);
        return fail(error, -1, "无法构建 Seedance 请求");
    }
    http_response *response = http_post(origin, path, json, "application/json", &headers, 30000);
    free(json);
    strmap_free(&headers);
    int rc = response_error(response, 1, error);
    if (rc == 0) {
        cJSON *reply = response->body ? cJSON_ParseWithOpts(response->body, NULL, 1) : NULL;
        const char *id = field(reply, "id");
        if (!id || seedance_endpoint(rt, origin, path, id) != 0) {
            rc = fail(error, -2, "Seedance 未返回可用任务编号，请核对服务商控制台");
        } else {
            snprintf(task_id, 128, "%s", id);
        }
        cJSON_Delete(reply);
    }
    http_response_free(response);
    return rc;
}

int video_seedance_poll(const route *rt, const char *task_id, char **url, char **error) {
    char origin[256], path[1024];
    strmap headers = {0};
    if (!rt || !task_id || seedance_endpoint(rt, origin, path, task_id) != 0 ||
        seedance_headers(rt, &headers) != 0) {
        strmap_free(&headers);
        return fail(error, -3, "Seedance 路由或任务编号无效");
    }
    http_response *response = http_get(origin, path, &headers, 30000);
    strmap_free(&headers);
    int rc = response_error(response, 0, error);
    if (rc == 0) {
        cJSON *reply = response->body ? cJSON_ParseWithOpts(response->body, NULL, 1) : NULL;
        const char *status = field(reply, "status");
        if (status && (!strcmp(status, "queued") || !strcmp(status, "running"))) {
            rc = 0;
        } else if (status && !strcmp(status, "succeeded")) {
            const char *video = field(cJSON_GetObjectItemCaseSensitive(reply, "content"), "video_url");
            if (video && *video && (*url = xstrdup(video))) rc = 1;
            else rc = fail(error, -3, "Seedance 已完成但未返回视频地址");
        } else if (status && (!strcmp(status, "expired") || !strcmp(status, "failed") ||
                              !strcmp(status, "cancelled") || !strcmp(status, "canceled"))) {
            rc = fail(error, -1, "Seedance 视频任务失败、取消或超时");
        } else {
            rc = fail(error, -3, "Seedance 任务状态未知，已保留编号");
        }
        cJSON_Delete(reply);
    }
    http_response_free(response);
    return rc;
}
