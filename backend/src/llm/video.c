#include "llm/video.h"
#include "video_seedance.h"
#include "api/websocket.h"
#include "infra/util.h"
#include "llm/image.h"
#include "os/http.h"
#include "os/os_fs.h"
#include "os/os_time.h"
#include "cJSON.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define VIDEO_MAX_BYTES (32u * 1024u * 1024u)
#define VIDEO_IMAGE_MAX_BYTES (4u * 1024u * 1024u)
#define VIDEO_MEDIA_MAX_BYTES (12u * 1024u * 1024u)
#define VIDEO_SUBMIT "/api/v1/services/aigc/video-generation/video-synthesis"
#define VIDEO_QUERY "/api/v1/tasks/"

static int fail(char **error, int rc, const char *message) {
    if (error) *error = xstrdup(message);
    return rc;
}

static int seedance_model(const route *rt) {
    return rt && rt->model && !strncmp(rt->model, "doubao-seedance-", sizeof "doubao-seedance-" - 1);
}

static int kling_model(const route *rt) {
    return rt && rt->model && !strcmp(rt->model, "kling/kling-v3-turbo-video-generation");
}

llm_video_input llm_video_input_kind(const route *rt) {
    if (rt && rt->model) {
        if (!strcmp(rt->model, "happyhorse-1.1-i2v")) return LLM_VIDEO_FIRST_FRAME;
        if (!strcmp(rt->model, "happyhorse-1.1-r2v")) return LLM_VIDEO_REFERENCE;
    }
    return LLM_VIDEO_TEXT;
}

static int suffix(const char *value, const char *end) {
    size_t n = strlen(value), k = strlen(end);
    return n > k && !strcmp(value + n - k, end);
}

static int split_url(const char *url, char origin[256], const char **path) {
    if (!url) return -1;
    int https = !strncmp(url, "https://", 8);
    int local = !strncmp(url, "http://127.0.0.1:", 17);
    if (!https && !local) return -1;
    const char *host = url + (https ? 8 : 7);
    const char *end = host + strcspn(host, "/");
    if (host == end || end - url >= 256 || strpbrk(url, "#\\\r\n\t "))
        return -1;
    for (const char *p = host; p < end; p++) {
        char c = *p;
        if (local && p >= host + strlen("127.0.0.1:")) {
            if (c < '0' || c > '9') return -1;
        } else if ((c < 'a' || c > 'z') && (c < '0' || c > '9') &&
                   c != '.' && c != '-' && !(local && c == ':')) {
            return -1;
        }
    }
    if (local && end == host + strlen("127.0.0.1:")) return -1;
    snprintf(origin, 256, "%.*s", (int)(end - url), url);
    if (path) *path = end;
    return 0;
}

static int ali_origin(const route *rt, char origin[256]) {
    if (split_url(rt ? rt->base_url : NULL, origin, NULL) != 0) return -1;
    if (!strncmp(origin, "http://127.0.0.1:", 17) ||
        suffix(origin, ".maas.aliyuncs.com") ||
        !strcmp(origin, "https://dashscope.aliyuncs.com") ||
        !strcmp(origin, "https://dashscope-intl.aliyuncs.com"))
        return 0;
    return -1;
}

static int valid_id(const char *id) {
    if (!id || !*id || strlen(id) >= 128) return 0;
    for (const char *p = id; *p; p++)
        if ((*p < 'a' || *p > 'z') && (*p < 'A' || *p > 'Z') &&
            (*p < '0' || *p > '9') && *p != '-' && *p != '_')
            return 0;
    return 1;
}

static int credentials(const route *rt, strmap *headers) {
    if (!rt->api_key || !*rt->api_key || strpbrk(rt->api_key, "\r\n")) return 0;
    strbuf value;
    strbuf_init(&value);
    strbuf_appendf(&value, "Bearer %s", rt->api_key);
    if (value.buf) strmap_set(headers, "Authorization", value.buf);
    strbuf_free(&value);
    return strmap_get(headers, "Authorization") != NULL;
}

static const char *field(const cJSON *value, const char *key) {
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(value, key));
}

static int provider_failure(const cJSON *details, char **error) {
    const char *code = field(details, "code");
    if (valid_id(code) && strlen(code) <= 64) {
        char message[108];
        snprintf(message, sizeof message, "视频服务报告任务失败（%s）", code);
        return fail(error, -1, message);
    }
    return fail(error, -1, "视频服务报告任务失败或已取消");
}

static int api_failure(const http_response *response, int submit, char **error) {
    if (!response)
        return fail(error, -2, submit ? "视频提交未收到响应，接收状态未知" : "查询视频任务网络失败");
    int status = response->status;
    if (status >= 200 && status < 300 && response->body && response->body_len < 65536)
        return 0;
    char message[128];
    snprintf(message, sizeof message, "视频 API HTTP %d%s", status,
             status == 401 || status == 403 ? "，请检查 API Key 和模型授权" : "");
    int rc = -2;
    if (submit && status >= 400 && status < 500 && status != 408) rc = -1;
    if (!submit && status < 500 && status != 408 && status != 429) rc = -3;
    return fail(error, rc, message);
}

static char *image_data_uri(const char *path, size_t *total) {
    if (!path || !*path) return NULL;
    int more = 0;
    size_t bytes = 0;
    char *data = fs_read_file_slice(path, 0, VIDEO_IMAGE_MAX_BYTES, &bytes, &more);
    const char *mime = data && !more && bytes <= VIDEO_MEDIA_MAX_BYTES - *total ?
        llm_image_mime((const unsigned char *)data, bytes) : NULL;
    char *encoded = mime ? base64_encode((const unsigned char *)data, bytes) : NULL;
    free(data);
    if (!encoded) return NULL;
    strbuf uri;
    strbuf_init(&uri);
    strbuf_appendf(&uri, "data:%s;base64,%s", mime, encoded);
    free(encoded);
    if (!uri.buf) return NULL;
    *total += bytes;
    return strbuf_detach(&uri);
}

static int append_media(cJSON *input, llm_video_input mode, const char *const *paths,
                        size_t count, const char *sample) {
    cJSON *items = cJSON_AddArrayToObject(input, "media");
    if (!items) return 0;
    const char *kind = mode == LLM_VIDEO_FIRST_FRAME ? "first_frame" : "reference_image";
    size_t total = 0;
    for (size_t i = 0; i < count; i++) {
        char *uri = sample ? NULL : image_data_uri(paths[i], &total);
        cJSON *item = cJSON_CreateObject();
        int ok = (sample || uri) && cJSON_AddStringToObject(item, "type", kind) &&
            cJSON_AddStringToObject(item, "url", sample ? sample : uri) &&
            cJSON_AddItemToArray(items, item);
        free(uri);
        if (!ok) {
            cJSON_Delete(item);
            return 0;
        }
    }
    return 1;
}

static int submit_video(const route *rt, const char *prompt, int seconds,
                        const char *const *media, size_t media_count, const char *sample,
                        char task_id[128], char **error) {
    if (error) *error = NULL;
    task_id[0] = 0;
    if (!route_is_video(rt) || !rt->model || !prompt || !*prompt || strlen(prompt) > 7500 ||
        (seconds != 3 && seconds != 4))
        return fail(error, -1, "请选择视频模型，填写文案和 3 或 4 秒的片段时长");
    llm_video_input mode = llm_video_input_kind(rt);
    if ((media_count && !media && !sample) ||
        (mode == LLM_VIDEO_TEXT && media_count) ||
        (mode == LLM_VIDEO_FIRST_FRAME && media_count != 1) ||
        (mode == LLM_VIDEO_REFERENCE && (!media_count || media_count > 9)))
        return fail(error, -1, "文生视频不接收图片，首帧生视频需要一张图片，参考生视频需要 1–9 张图片");
    if (seedance_model(rt)) return video_seedance_submit(rt, prompt, seconds, task_id, error);
    char origin[256];
    int kling = kling_model(rt);
    if ((!kling && mode == LLM_VIDEO_TEXT && strcmp(rt->model, "happyhorse-1.1-t2v")) ||
        ali_origin(rt, origin) != 0)
        return fail(error, -1, "该模型或地址不支持百炼视频 API");

    cJSON *body = cJSON_CreateObject();
    cJSON *input = cJSON_AddObjectToObject(body, "input");
    cJSON *parameters = cJSON_AddObjectToObject(body, "parameters");
    int valid = cJSON_AddStringToObject(body, "model", rt->model) &&
        cJSON_AddStringToObject(input, "prompt", prompt) &&
        cJSON_AddNumberToObject(parameters, "duration", seconds);
    if (valid && media_count && !append_media(input, mode, media, media_count, sample)) {
        cJSON_Delete(body);
        return fail(error, -1, "视频素材必须是完整的 PNG、JPEG 或 WebP，每张不超过 4 MiB，合计不超过 12 MiB");
    }
    if (valid && kling)
        valid = cJSON_AddStringToObject(parameters, "mode", "std") &&
                cJSON_AddStringToObject(parameters, "aspect_ratio", "9:16");
    else if (valid)
        valid = cJSON_AddStringToObject(parameters, "resolution", "480P") &&
                (mode == LLM_VIDEO_FIRST_FRAME || cJSON_AddStringToObject(parameters, "ratio", "9:16"));
    char *payload = valid ? cJSON_PrintUnformatted(body) : NULL;
    cJSON_Delete(body);
    strmap headers = {0};
    strmap_set(&headers, "X-DashScope-Async", "enable");
    if (!payload || !strmap_get(&headers, "X-DashScope-Async") || !credentials(rt, &headers)) {
        strmap_free(&headers);
        free(payload);
        return fail(error, -1, "无法准备视频请求，请检查路由 API Key");
    }
    http_response *response = http_post(origin, VIDEO_SUBMIT, payload, "application/json",
                                        &headers, media_count ? 60000 : 30000);
    strmap_free(&headers);
    free(payload);
    int result = api_failure(response, 1, error);
    if (result == 0) {
        cJSON *json = cJSON_ParseWithOpts(response->body, NULL, 1);
        const cJSON *output = cJSON_GetObjectItemCaseSensitive(json, "output");
        const char *id = field(output, "task_id");
        if (valid_id(id)) snprintf(task_id, 128, "%s", id);
        else result = fail(error, -2, "视频服务未返回任务 ID，接收状态未知");
        cJSON_Delete(json);
    }
    http_response_free(response);
    return result;
}

int llm_video_submit_media(const route *rt, const char *prompt, int seconds,
                           const char *const *media, size_t media_count,
                           char task_id[128], char **error) {
    return submit_video(rt, prompt, seconds, media, media_count, NULL, task_id, error);
}

int llm_video_submit(const route *rt, const char *prompt, int seconds,
                     char task_id[128], char **error) {
    return llm_video_submit_media(rt, prompt, seconds, NULL, 0, task_id, error);
}

int llm_video_poll(const route *rt, const char *task_id, char **url, char **error) {
    *url = NULL;
    if (error) *error = NULL;
    if (!route_is_video(rt) || !valid_id(task_id))
        return fail(error, -3, "无效的视频任务 ID 或路由");
    if (seedance_model(rt)) return video_seedance_poll(rt, task_id, url, error);
    char origin[256], path[sizeof VIDEO_QUERY + 128];
    if (ali_origin(rt, origin) != 0)
        return fail(error, -3, "无效的百炼视频服务地址");
    strmap headers = {0};
    if (!credentials(rt, &headers)) {
        strmap_free(&headers);
        return fail(error, -3, "无法查询视频任务，请检查路由 API Key");
    }
    snprintf(path, sizeof path, "%s%s", VIDEO_QUERY, task_id);
    http_response *response = http_get(origin, path, &headers, 15000);
    strmap_free(&headers);
    int result = api_failure(response, 0, error);
    if (result == 0) {
        cJSON *json = cJSON_ParseWithOpts(response->body, NULL, 1);
        const cJSON *output = cJSON_GetObjectItemCaseSensitive(json, "output");
        const char *status = field(output, "task_status");
        const char *reported_id = field(output, "task_id");
        if (reported_id && strcmp(reported_id, task_id)) {
            result = fail(error, -3, "视频服务返回的任务 ID 不匹配");
        } else if (status && !strcmp(status, "SUCCEEDED")) {
            const char *download = field(output, "video_url");
            if (download && *download) *url = xstrdup(download);
            result = *url ? 1 : fail(error, -3, "视频任务完成但未返回下载链接");
        } else if (status && (!strcmp(status, "FAILED") || !strcmp(status, "CANCELED"))) {
            result = provider_failure(output, error);
        } else if (!status || (strcmp(status, "PENDING") && strcmp(status, "RUNNING"))) {
            result = fail(error, -3, "视频任务状态未知或任务 ID 已过期");
        }
        cJSON_Delete(json);
    }
    http_response_free(response);
    return result;
}

static uint32_t read_be(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

/* Validate box boundaries and require a video handler within a track. This
 * verifies the transport; the editor separately decodes the actual frames. */
static int box_next(const unsigned char *data, size_t n, size_t *offset,
                    const unsigned char **type, const unsigned char **content, size_t *length) {
    size_t remaining = n - *offset, header = 8;
    if (remaining < header) return 0;
    const unsigned char *p = data + *offset;
    uint64_t bytes = read_be(p);
    if (bytes == 1) {
        header = 16;
        if (remaining < header) return 0;
        bytes = (uint64_t)read_be(p + 8) << 32 | read_be(p + 12);
    } else if (!bytes) bytes = remaining;
    if (bytes < header || bytes > remaining) return 0;
    *type = p + 4;
    *content = p + header;
    *length = (size_t)bytes - header;
    *offset += (size_t)bytes;
    return 1;
}

static int video_track(const unsigned char *data, size_t n, unsigned level) {
    static const char hierarchy[][5] = {"trak", "mdia", "hdlr"};
    int present = 0;
    for (size_t offset = 0; offset < n;) {
        const unsigned char *type, *content;
        size_t bytes;
        if (!box_next(data, n, &offset, &type, &content, &bytes)) return 0;
        if (memcmp(type, hierarchy[level], 4)) continue;
        if (level == 2) present |= bytes >= 12 && !memcmp(content + 8, "vide", 4);
        else present |= video_track(content, bytes, level + 1);
    }
    return present;
}

static int mp4_valid(const unsigned char *data, size_t n) {
    int ftyp = 0, moov = 0, mdat = 0;
    for (size_t offset = 0; offset < n;) {
        const unsigned char *type, *content;
        size_t bytes;
        if (!box_next(data, n, &offset, &type, &content, &bytes)) return 0;
        if (!memcmp(type, "ftyp", 4)) ftyp |= bytes >= 8;
        if (!memcmp(type, "moov", 4)) moov |= video_track(content, bytes, 0);
        if (!memcmp(type, "mdat", 4)) mdat |= bytes > 0;
    }
    return ftyp && moov && mdat;
}

int llm_video_validate_file(const char *path) {
    size_t bytes = 0;
    int more = 0;
    char *data = fs_read_file_slice(path, 0, VIDEO_MAX_BYTES, &bytes, &more);
    int valid = data && !more && mp4_valid((const unsigned char *)data, bytes);
    free(data);
    return valid;
}

static int trusted_download(const route *rt, const char *origin) {
    char provider[256];
    if (split_url(rt ? rt->base_url : NULL, provider, NULL) == 0 && !strcmp(origin, provider))
        return 1;
    if (strncmp(origin, "https://", 8)) return 0;
    if (seedance_model(rt)) {
        const char *host = origin + 8, *bucket = strstr(host, ".tos-");
        return bucket && bucket != host && suffix(origin, ".volces.com");
    }
    if (kling_model(rt) && suffix(origin, ".kechuangai.com")) return 1;
    return suffix(origin, ".aliyuncs.com");
}

static int video_receive(const route *rt, const char *url, strbuf *body, char **error) {
    if (error) *error = NULL;
    char origin[256];
    const char *request;
    if (!route_is_video(rt) || split_url(url, origin, &request) != 0 || !*request ||
        strlen(request) >= 1536 || !trusted_download(rt, origin))
        return fail(error, -1, "视频服务返回了不支持的下载地址");
    strmap headers = {0};
    strmap_set(&headers, "Accept", "video/mp4");
    /* Use a new request with no model credentials, even when the URL is
     * served by the provider itself. */
    char buffer[16384];
    const char *message = "视频不完整或没有有效 MP4 视频轨，可沿用现有任务 ID 重试";
    for (int attempt = 0; attempt < 2; attempt++) {
        http_stream *stream = http_stream_open(origin, "GET", request, NULL, NULL, &headers, 60000);
        if (!stream || http_stream_status(stream) != 200) {
            if (stream) http_stream_close(stream);
            message = "下载视频失败，可沿用现有任务 ID 重试";
            break;
        }
        int n;
        int read_ok = 1;
        while ((n = http_stream_read(stream, buffer, sizeof buffer)) > 0) {
            size_t expected = body->len + (size_t)n;
            if (expected > VIDEO_MAX_BYTES) {
                message = "视频超过 32 MiB 下载上限";
                read_ok = 0;
                break;
            }
            strbuf_append_n(body, buffer, (size_t)n);
            if (body->len != expected) {
                message = "无法分配视频下载缓冲区";
                read_ok = 0;
                break;
            }
        }
        http_stream_close(stream);
        if (!read_ok) break;
        if (n == 0 && mp4_valid((const unsigned char *)body->buf, body->len)) {
            strmap_free(&headers);
            return 0;
        }
        /* A CDN can close a 200 response before delivering the complete MP4. */
        strbuf_free(body);
        strbuf_init(body);
    }
    strmap_free(&headers);
    return fail(error, -1, message);
}

int llm_video_download(const route *rt, const char *url, const char *path, char **error) {
    strbuf body;
    strbuf_init(&body);
    if (video_receive(rt, url, &body, error) != 0) {
        strbuf_free(&body);
        return -1;
    }
    static atomic_uint sequence;
    char staged[4096];
    int length = snprintf(staged, sizeof staged, "%s.video-%lld-%u.tmp", path,
                          (long long)time_now_us(), atomic_fetch_add(&sequence, 1));
    int rc = -1;
    if (length > 0 && length < (int)sizeof staged) {
        if (fs_write_file(staged, body.buf, body.len) == 0 &&
            fs_file_size(staged) == (long long)body.len && fs_replace_file(staged, path) == 0)
            rc = 0;
        if (rc != 0) fs_remove(staged);
    }
    strbuf_free(&body);
    return rc == 0 ? 0 : fail(error, -1, "无法原子写入视频片段");
}

int llm_video_probe(const route *rt, size_t *bytes, char **error) {
    /* A small baked-in 450x800 horizon scene avoids uploading a user's file
     * merely to test the model route. Both axes exceed the R2V minimum. */
    static const char horizon[] =
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAcIAAAMgAQMAAACKxfCmAAAABlBMVEWTvOREbVEJE7/MAAAAYElE"
        "QVR42u3LsQ0AAAgDoP7/tP7QzQg7CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADww"
        "LdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0zdtzAfH9Xg2o/2tSAAAAAElFTkSuQmCC";
    *bytes = 0;
    char id[128];
    llm_video_input mode = llm_video_input_kind(rt);
    const char *prompt = mode == LLM_VIDEO_REFERENCE ?
        "Use the blue sky and green horizon from [Image 1]. A small white cloud drifts past." :
        "A white cloud drifts slowly across a clear blue sky, static camera.";
    int rc = submit_video(rt, prompt, seedance_model(rt) ? 4 : 3, NULL,
                          mode != LLM_VIDEO_TEXT, mode != LLM_VIDEO_TEXT ? horizon : NULL, id, error);
    if (rc != 0) return -1;
    char *url = NULL;
    int64_t deadline = time_now_ms() + 360000;
    do {
        rc = llm_video_poll(rt, id, &url, error);
        if (rc != 0 && rc != -2) break;
        if (error) { free(*error); *error = NULL; }
        time_sleep_ms(!strncmp(rt->base_url ? rt->base_url : "", "http://127.0.0.1:", 17) ? 50 : 5000);
    } while (time_now_ms() < deadline);
    if (rc != 1) {
        if (rc == 0 || rc == -2)
            return fail(error, -1, "视频测试等待超时，请在服务商控制台查看任务");
        return -1;
    }
    strbuf body;
    strbuf_init(&body);
    rc = video_receive(rt, url, &body, error);
    if (rc == 0) *bytes = body.len;
    strbuf_free(&body);
    free(url);
    return rc;
}
