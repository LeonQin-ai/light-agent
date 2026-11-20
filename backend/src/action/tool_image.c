#include "action/tools.h"
#include "infra/util.h"
#include "llm/image.h"
#include "llm/router.h"
#include "os/os_fs.h"
#include "os/os_time.h"
#include "cJSON.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int image_write(const char *path, const void *data, size_t len) {
    char dir[2048], tmp[2128];
    static atomic_uint sequence;
    const char *slash = strrchr(path, '/'), *back = strrchr(path, '\\');
    if (back && (!slash || back > slash)) slash = back;
    if (slash) {
        snprintf(dir, sizeof dir, "%.*s", (int)(slash - path + 1), path);
        if (fs_mkdirs(dir) != 0) return -1;
    }
    snprintf(tmp, sizeof tmp, "%s.image-%lld-%u.tmp", path,
             (long long)time_now_us(), atomic_fetch_add(&sequence, 1));
    if (fs_write_file(tmp, data, len) == 0) {
        size_t read_len = 0;
        int more = 0;
        char *verify = fs_read_file_slice(tmp, 0, len, &read_len, &more);
        int valid = verify && !more && read_len == len && !memcmp(verify, data, len);
        free(verify);
        if (valid && fs_replace_file(tmp, path) == 0) return 0;
    }
    fs_remove(tmp);
    return -1;
}

static int extension_matches(const char *path, const char *mime) {
    const char *ext = strrchr(path, '.');
    return ext && mime &&
           ((!strcmp(mime, "image/png") && !strcmp(ext, ".png")) ||
            (!strcmp(mime, "image/jpeg") && (!strcmp(ext, ".jpg") || !strcmp(ext, ".jpeg"))) ||
            (!strcmp(mime, "image/webp") && !strcmp(ext, ".webp")));
}

static tool_result *image_generate_exec(const tool *self, const tool_ctx *ctx, const char *args_json) {
    (void)self;
    cJSON *args = cJSON_Parse(args_json);
    const char *prompt = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(args, "prompt"));
    const char *path = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(args, "path"));
    const char *name = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(args, "route"));
    const char *size = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(args, "size"));
    const char *error = NULL;
    route selected = {0};
    llm_image_result image = {0};
    tool_result *result = NULL;
    char target[2048];

    if (!ctx || !prompt || !*prompt || !path || !*path ||
        strlen(path) + (ctx->workspace ? strlen(ctx->workspace) : 0) + 2 >= sizeof target) {
        error = "image_generate: provide a nonempty visual prompt and a valid local output path";
        goto done;
    }
    if (!extension_matches(path, "image/png") && !extension_matches(path, "image/jpeg") &&
        !extension_matches(path, "image/webp")) {
        error = "image_generate: use a .png, .jpg, .jpeg or .webp output path";
        goto done;
    }
    path_resolve(target, sizeof target, ctx->workspace, path);
    if (fs_is_dir(target)) {
        error = "image_generate: the output path is a directory";
        goto done;
    }
    if (router_select_image(tool_registry_router(ctx->reg), name, &selected) != 0) {
        error = "image_generate: no matching image model. Check the image route in Model routes";
        goto done;
    }
    if (ctx->should_abort && ctx->should_abort(ctx->abort_ud)) {
        error = "image_generate: cancelled";
        goto done;
    }
    if (llm_image_generate(&selected, prompt, size, &image) != 0) {
        error = image.error ? image.error : "image_generate: provider request failed";
        goto done;
    }
    if (ctx->should_abort && ctx->should_abort(ctx->abort_ud)) {
        error = "image_generate: cancelled";
        goto done;
    }
    if (!extension_matches(path, image.mime)) {
        error = "image_generate: provider image format does not match output extension";
        goto done;
    }
    cJSON *receipt = cJSON_CreateObject();
    if (receipt) {
        cJSON_AddStringToObject(receipt, "path", target);
        cJSON_AddStringToObject(receipt, "model", selected.model);
        cJSON_AddStringToObject(receipt, "mime", image.mime);
        cJSON_AddNumberToObject(receipt, "bytes", (double)image.len);
        cJSON_AddStringToObject(receipt, "note", "Image saved and byte-verified. Use the path in the UI.");
        char *json = cJSON_PrintUnformatted(receipt);
        if (json) result = tool_result_new(1, json);
        free(json);
        cJSON_Delete(receipt);
    }
    if (!result || !result->output || image_write(target, image.data, image.len) != 0) {
        tool_result_free(result);
        result = NULL;
        error = "image_generate: could not save and verify the image";
    }
done:
    if (!result) result = tool_result_new(0, error);
    llm_image_result_free(&image);
    route_clear(&selected);
    cJSON_Delete(args);
    return result;
}

const tool *tool_image_generate(void) {
    static const tool entry = {
        "image_generate",
        "Generate original text-to-image artwork with a configured image model. Saves and "
        "verifies a local asset for use in a website, app, or other visual deliverable. "
        "Set path to the intended project asset (.png is recommended). Select a route by name "
        "only if explicitly requested. Reuse a successful result instead of generating it again.",
        "{\"type\":\"object\",\"properties\":{"
        "\"prompt\":{\"type\":\"string\"},\"path\":{\"type\":\"string\",\"format\":\"file-path\"},"
        "\"route\":{\"type\":\"string\"},\"size\":{\"type\":\"string\"}},"
        "\"required\":[\"prompt\",\"path\"]}",
        1, image_generate_exec, NULL
    };
    return &entry;
}
