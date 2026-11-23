#ifndef _WIN32
#define _XOPEN_SOURCE 700
#endif
#include "runtime/manju_paint.h"
#include "infra/util.h"
#include "llm/image.h"
#include "os/http_cancel.h"
#include "os/os_fs.h"
#include "os/os_thread.h"
#include "os/os_time.h"
#include "cJSON.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PAINT_PATH_CAP 2048
#define PAINT_WORKERS 4
#define PAINT_SHOTS_MAX 180
#define PAINT_IMAGE_MAX (16u * 1024u * 1024u)
#define PAINT_ATTEMPTS 3

typedef struct paint_panel {
    char *prompt;
    char file[64];
    char error[200];
} paint_panel;

typedef struct paint_run {
    route model;
    paint_panel *panels;
    const char *directory;
    const char *size;
    int total;
    atomic_int next, rejected;
    int ready, requests;
    mutex_t progress_lock;
    int (*abort_check)(void *);
    manju_paint_progress progress;
    void *context;
} paint_run;

static const char *field(const cJSON *object, const char *key) {
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(object, key));
}

void manju_append_cast(strbuf *prompt, const cJSON *board, const cJSON *shot) {
    const cJSON *ids = cJSON_GetObjectItemCaseSensitive(shot, "character_ids");
    const cJSON *cast = cJSON_GetObjectItemCaseSensitive(board, "characters");
    const cJSON *id;
    cJSON_ArrayForEach(id, ids) {
        const char *wanted = cJSON_GetStringValue(id);
        if (!wanted) continue;
        const cJSON *character;
        cJSON_ArrayForEach(character, cast) {
            const char *key = field(character, "id");
            const char *visual = field(character, "visual");
            if (!key || !visual || strcmp(key, wanted)) continue;
            const char *name = field(character, "name");
            strbuf_appendf(prompt, "\n保持角色 %s 的外观：%s", name ? name : key, visual);
            break;
        }
    }
}

static char *existing_path(const char *path) {
#ifdef _WIN32
    return fs_windows_final_path(path);
#else
    return realpath(path, NULL);
#endif
}

static int existing_under(const char *root, const char *path) {
    char *real = existing_path(path);
    size_t n = strlen(root);
    int ok = real && !strncmp(root, real, n) &&
             (root[n - 1] == '/' || real[n] == '/' || real[n] == 0);
    free(real);
    return ok;
}

static int paint_write(const char *path, const void *content, size_t bytes) {
    static atomic_uint sequence;
    char tmp[PAINT_PATH_CAP + 80];
    snprintf(tmp, sizeof tmp, "%s.paint-%lld-%u.tmp", path,
             (long long)time_now_us(), atomic_fetch_add(&sequence, 1));
    int ok = !fs_write_file(tmp, content, bytes) && fs_file_size(tmp) == (long long)bytes &&
             !fs_replace_file(tmp, path);
    if (!ok) fs_remove(tmp);
    return ok ? 0 : -1;
}

static const char *extension(const char *mime) {
    if (mime && !strcmp(mime, "image/png")) return ".png";
    if (mime && !strcmp(mime, "image/jpeg")) return ".jpg";
    if (mime && !strcmp(mime, "image/webp")) return ".webp";
    return NULL;
}

static int valid_cached_image(const char *directory, const char *path, const char *ext) {
    if (!existing_under(directory, path)) return 0;
    size_t len = 0;
    int more = 0;
    char *data = fs_read_file_slice(path, 0, PAINT_IMAGE_MAX, &len, &more);
    const char *got = data && !more ? extension(llm_image_mime((const unsigned char *)data, len)) : NULL;
    free(data);
    return got && !strcmp(got, ext);
}

static void notify(paint_run *run, int ready, int requests) {
    mutex_lock(&run->progress_lock);
    run->ready += ready;
    run->requests += requests;
    if (run->progress) run->progress(run->ready, run->total, run->requests, run->context);
    mutex_unlock(&run->progress_lock);
}

static int aborted(paint_run *run) {
    return run->abort_check && run->abort_check(run->context);
}

static int cancel_request(void *context) {
    return aborted(context);
}

static void paint_one(paint_run *run, paint_panel *panel, int index) {
    const char *suffixes[] = {".png", ".jpg", ".webp"};
    char digest[17], stem[50], path[PAINT_PATH_CAP];
    strbuf signature;
    strbuf_init(&signature);
    strbuf_appendf(&signature, "%s\n%s\n%s\n%s", run->model.base_url ? run->model.base_url : "",
                   run->model.model, run->size ? run->size : "", panel->prompt);
    if (!signature.buf) {
        snprintf(panel->error, sizeof panel->error, "无法分配图像任务");
        return;
    }
    hash_hex(digest, hash64(signature.buf, signature.len));
    strbuf_free(&signature);
    snprintf(stem, sizeof stem, "ai_%03d_%s", index, digest);
    for (size_t i = 0; i < sizeof suffixes / sizeof suffixes[0]; i++) {
        snprintf(panel->file, sizeof panel->file, "%s%s", stem, suffixes[i]);
        path_join(path, sizeof path, run->directory, panel->file);
        if (valid_cached_image(run->directory, path, suffixes[i])) {
            notify(run, 1, 0);
            return;
        }
    }
    panel->file[0] = 0;
    for (int attempt = 0; attempt < PAINT_ATTEMPTS && !aborted(run) && !atomic_load(&run->rejected); attempt++) {
        notify(run, 0, 1);
        llm_image_result image = {0};
        int rc = llm_image_generate(&run->model, panel->prompt, run->size, &image);
        int status = image.provider_status;
        if (rc == 0 && !aborted(run)) {
            const char *ext = extension(image.mime);
            if (ext) {
                snprintf(panel->file, sizeof panel->file, "%s%s", stem, ext);
                path_join(path, sizeof path, run->directory, panel->file);
                if (paint_write(path, image.data, image.len) == 0) {
                    llm_image_result_free(&image);
                    notify(run, 1, 0);
                    return;
                }
            }
            panel->file[0] = 0;
            snprintf(panel->error, sizeof panel->error, "无法保存图片");
            llm_image_result_free(&image);
            return;
        }
        snprintf(panel->error, sizeof panel->error, "%s", image.error ? image.error : "出图已中止");
        llm_image_result_free(&image);
        if (status == 401 || status == 403) atomic_store(&run->rejected, 1);
        if (status >= 400 && status < 500 && status != 408 && status != 429) break;
        if (attempt + 1 < PAINT_ATTEMPTS) {
            int delay = (status == 429 ? 30 : 10) * (1 << attempt);
            for (int tick = 0; tick < delay && !aborted(run); tick++) time_sleep_ms(100);
        }
    }
}

static void paint_worker(void *context) {
    paint_run *run = context;
    http_set_cancel_check(cancel_request, run);
    for (;;) {
        int i = atomic_fetch_add(&run->next, 1);
        if (i >= run->total || aborted(run)) break;
        if (run->panels[i].prompt) paint_one(run, &run->panels[i], i);
    }
    http_set_cancel_check(NULL, NULL);
}

static char *visual_prompt(const char *direction, const char *visual, const char *text,
                           const cJSON *board, const cJSON *shot) {
    strbuf prompt;
    strbuf_init(&prompt);
    strbuf_append(&prompt, "竖版9:16的精绘二维中国漫剧分镜，赛璐璐光影，电影构图。"
                          "主体保持在中间区域，保留上方构图空间。"
                          "无可读文字、字幕、气泡、边框和标志。\n");
    if (direction && *direction) strbuf_appendf(&prompt, "%s\n", direction);
    if (visual && *visual) strbuf_append(&prompt, visual);
    else strbuf_appendf(&prompt, "将以下情节描绘成单幅画面，忠实表达场景和人物动作，不绘制文字：%s", text);
    manju_append_cast(&prompt, board, shot);
    return strbuf_detach(&prompt);
}

static int valid_shot(const cJSON *shot) {
    const char *kind = field(shot, "kind"), *text = field(shot, "text"), *prompt = field(shot, "image_prompt");
    return kind && (!strcmp(kind, "narration") || !strcmp(kind, "dialogue") || !strcmp(kind, "sys")) &&
           text && *text && strlen(text) <= 12000 && (!prompt || strlen(prompt) <= 12000);
}

static int panel_add(cJSON *entries, const char *file, const char *kind, int index, int last) {
    const char *role = index == 0 ? "title" : last ? "end" : "shot";
    double z0 = 1.04, z1 = 1.11, cy = .50, px = .045, py = -.02, ph = index * 1.7;
    if (!index) { z0 = 1; z1 = 1.05; px = -.04; py = 0; }
    else if (last) { z0 = 1.12; z1 = 1.02; px = .03; py = -.02; ph = 3.1; }
    else {
        if (!strcmp(kind, "sys")) { z0 = 1.14; z1 = 1.04; cy = .55; px = .05; py = 0; }
        if (!strcmp(kind, "dialogue")) { z0 = 1.02; z1 = 1.09; cy = .46; px = -.05; py = .02; }
        if (index % 2 == 0) { px = -px; py = -py; }
    }
    cJSON *out = cJSON_CreateObject();
    if (!out) return 0;
    int ok = cJSON_AddStringToObject(out, "file", file) && cJSON_AddStringToObject(out, "role", role) &&
             cJSON_AddStringToObject(out, "kind", kind) && cJSON_AddNumberToObject(out, "z0", z0) &&
             cJSON_AddNumberToObject(out, "z1", z1) && cJSON_AddNumberToObject(out, "cx", .50) &&
             cJSON_AddNumberToObject(out, "cy", cy) && cJSON_AddNumberToObject(out, "px", px) &&
             cJSON_AddNumberToObject(out, "py", py) && cJSON_AddNumberToObject(out, "ph", ph) &&
             cJSON_AddItemToArray(entries, out);
    if (!ok) cJSON_Delete(out);
    return ok;
}

static int paint_episode(router *routes, const char *route_name, const char *workspace, int episode,
                         int characters, int (*abort_check)(void *), manju_paint_progress progress,
                         void *context, char **result) {
    char directory[PAINT_PATH_CAP], episode_dir[PAINT_PATH_CAP], path[PAINT_PATH_CAP], relative[40];
    char *root = NULL, *canonical_directory = NULL, *text = NULL, *manifest = NULL;
    cJSON *board = NULL, *doc = NULL;
    int rc = -1, expected = 0;
    paint_run run = {.abort_check = abort_check, .progress = progress, .context = context};
    const char *error = "无法分配漫剧绘制任务";
    strbuf report;
    strbuf_init(&report);
    *result = NULL;
    if (router_select_image(routes, route_name, &run.model) != 0) {
        error = "没有匹配的文生图路由，请在 Model routes 配置并测试出图";
        goto done;
    }
    if (strstr(run.model.model, "qwen-image-")) run.size = characters ? "1024x576" : "576x1024";
    root = existing_path(workspace && *workspace ? workspace : ".");
    snprintf(relative, sizeof relative, "manju/ep%02d", episode);
    if (!root || strlen(root) + strlen(relative) + 100 >= sizeof directory) {
        error = "无效的漫剧工作区路径";
        goto done;
    }
    path_join(episode_dir, sizeof episode_dir, root, relative);
    path_join(path, sizeof path, episode_dir, "shots.json");
    if (!existing_under(root, path)) {
        error = "分镜 shots.json 不存在或超出工作区";
        goto done;
    }
    size_t length = 0;
    int more = 0;
    text = fs_read_file_slice(path, 0, 2 * 1024 * 1024, &length, &more);
    board = text && !more ? cJSON_ParseWithOpts(text, NULL, 1) : NULL;
    cJSON *shots = cJSON_GetObjectItemCaseSensitive(board, "shots");
    const char *title = field(board, "title"), *badge = field(board, "badge");
    int count = cJSON_GetArraySize(shots);
    const cJSON *cast = cJSON_GetObjectItemCaseSensitive(board, "characters");
    int cast_count = cJSON_GetArraySize(cast);
    if (!title || !*title || !badge || !*badge || !cJSON_IsArray(shots) ||
        count < 1 || count > PAINT_SHOTS_MAX) {
        error = "shots.json 必须包含标题、短标题及 1–180 个有效镜头";
        goto done;
    }
    if (characters && (!cJSON_IsArray(cast) || cast_count > 24)) {
        error = "分镜角色清单无效，需要 0–24 个角色";
        goto done;
    }
    run.total = characters ? cast_count : count + 2;
    if (!run.total) {
        strbuf_append(&report, "本集没有需要生成三视图的角色。");
        rc = 0;
        goto done;
    }
    run.panels = calloc((size_t)run.total, sizeof(*run.panels));
    if (!run.panels) goto done;
    const char *direction = field(board, "art_direction");
    const char *opening = field(board, "title_image_prompt"), *closing = field(board, "end_image_prompt");
    if ((direction && strlen(direction) > 10000) || (opening && strlen(opening) > 12000) ||
        (closing && strlen(closing) > 12000)) {
        error = "画风或首尾卡提示过长";
        goto done;
    }
    if (characters) {
        for (int i = 0; i < cast_count; i++) {
            const cJSON *character = cJSON_GetArrayItem(cast, i);
            const char *visual = field(character, "visual");
            const char *name = field(character, "name");
            const char *id = field(character, "id");
            if (!visual || !*visual || !name || !id || strlen(visual) > 1500 || strlen(name) > 320) {
                error = "角色缺少用于生成三视图的外观说明";
                goto done;
            }
            strbuf prompt;
            strbuf_init(&prompt);
            strbuf_appendf(&prompt, "横版中文漫剧角色设定图。浅灰纯色背景，三栏从左到右：同一个角色的正面、侧面、背面全身。"
                           "三栏都为自然站姿，全身入镜，保持完全相同的发型、身高、服装、色彩和道具，使用正交视角。"
                           "无字幕、标签、水印、边框或任何文字。角色：%s。造型：%s", name, visual);
            if (direction && *direction) strbuf_appendf(&prompt, "\n统一画风：%s", direction);
            run.panels[i].prompt = strbuf_detach(&prompt);
            if (!run.panels[i].prompt) goto done;
            expected++;
        }
    }
    if (!characters && opening && *opening) {
        run.panels[0].prompt = visual_prompt(direction, opening, title, board, NULL);
        if (!run.panels[0].prompt) goto done;
        expected++;
    }
    if (!characters && closing && *closing) {
        run.panels[count + 1].prompt = visual_prompt(direction, closing, title, board, NULL);
        if (!run.panels[count + 1].prompt) goto done;
        expected++;
    }
    for (int i = 0; !characters && i < count; i++) {
        cJSON *shot = cJSON_GetArrayItem(shots, i);
        if (!valid_shot(shot)) {
            strbuf_appendf(&report, "镜头 %d 的类型、台词或绘图说明无效", i + 1);
            goto done;
        }
        run.panels[i + 1].prompt = visual_prompt(direction, field(shot, "image_prompt"),
                                                field(shot, "text"), board, shot);
        if (!run.panels[i + 1].prompt) goto done;
        expected++;
    }
    path_join(directory, sizeof directory, episode_dir, characters ? "characters" : "frames");
    if (!existing_under(root, episode_dir) || fs_mkdirs(directory) != 0 || !existing_under(root, directory) ||
        !(canonical_directory = existing_path(directory))) {
        error = "画面目录无法创建或超出工作区";
        goto done;
    }
    run.directory = canonical_directory;
    if (mutex_init(&run.progress_lock) != 0) goto done;
    if (progress) progress(0, run.total, 0, context);
    thread_t *threads[PAINT_WORKERS] = {0};
    for (int i = 0; i < PAINT_WORKERS && i < expected; i++)
        threads[i] = thread_create(paint_worker, &run);
    int workers = 0;
    for (int i = 0; i < PAINT_WORKERS; i++) {
        if (threads[i]) {
            thread_join(threads[i]);
            workers++;
        }
    }
    mutex_destroy(&run.progress_lock);
    if (!workers) {
        error = "无法启动文生图工作线程";
        goto done;
    }
    if (aborted(&run)) {
        error = "已中止文生图，已保存的镜头可继续使用";
        goto done;
    }
    if (run.ready != expected) {
        strbuf_appendf(&report, "文生图 %d/%d 张就绪，已提交 %d 次生成。继续任务时将复用已验证的镜头。\n",
                       run.ready, expected, run.requests);
        for (int i = 0; i < run.total; i++) {
            paint_panel *panel = &run.panels[i];
            if (panel->prompt && !panel->file[0])
                strbuf_appendf(&report, "画面 %d：%s\n", i, panel->error[0] ? panel->error : "未生成");
        }
        goto done;
    }
    if (characters) {
        doc = cJSON_CreateObject();
        cJSON *entries = doc ? cJSON_AddArrayToObject(doc, "characters") : NULL;
        if (!entries || !cJSON_AddNumberToObject(doc, "ep", episode) ||
            !cJSON_AddStringToObject(doc, "image_model", run.model.model)) goto done;
        for (int i = 0; i < cast_count; i++) {
            const cJSON *character = cJSON_GetArrayItem(cast, i);
            cJSON *item = cJSON_CreateObject();
            if (!item || !cJSON_AddStringToObject(item, "id", field(character, "id")) ||
                !cJSON_AddStringToObject(item, "name", field(character, "name")) ||
                !cJSON_AddStringToObject(item, "visual", field(character, "visual")) ||
                !cJSON_AddStringToObject(item, "file", run.panels[i].file) ||
                !cJSON_AddItemToArray(entries, item)) {
                cJSON_Delete(item);
                goto done;
            }
        }
        manifest = cJSON_PrintUnformatted(doc);
        path_join(path, sizeof path, directory, "characters.json");
        if (!manifest || paint_write(path, manifest, strlen(manifest)) != 0) {
            error = "无法发布角色三视图清单";
            goto done;
        }
        strbuf_appendf(&report, "%s 完成 %d 个角色的正面、侧面和背面设定（%d 次出图请求）。\n角色清单：`%s`",
                       run.model.model, cast_count, run.requests, path);
        rc = 0;
        goto done;
    }
    if (!run.panels[0].prompt) strcpy(run.panels[0].file, run.panels[1].file);
    if (!run.panels[count + 1].prompt) strcpy(run.panels[count + 1].file, run.panels[count].file);
    doc = cJSON_CreateObject();
    cJSON *entries = doc ? cJSON_AddArrayToObject(doc, "panels") : NULL;
    const char *poster = run.panels[run.panels[0].prompt ? 0 : 1].file;
    if (!entries || !cJSON_AddNumberToObject(doc, "ep", episode) || !cJSON_AddStringToObject(doc, "title", title) ||
        !cJSON_AddStringToObject(doc, "poster", poster) || !cJSON_AddStringToObject(doc, "image_model", run.model.model) ||
        !cJSON_AddNumberToObject(doc, "generated_assets", expected)) goto done;
    for (int i = 0; i < run.total; i++) {
        int last = i == count + 1;
        const char *kind = i == 0 ? "title" : last ? "end" : field(cJSON_GetArrayItem(shots, i - 1), "kind");
        if (!panel_add(entries, run.panels[i].file, kind, i, last)) goto done;
    }
    manifest = cJSON_PrintUnformatted(doc);
    path_join(path, sizeof path, directory, "panels.json");
    if (!manifest || paint_write(path, manifest, strlen(manifest)) != 0) {
        error = "无法发布画面清单";
        goto done;
    }
    strbuf_appendf(&report, "%s 画面完成：%d 个正片镜头，%d 张 AI 图片，%d 次生成请求。\n"
                            "画面清单：`%s`\n封面：`%s/%s`",
                   run.model.model, count, expected, run.requests, path, canonical_directory, poster);
    if (progress) progress(run.total, run.total, run.requests, context);
    rc = 0;
done:
    *result = report.len ? strbuf_detach(&report) : xstrdup(error);
    strbuf_free(&report);
    if (run.panels) for (int i = 0; i < run.total; i++) free(run.panels[i].prompt);
    free(run.panels);
    free(text);
    free(root);
    free(canonical_directory);
    free(manifest);
    cJSON_Delete(board);
    cJSON_Delete(doc);
    route_clear(&run.model);
    return rc;
}

int manju_paint_episode(router *routes, const char *route_name, const char *workspace, int episode,
                        int (*abort_check)(void *), manju_paint_progress progress, void *context,
                        char **result) {
    return paint_episode(routes, route_name, workspace, episode, 0, abort_check, progress, context, result);
}

int manju_character_views(router *routes, const char *route_name, const char *workspace, int episode,
                          int (*abort_check)(void *), manju_paint_progress progress, void *context,
                          char **result) {
    return paint_episode(routes, route_name, workspace, episode, 1, abort_check, progress, context, result);
}
