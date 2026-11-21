#include "llm/image.h"
#include "api/websocket.h"
#include "infra/util.h"
#include "os/http.h"
#include "cJSON.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define IMAGE_GENERATION_TIMEOUT_MS 120000
#define IMAGE_DOWNLOAD_TIMEOUT_MS 30000
#define IMAGE_MAX_BYTES (16u * 1024u * 1024u)
#define DASHSCOPE_GENERATION_PATH "/api/v1/services/aigc/multimodal-generation/generation"

static int image_fail(llm_image_result *out, const char *error) {
    out->error = xstrdup(error);
    return -1;
}

void llm_image_result_free(llm_image_result *out) {
    free(out->data);
    free(out->error);
    memset(out, 0, sizeof(*out));
}

/* Bound the authority and reject ambiguous URL delimiters. The native Token
 * Plan endpoint uses the same origin as its chat route, never a separate host. */
static char *image_origin(const char *url, const char **path) {
    const char *authority;
    if (!strncmp(url, "https://", 8))
        authority = url + 8;
    else if (!strncmp(url, "http://", 7))
        authority = url + 7;
    else
        return NULL;
    *path = authority + strcspn(authority, "/");
    size_t length = (size_t)(*path - url);
    if (*path == authority || length > 253)
        return NULL;
    for (const char *p = authority; p < *path; p++)
        if (strchr("@?#\\", *p) || (unsigned char)*p <= 32)
            return NULL;
    char *origin = malloc(length + 1);
    if (origin) {
        memcpy(origin, url, length);
        origin[length] = '\0';
    }
    return origin;
}

static int token_plan_origin(const char *origin) {
    return !strcmp(origin, "https://token-plan.cn-beijing.maas.aliyuncs.com") ||
           !strcmp(origin, "https://token-plan.ap-southeast-1.maas.aliyuncs.com");
}

static int image_size(const char *size, int native, char normalized[24]) {
    if (!size || !*size || !strcmp(size, "auto")) {
        normalized[0] = 0;
        return 0;
    }
    unsigned width = 0, height = 0;
    char separator, trailing;
    if (sscanf(size, "%4u%c%4u%c", &width, &separator, &height, &trailing) != 3 ||
        (separator != 'x' && separator != '*') || !width || !height ||
        width > 4096 || height > 4096)
        return -1;
    snprintf(normalized, 24, "%u%c%u", width, native ? '*' : 'x', height);
    return 0;
}

static char *image_request(const route *rt, const char *prompt, const char *size, int native) {
    cJSON *body = cJSON_CreateObject();
    if (!body)
        return NULL;
    int ok = cJSON_AddStringToObject(body, "model", rt->model) != NULL;
    if (ok && native) {
        cJSON *input = cJSON_AddObjectToObject(body, "input");
        cJSON *messages = input ? cJSON_AddArrayToObject(input, "messages") : NULL;
        cJSON *user = messages ? cJSON_CreateObject() : NULL;
        if (user && !cJSON_AddItemToArray(messages, user)) {
            cJSON_Delete(user);
            user = NULL;
        }
        cJSON *content = user && cJSON_AddStringToObject(user, "role", "user")
                             ? cJSON_AddArrayToObject(user, "content") : NULL;
        cJSON *text = content ? cJSON_CreateObject() : NULL;
        if (text && !cJSON_AddItemToArray(content, text)) {
            cJSON_Delete(text);
            text = NULL;
        }
        cJSON *params = cJSON_AddObjectToObject(body, "parameters");
        ok = text && cJSON_AddStringToObject(text, "text", prompt) && params &&
             (!*size || cJSON_AddStringToObject(params, "size", size));
    } else if (ok) {
        ok = cJSON_AddStringToObject(body, "prompt", prompt) &&
             (!*size || cJSON_AddStringToObject(body, "size", size));
        if (ok && !strncmp(rt->model, "dall-e-", 7))
            ok = cJSON_AddStringToObject(body, "response_format", "b64_json") != NULL;
    }
    char *json = ok ? cJSON_PrintUnformatted(body) : NULL;
    cJSON_Delete(body);
    return json;
}

static uint32_t read_be32(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static int valid_png(const unsigned char *p, size_t len) {
    if (len < 45 || memcmp(p, "\x89PNG\r\n\x1a\n", 8) ||
        read_be32(p + 8) != 13 || memcmp(p + 12, "IHDR", 4) ||
        !read_be32(p + 16) || !read_be32(p + 20))
        return 0;
    uint32_t table[256];
    for (uint32_t i = 0; i < 256; ++i) {
        uint32_t crc = i;
        for (int bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ ((crc & 1) ? UINT32_C(0xedb88320) : 0);
        table[i] = crc;
    }
    int image_data = 0;
    for (size_t i = 8; len - i >= 12;) {
        size_t size = read_be32(p + i);
        if (size > len - i - 12)
            return 0;
        const unsigned char *type = p + i + 4;
        uint32_t crc = UINT32_MAX;
        for (size_t j = 0; j < size + 4; ++j)
            crc = table[(crc ^ type[j]) & 255] ^ (crc >> 8);
        if ((crc ^ UINT32_MAX) != read_be32(type + 4 + size))
            return 0;
        i += size + 12;
        if (!memcmp(type, "IDAT", 4) && size)
            image_data = 1;
        if (!memcmp(type, "IEND", 4))
            return image_data && size == 0 && i == len;
    }
    return 0;
}

const char *llm_image_mime(const unsigned char *p, size_t len) {
    if (!p) return NULL;
    if (valid_png(p, len))
        return "image/png";
    if (len >= 4 && p[0] == 0xff && p[1] == 0xd8 && p[len - 2] == 0xff && p[len - 1] == 0xd9)
        return "image/jpeg";
    if (len >= 16 && !memcmp(p, "RIFF", 4) && !memcmp(p + 8, "WEBP", 4)) {
        size_t size = (size_t)p[4] | (size_t)p[5] << 8 | (size_t)p[6] << 16 | (size_t)p[7] << 24;
        if (size == len - 8)
            return "image/webp";
    }
    return NULL;
}

static const cJSON *property(const cJSON *obj, const char *name) {
    return cJSON_GetObjectItemCaseSensitive(obj, name);
}

static const char *native_image_url(const cJSON *response) {
    const cJSON *choices = property(property(response, "output"), "choices");
    const cJSON *message = property(cJSON_GetArrayItem(choices, 0), "message");
    const cJSON *entry;
    cJSON_ArrayForEach(entry, property(message, "content")) {
        const char *url = cJSON_GetStringValue(property(entry, "image"));
        if (url && *url)
            return url;
    }
    return NULL;
}

static int has_suffix(const char *s, const char *suffix) {
    size_t len = strlen(s), end = strlen(suffix);
    return len > end && !strcmp(s + len - end, suffix);
}

static int download_image(const char *url, const char *provider_origin, llm_image_result *out) {
    const char *path;
    char *origin = image_origin(url, &path);
    if (!origin || strncmp(origin, "https://", 8) || strlen(path) >= 1536 ||
        (strcmp(origin, provider_origin) != 0 &&
         !(has_suffix(provider_origin, ".aliyuncs.com") && has_suffix(origin, ".aliyuncs.com")))) {
        free(origin);
        return image_fail(out, "Image service returned an unsupported download URL");
    }
    /* Never forward model credentials to the download host. */
    http_response *response = http_get(origin, *path ? path : "/", NULL, IMAGE_DOWNLOAD_TIMEOUT_MS);
    free(origin);
    if (!response || response->status != 200 || !response->body ||
        !response->body_len || response->body_len > IMAGE_MAX_BYTES) {
        http_response_free(response);
        return image_fail(out, "Could not download the generated image (maximum 16 MiB)");
    }
    out->data = (unsigned char *)response->body;
    out->len = response->body_len;
    response->body = NULL;
    http_response_free(response);
    return 0;
}

static int decode_image(const char *payload, llm_image_result *out) {
    size_t length = strlen(payload);
    if (!length || length > ((IMAGE_MAX_BYTES + 2) / 3) * 4)
        return image_fail(out, "Generated image exceeds 16 MiB or is empty");
    size_t capacity = length / 4 * 3 + 3;
    out->data = malloc(capacity);
    if (!out->data)
        return image_fail(out, "Could not allocate generated image buffer");
    if (base64_decode(payload, out->data, capacity, &out->len) != 0 ||
        !out->len || out->len > IMAGE_MAX_BYTES) {
        free(out->data);
        out->data = NULL;
        return image_fail(out, "Image service returned invalid Base64 data");
    }
    return 0;
}

int llm_image_generate(const route *rt, const char *prompt, const char *size, llm_image_result *out) {
    if (!out)
        return -1;
    memset(out, 0, sizeof(*out));
    if (!route_is_image(rt) || !rt->model || !*rt->model || !prompt || !*prompt)
        return image_fail(out, "Select a text-to-image route and supply a prompt");
    const char *base = rt->base_url && *rt->base_url ? rt->base_url : "https://api.openai.com/v1";
    const char *base_path;
    char *origin = image_origin(base, &base_path);
    if (!origin)
        return image_fail(out, "Invalid image service base URL");
    int native = token_plan_origin(origin);
    const char *generation_path = DASHSCOPE_GENERATION_PATH;
    char compatible_path[1536];
    if (!native) {
        size_t path_length = strlen(base_path);
        if (path_length + sizeof("/images/generations") > sizeof compatible_path ||
            strcspn(base_path, "\r\n#?") != path_length) {
            free(origin);
            return image_fail(out, "Invalid image service base path");
        }
        while (path_length && base_path[path_length - 1] == '/') path_length--;
        snprintf(compatible_path, sizeof compatible_path, "%.*s/images/generations",
                 (int)path_length, base_path);
        /* The plain POSIX transport receives the complete request path
         * separately and strips any path from the connection base. */
        generation_path = compatible_path;
    }
    char normalized[24];
    if (image_size(size, native, normalized) != 0) {
        free(origin);
        return image_fail(out, "Image size must be WxH or auto (dimensions up to 4096)");
    }
    char *json = image_request(rt, prompt, normalized, native);
    if (!json) {
        free(origin);
        return image_fail(out, "Could not build the image service request");
    }
    strmap headers = {0};
    if (rt->api_key && *rt->api_key) {
        strbuf auth;
        strbuf_init(&auth);
        strbuf_appendf(&auth, "Bearer %s", rt->api_key);
        if (auth.buf)
            strmap_set(&headers, "Authorization", auth.buf);
        strbuf_free(&auth);
    }
    http_response *response = http_post(origin, generation_path, json, "application/json",
        &headers, IMAGE_GENERATION_TIMEOUT_MS);
    free(json);
    strmap_free(&headers);
    int rc = -1;
    if (!response) {
        rc = image_fail(out, *http_last_error() ? http_last_error() : "Could not call the image service");
        goto done;
    }
    out->provider_status = response->status;
    if (response->status < 200 || response->status >= 300) {
        char error[100];
        const char *hint = response->status == 401 || response->status == 403 ? " (check route credentials)" :
                           response->status == 429 ? " (rate limit or quota)" :
                           response->status == 400 ? " (check image model, prompt and size)" : "";
        snprintf(error, sizeof(error), "Image service HTTP %d%s", response->status, hint);
        rc = image_fail(out, error);
        goto done;
    }
    if (!response->body || response->body_len > IMAGE_MAX_BYTES * 2u) {
        rc = image_fail(out, "Missing or oversized image service response");
        goto done;
    }
    cJSON *root = cJSON_Parse(response->body);
    const cJSON *item = cJSON_GetArrayItem(property(root, "data"), 0);
    const char *payload = cJSON_GetStringValue(property(item, "b64_json"));
    const char *url = native ? native_image_url(root) : cJSON_GetStringValue(property(item, "url"));
    if (payload && *payload && !native)
        rc = decode_image(payload, out);
    else if (url && *url)
        rc = download_image(url, origin, out);
    else
        rc = image_fail(out, "Image service response contained no generated image");
    cJSON_Delete(root);
    if (rc == 0) {
        out->mime = llm_image_mime(out->data, out->len);
        if (!out->mime) {
            free(out->data);
            out->data = NULL;
            out->len = 0;
            rc = image_fail(out, "Generated response is not a complete PNG, JPEG or WebP image");
        }
    }
done:
    http_response_free(response);
    free(origin);
    return rc;
}
