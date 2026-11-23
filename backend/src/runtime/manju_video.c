#ifndef _WIN32
#define _XOPEN_SOURCE 700
#endif
#include "runtime/manju_video.h"
#include "runtime/manju_paint.h"
#include "infra/util.h"
#include "llm/video.h"
#include "os/http_cancel.h"
#include "os/os_fs.h"
#include "os/os_thread.h"
#include "os/os_time.h"
#include "cJSON.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define VIDEO_PATH_CAP 2048
#define VIDEO_SHOTS_MAX 180
#define VIDEO_WORKERS 2
#define VIDEO_WAIT_MS (15 * 60 * 1000)

typedef struct clip {
    const char *kind;
    char *prompt;
    char file[64];
    char error[224];
    int ready;
} clip;

typedef struct clip_run {
    route model;
    const char *directory;
    int seconds, total;
    clip *clips;
    atomic_int next, failed;
    int ready, requests;
    mutex_t progress_lock;
    int (*abort_check)(void *);
    manju_video_progress progress;
    void *context;
} clip_run;

static const char *field(const cJSON *o, const char *key) {
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, key));
}

static char *existing_path(const char *path) {
#ifdef _WIN32
    return fs_windows_final_path(path);
#else
    return realpath(path, NULL);
#endif
}

static int inside(const char *root, const char *path) {
    char *resolved = existing_path(path);
    size_t len = strlen(root);
    int ok = resolved && len && !strncmp(root, resolved, len) &&
        (root[len - 1] == '/' || resolved[len] == '/' || !resolved[len]);
    free(resolved);
    return ok;
}

static int atomic_write(const char *path, const char *value) {
    static atomic_uint sequence;
    char tmp[VIDEO_PATH_CAP + 80];
    snprintf(tmp, sizeof tmp, "%s.video-%lld-%u.tmp", path, (long long)time_now_us(),
             atomic_fetch_add(&sequence, 1));
    size_t len = strlen(value);
    if (!fs_write_file(tmp, value, len) && fs_file_size(tmp) == (long long)len &&
        !fs_replace_file(tmp, path)) return 0;
    fs_remove(tmp);
    return -1;
}

static void notify(clip_run *run, int ready, int requests) {
    mutex_lock(&run->progress_lock);
    run->ready += ready;
    run->requests += requests;
    if (run->progress) run->progress(run->ready, run->total, run->requests, run->context);
    mutex_unlock(&run->progress_lock);
}

static int cancelled(void *ctx) {
    clip_run *run = ctx;
    return run->abort_check && run->abort_check(run->context);
}

static int wait_slice(clip_run *run, int ms) {
    for (int i = 0; i < ms && !cancelled(run); i += 100) time_sleep_ms(100);
    return cancelled(run);
}

/* Serialize checkpoint and submit across overlapping flows in this runtime.
 * Polling and download remain parallel. */
static atomic_flag submit_lock = ATOMIC_FLAG_INIT;

static int load_or_submit(clip_run *run, clip *entry, const char *checkpoint,
                          char task_id[128], char **error) {
    while (atomic_flag_test_and_set(&submit_lock))
        if (wait_slice(run, 100)) return -1;
    int rc = -1;
    if (cancelled(run)) goto done;
    if (fs_exists(checkpoint)) {
        size_t len = 0;
        int more = 0;
        char *saved = inside(run->directory, checkpoint)
            ? fs_read_file_slice(checkpoint, 0, 128, &len, &more) : NULL;
        if (saved && !more && len && len < 128 && len == strlen(saved) && strcmp(saved, "submitting")) {
            memcpy(task_id, saved, len + 1);
            rc = 0;
        } else {
            *error = xstrdup("上次提交结果未确认，已避免重复扣费。请先在服务商控制台核对该镜头的请求。");
        }
        free(saved);
        goto done;
    }
    if (atomic_write(checkpoint, "submitting") != 0) {
        *error = xstrdup("无法保存提交检查点");
        goto done;
    }
    notify(run, 0, 1);
    int status = llm_video_submit(&run->model, entry->prompt, run->seconds, task_id, error);
    if (status == -1) fs_remove(checkpoint);
    if (status != 0) goto done;
    if (atomic_write(checkpoint, task_id) != 0) {
        *error = xstrdup("请求已受理，但无法保存任务编号。请先核对服务商控制台。");
        goto done;
    }
    rc = 0;
done:
    atomic_flag_clear(&submit_lock);
    return rc;
}

static void generate_clip(clip_run *run, clip *entry, int index) {
    char key[17], path[VIDEO_PATH_CAP], checkpoint[VIDEO_PATH_CAP], job[80], task_id[128];
    char *error = NULL, *url = NULL;
    strbuf identity;
    strbuf_init(&identity);
    strbuf_appendf(&identity, "%s\n%s\n%d\n%s", run->model.base_url ? run->model.base_url : "",
                   run->model.model, run->seconds, entry->prompt);
    if (!identity.buf) goto done;
    hash_hex(key, hash64(identity.buf, identity.len));
    snprintf(entry->file, sizeof entry->file, "clip_%03d_%s.mp4", index, key);
    snprintf(job, sizeof job, "clip_%03d_%s.task", index, key);
    path_join(path, sizeof path, run->directory, entry->file);
    path_join(checkpoint, sizeof checkpoint, run->directory, job);
    if (inside(run->directory, path) && llm_video_validate_file(path)) {
        entry->ready = 1;
        goto done;
    }
    if (load_or_submit(run, entry, checkpoint, task_id, &error) != 0) goto done;
    int64_t deadline = time_now_ms() + VIDEO_WAIT_MS;
    for (;;) {
        if (cancelled(run) || time_now_ms() >= deadline) {
            free(error);
            error = xstrdup("已停止等待，继续时会查询已提交的视频任务");
            break;
        }
        free(error);
        error = NULL;
        int poll = llm_video_poll(&run->model, task_id, &url, &error);
        if (poll == 1) {
            if (llm_video_download(&run->model, url, path, &error) == 0)
                entry->ready = 1;
            /* Keep the task ID so a damaged/missing download can be recovered. */
            break;
        }
        if (poll < 0 && poll != -2) {
            if (poll == -1) fs_remove(checkpoint);
            break;
        }
        wait_slice(run, poll == -2 ? 5000 : 8000);
    }
done:
    if (entry->ready) notify(run, 1, 0);
    else {
        snprintf(entry->error, sizeof entry->error, "%s", error ? error : "短视频任务未完成");
        atomic_store(&run->failed, 1);
    }
    free(url);
    free(error);
    strbuf_free(&identity);
}

static void video_worker(void *context) {
    clip_run *run = context;
    http_set_cancel_check(cancelled, run);
    while (!cancelled(run) && !atomic_load(&run->failed)) {
        int i = atomic_fetch_add(&run->next, 1);
        if (i >= run->total) break;
        generate_clip(run, &run->clips[i], i);
    }
    http_set_cancel_check(NULL, NULL);
}

static char *make_prompt(const cJSON *board, const cJSON *shot, const char *visual, int seconds) {
    const char *title = field(board, "title");
    const char *direction = field(board, "art_direction");
    const char *action = field(shot, "video_prompt");
    const char *text = field(shot, "text");
    const char *scene = field(shot, "scene");
    strbuf prompt;
    strbuf_init(&prompt);
    strbuf_appendf(&prompt, "生成%d秒、9:16竖版的精绘二维中文漫剧视频。保持统一的角色和画风。"
                   "单一连续镜头，动作自然，画面不含字幕、可读文字、对白气泡或标志。", seconds);
    if (direction && *direction) strbuf_appendf(&prompt, "\n画风：%s", direction);
    strbuf_appendf(&prompt, "\n场景及角色外观：%s", visual && *visual ? visual : scene && *scene ? scene : title);
    if (action && *action) strbuf_appendf(&prompt, "\n镜头动作：%s", action);
    else if (text && *text) strbuf_appendf(&prompt, "\n依据文案表现动作与情绪：%s", text);
    else strbuf_append(&prompt, "\n一点自然的环境运动，镜头缓慢推进。");
    manju_append_cast(&prompt, board, shot);
    if (prompt.len > 6000) {
        strbuf_free(&prompt);
        return NULL;
    }
    return strbuf_detach(&prompt);
}

int manju_video_episode(router *routes, const char *route_name, const char *workspace,
                        int episode, int seconds, int (*abort_check)(void *),
                        manju_video_progress progress, void *context, char **result) {
    char relative[64], directory[VIDEO_PATH_CAP], path[VIDEO_PATH_CAP];
    char *root = NULL, *canonical_directory = NULL, *text = NULL, *manifest = NULL;
    cJSON *board = NULL, *doc = NULL;
    clip_run run = {.seconds = seconds, .abort_check = abort_check, .progress = progress, .context = context};
    const char *error = "无法准备视频分镜";
    strbuf report;
    strbuf_init(&report);
    int rc = -1;
    if ((seconds != 3 && seconds != 4) || episode < 1 || episode > 500) goto done;
    if (router_select_video(routes, route_name, &run.model) != 0) {
        error = "未找到选定的文生视频路由，请在模型路由中配置并测试";
        goto done;
    }
    root = existing_path(workspace && *workspace ? workspace : ".");
    if (!root || strlen(root) + 200 >= sizeof directory) goto done;
    snprintf(relative, sizeof relative, "manju/ep%02d", episode);
    path_join(directory, sizeof directory, root, relative);
    path_join(path, sizeof path, directory, "shots.json");
    size_t bytes = 0;
    int more = 0;
    if (inside(root, directory) && inside(root, path))
        text = fs_read_file_slice(path, 0, 2 * 1024 * 1024, &bytes, &more);
    if (text && !more && bytes == strlen(text)) board = cJSON_ParseWithOpts(text, NULL, 1);
    const char *title = field(board, "title");
    cJSON *shots = cJSON_GetObjectItemCaseSensitive(board, "shots");
    int count = cJSON_GetArraySize(shots);
    if (!title || !*title || !cJSON_IsArray(shots) || count < 1 || count > VIDEO_SHOTS_MAX) {
        error = "shots.json 缺少有效分镜";
        goto done;
    }
    run.total = count + 2;
    run.clips = calloc((size_t)run.total, sizeof(*run.clips));
    if (!run.clips) goto done;
    for (int i = 0; i < run.total; i++) {
        int last = i == run.total - 1;
        cJSON *shot = i && !last ? cJSON_GetArrayItem(shots, i - 1) : NULL;
        const char *kind = !i ? "title" : last ? "end" : field(shot, "kind");
        if (!kind || (shot && (strcmp(kind, "narration") && strcmp(kind, "dialogue") && strcmp(kind, "sys"))) ||
            (shot && (!field(shot, "text") || !*field(shot, "text")))) {
            error = "分镜类型或台词无效";
            goto done;
        }
        run.clips[i].kind = kind;
        const char *visual = field(shot ? shot : board,
                                   !i ? "title_image_prompt" : last ? "end_image_prompt" : "image_prompt");
        run.clips[i].prompt = make_prompt(board, shot, visual, seconds);
        if (!run.clips[i].prompt) goto done;
    }
    snprintf(relative, sizeof relative, "manju/ep%02d/clips", episode);
    path_join(directory, sizeof directory, root, relative);
    if (fs_mkdirs(directory) != 0 || !inside(root, directory) ||
        !(canonical_directory = existing_path(directory))) {
        error = "无法准备短视频目录";
        goto done;
    }
    run.directory = canonical_directory;
    if (mutex_init(&run.progress_lock) != 0) goto done;
    notify(&run, 0, 0);
    thread_t *workers[VIDEO_WORKERS];
    for (int i = 0; i < VIDEO_WORKERS; i++) workers[i] = thread_create(video_worker, &run);
    for (int i = 0; i < VIDEO_WORKERS; i++) if (workers[i]) thread_join(workers[i]);
    mutex_destroy(&run.progress_lock);
    if (cancelled(&run)) {
        error = "短视频流程已停止，继续时将复用已保存片段";
        goto done;
    }
    if (run.ready != run.total) {
        strbuf_appendf(&report, "短视频 %d/%d 准备完成，已提交 %d 次。继续时复用有效片段和任务编号。",
                       run.ready, run.total, run.requests);
        for (int i = 0; i < run.total; i++)
            if (run.clips[i].error[0])
                strbuf_appendf(&report, "\n镜头 %d：%s", i, run.clips[i].error);
        goto done;
    }
    char fingerprint[17];
    hash_hex(fingerprint, hash64(text, bytes));
    doc = cJSON_CreateObject();
    cJSON *entries = doc ? cJSON_AddArrayToObject(doc, "clips") : NULL;
    if (!entries || !cJSON_AddNumberToObject(doc, "ep", episode) ||
        !cJSON_AddStringToObject(doc, "title", title) ||
        !cJSON_AddStringToObject(doc, "video_model", run.model.model) ||
        !cJSON_AddStringToObject(doc, "storyboard_fingerprint", fingerprint) ||
        !cJSON_AddNumberToObject(doc, "clip_seconds", seconds)) goto done;
    for (int i = 0; i < run.total; i++) {
        cJSON *item = cJSON_CreateObject();
        if (!item || !cJSON_AddStringToObject(item, "file", run.clips[i].file) ||
            !cJSON_AddStringToObject(item, "role", !i ? "title" : i == run.total - 1 ? "end" : "shot") ||
            !cJSON_AddStringToObject(item, "kind", run.clips[i].kind) || !cJSON_AddItemToArray(entries, item)) {
            cJSON_Delete(item);
            goto done;
        }
    }
    manifest = cJSON_PrintUnformatted(doc);
    path_join(path, sizeof path, directory, "clips.json");
    if (!manifest || atomic_write(path, manifest) != 0) {
        error = "无法发布短视频清单";
        goto done;
    }
    strbuf_appendf(&report, "%s 完成 %d 个短视频（每段 %d 秒，%d 次新提交）。\n短视频清单：`%s`",
                   run.model.model, run.total, seconds, run.requests, path);
    rc = 0;
done:
    *result = report.len ? strbuf_detach(&report) : xstrdup(error);
    strbuf_free(&report);
    for (int i = 0; run.clips && i < run.total; i++) free(run.clips[i].prompt);
    free(run.clips);
    free(manifest);
    free(text);
    free(canonical_directory);
    free(root);
    cJSON_Delete(doc);
    cJSON_Delete(board);
    route_clear(&run.model);
    return rc;
}
