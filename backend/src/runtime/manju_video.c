#ifndef _WIN32
#define _XOPEN_SOURCE 700
#endif
#include "runtime/manju_video.h"
#include "runtime/manju_paint.h"
#include "runtime/manju_references.h"
#include "infra/util.h"
#include "llm/video.h"
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

#define VIDEO_PATH_CAP 2048
#define VIDEO_SHOTS_MAX 180
#define VIDEO_WORKERS 2
#define VIDEO_WAIT_MS (15 * 60 * 1000)

typedef struct clip {
    const char *kind;
    char *prompt;
    char *first_frame_prompt;
    char *media[9];
    size_t media_count;
    const route *model;
    char file[64];
    char error[224];
    int ready;
} clip;

typedef struct clip_run {
    route models[3];
    const char *directory;
    const char *workspace, *episode_dir, *image_route;
    router *routes;
    int seconds, total, requested;
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
    if (run->progress) run->progress(run->ready, run->requested, run->requests, run->context);
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
    int status = llm_video_submit_media(entry->model, entry->prompt, run->seconds,
                                        (const char *const *)entry->media, entry->media_count,
                                        task_id, error);
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

static char *generate_frame(router *routes, const char *route_name, const char *root,
                            const char *episode_dir, const char *prompt);

static void generate_clip(clip_run *run, clip *entry, int index) {
    char key[17], path[VIDEO_PATH_CAP], checkpoint[VIDEO_PATH_CAP], job[80], task_id[128];
    char *error = NULL, *url = NULL;
    strbuf identity;
    strbuf_init(&identity);
    if (entry->first_frame_prompt) {
        entry->media[0] = generate_frame(run->routes, run->image_route, run->workspace,
                                         run->episode_dir, entry->first_frame_prompt);
        if (!entry->media[0]) {
            error = xstrdup("无法生成首帧，请检查文生图路由或给镜头指定 first_frame");
            goto done;
        }
        entry->media_count = 1;
    }
    strbuf_appendf(&identity, "%s\n%s\n%d\n%s", entry->model->base_url ? entry->model->base_url : "",
                   entry->model->model, run->seconds, entry->prompt);
    for (size_t i = 0; i < entry->media_count; i++) {
        size_t bytes = 0;
        int more = 0;
        char *data = fs_read_file_slice(entry->media[i], 0, 4 * 1024 * 1024, &bytes, &more);
        if (!data || !bytes || more) {
            free(data);
            error = xstrdup("参考素材不可读或超过 4 MiB");
            goto done;
        }
        strbuf_appendf(&identity, "\n%llu", (unsigned long long)hash64(data, bytes));
        free(data);
    }
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
        int poll = llm_video_poll(entry->model, task_id, &url, &error);
        if (poll == 1) {
            if (llm_video_download(entry->model, url, path, &error) == 0)
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
        if (run->clips[i].model) generate_clip(run, &run->clips[i], i);
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

static char *media_path(const char *root, const char *base, const char *name) {
    char path[VIDEO_PATH_CAP];
    if (!name || !*name || strlen(base) + strlen(name) + 2 >= sizeof path) return NULL;
    path_resolve(path, sizeof path, base, name);
    if (!inside(root, path) || fs_is_dir(path)) return NULL;
    return existing_path(path);
}

static int video_kind(const char *kind) {
    if (!kind) return -1;
    if (!strcmp(kind, "text")) return LLM_VIDEO_TEXT;
    if (!strcmp(kind, "first_frame")) return LLM_VIDEO_FIRST_FRAME;
    if (!strcmp(kind, "reference")) return LLM_VIDEO_REFERENCE;
    return -1;
}

static char *generate_frame(router *routes, const char *route_name, const char *root,
                            const char *episode_dir, const char *prompt) {
    route image = {0};
    llm_image_result generated = {0};
    strbuf signature;
    strbuf_init(&signature);
    char path[VIDEO_PATH_CAP], dir[VIDEO_PATH_CAP], name[64], key[17];
    char *out = NULL;
    if (router_select_image(routes, route_name, &image) != 0) goto done;
    path_join(dir, sizeof dir, episode_dir, "keyframes");
    if (fs_mkdirs(dir) != 0 || !inside(root, dir)) goto done;
    strbuf_appendf(&signature, "%s\n%s\n%s", image.base_url ? image.base_url : "", image.model, prompt);
    if (!signature.buf) goto done;
    hash_hex(key, hash64(signature.buf, signature.len));
    const char *ext[] = {".png", ".jpg", ".webp"};
    const char *mime[] = {"image/png", "image/jpeg", "image/webp"};
    for (size_t i = 0; i < 3; i++) {
        snprintf(name, sizeof name, "%s%s", key, ext[i]);
        char *cached = media_path(root, dir, name);
        if (!cached) continue;
        size_t length = 0;
        int more = 0;
        char *data = fs_read_file_slice(cached, 0, 4 * 1024 * 1024, &length, &more);
        const char *found = data && !more ? llm_image_mime((unsigned char *)data, length) : NULL;
        int valid = found && !strcmp(found, mime[i]);
        free(data);
        if (valid) { out = cached; goto done; }
        free(cached);
    }
    const char *size = strstr(image.model, "qwen-image-") ? "576x1024" : NULL;
    if (llm_image_generate(&image, prompt, size, &generated) != 0 ||
        !generated.len || generated.len > 4 * 1024 * 1024) goto done;
    for (size_t i = 0; i < 3; i++) {
        if (!generated.mime || strcmp(generated.mime, mime[i])) continue;
        snprintf(name, sizeof name, "%s%s", key, ext[i]);
        path_join(path, sizeof path, dir, name);
        static atomic_uint seq;
        char temp[VIDEO_PATH_CAP + 80];
        snprintf(temp, sizeof temp, "%s.tmp-%lld-%u", path, (long long)time_now_us(), atomic_fetch_add(&seq, 1));
        if (!fs_write_file(temp, generated.data, generated.len) &&
            fs_file_size(temp) == (long long)generated.len && !fs_replace_file(temp, path))
            out = existing_path(path);
        else
            fs_remove(temp);
        break;
    }
done:
    strbuf_free(&signature);
    llm_image_result_free(&generated);
    route_clear(&image);
    return out;
}

static const cJSON *cast_member(const cJSON *board, const char *id) {
    const cJSON *cast = cJSON_GetObjectItemCaseSensitive(board, "characters"), *character;
    cJSON_ArrayForEach(character, cast) {
        const char *key = field(character, "id");
        if (id && key && !strcmp(id, key)) return character;
    }
    return NULL;
}

static int shot_has_uploaded_references(const cJSON *board, const cJSON *shot, const cJSON *references) {
    const cJSON *id;
    cJSON_ArrayForEach(id, cJSON_GetObjectItemCaseSensitive(shot, "character_ids"))
        if (manju_references_cover(references, cast_member(board, cJSON_GetStringValue(id)))) return 1;
    return 0;
}

static int clip_media(clip *entry, int kind, const cJSON *shot, const cJSON *assets, const cJSON *references,
                      const char *root, const char *episode_dir, const cJSON *board,
                      const char *visual) {
    if (kind == LLM_VIDEO_TEXT) return 0;
    if (kind == LLM_VIDEO_FIRST_FRAME) {
        const char *name = field(shot, "first_frame");
        if (name) {
            entry->media[0] = media_path(root, root, name);
            if (!entry->media[0]) return -1;
            entry->media_count = 1;
        } else {
            strbuf first;
            strbuf_init(&first);
            strbuf_appendf(&first, "绘制一张 9:16 竖版漫画首帧，不含标题和字幕。\n%s",
                           visual && *visual ? visual : entry->prompt);
            manju_append_cast(&first, board, shot);
            entry->first_frame_prompt = strbuf_detach(&first);
            if (!entry->first_frame_prompt) return -1;
        }
        return 0;
    }
    const char *names[9] = {0};
    const cJSON *explicit_refs = cJSON_GetObjectItemCaseSensitive(shot, "reference_images");
    if (explicit_refs) {
        if (!cJSON_IsArray(explicit_refs) || cJSON_GetArraySize(explicit_refs) > 9) return -1;
        const cJSON *item;
        cJSON_ArrayForEach(item, explicit_refs) {
            char *path = media_path(root, root, cJSON_GetStringValue(item));
            if (!path) return -1;
            entry->media[entry->media_count++] = path;
        }
    } else {
        char characters[VIDEO_PATH_CAP];
        path_join(characters, sizeof characters, episode_dir, "characters");
        const cJSON *id;
        cJSON_ArrayForEach(id, cJSON_GetObjectItemCaseSensitive(shot, "character_ids")) {
            const char *wanted = cJSON_GetStringValue(id);
            const cJSON *character = cast_member(board, wanted);
            const char *name = field(character, "name");
            int uploaded = 0;
            const cJSON *reference;
            cJSON_ArrayForEach(reference, references) {
                if (!manju_reference_matches(reference, character)) continue;
                uploaded = 1;
                char *path = media_path(root, root, field(reference, "path"));
                if (!path) return -1;
                int seen = 0;
                for (size_t n = 0; n < entry->media_count; n++)
                    if (names[n] == name && !strcmp(entry->media[n], path)) seen = 1;
                if (seen || entry->media_count >= 9) {
                    free(path);
                    if (seen) continue;
                    return -1;
                }
                names[entry->media_count] = name;
                entry->media[entry->media_count++] = path;
            }
            if (uploaded) continue;
            const cJSON *asset;
            cJSON_ArrayForEach(asset, cJSON_GetObjectItemCaseSensitive(assets, "characters")) {
                const char *key = field(asset, "id");
                if (!key || !wanted || strcmp(key, wanted)) continue;
                if (entry->media_count >= 9) return -1;
                char *path = media_path(root, characters, field(asset, "file"));
                if (!path) return -1;
                names[entry->media_count] = name ? name : field(asset, "name");
                entry->media[entry->media_count++] = path;
                break;
            }
        }
    }
    if (!entry->media_count) return -1;
    strbuf linked;
    strbuf_init(&linked);
    strbuf_append(&linked, entry->prompt);
    for (size_t i = 0; i < entry->media_count; i++) {
        if (!references) strbuf_appendf(&linked,
            "\n[Image %zu] 是依次出现的角色或场景参考，保持其造型、颜色和典型细节。"
            "若参考为三视图，这是同一角色的三个角度，画面中只出现一个该角色。", i + 1);
        else if (names[i]) strbuf_appendf(&linked, "\n[Image %zu] 对应角色「%s」。", i + 1, names[i]);
        else strbuf_appendf(&linked, "\n[Image %zu] 是依次出现的角色或场景参考。", i + 1);
    }
    if (references) strbuf_append(&linked, "\n按各参考图保持造型、颜色和典型细节。三视图是同一角色的三个角度。"
                           "每个角色只出现一次，不交换不同角色的造型。");
    if (linked.len > 7500) { strbuf_free(&linked); return -1; }
    char *prompt = strbuf_detach(&linked);
    if (!prompt) return -1;
    free(entry->prompt);
    entry->prompt = prompt;
    return 0;
}

static int video_episode(router *routes, const char *route_name, const char *const names[3],
                         const char *image_route, const char *workspace, int episode,
                         int seconds, const char *reference_set, int use_generated_references,
                         int (*abort_check)(void *), manju_video_progress progress,
                         void *context, char **result) {
    char relative[64], directory[VIDEO_PATH_CAP], path[VIDEO_PATH_CAP];
    char *root = NULL, *canonical_directory = NULL, *text = NULL, *manifest = NULL;
    cJSON *board = NULL, *doc = NULL, *assets = NULL, *references = NULL;
    clip_run run = {.seconds = seconds, .routes = routes, .image_route = image_route,
                    .abort_check = abort_check, .progress = progress, .context = context};
    const char *error = "无法准备视频分镜";
    strbuf report;
    strbuf_init(&report);
    int rc = -1, preferred = LLM_VIDEO_TEXT, requested = 0;
    if ((seconds != 3 && seconds != 4) || episode < 1 || episode > 500) goto done;
    error = "未找到匹配的视频路由，请在模型路由中配置并测试";
    if (names) {
        for (int k = 0; k < 3; k++)
            if (!names[k] || router_select_video(routes, names[k], &run.models[k]) != 0 ||
                (int)llm_video_input_kind(&run.models[k]) != k) goto done;
    } else {
        route selected = {0};
        if (router_select_video(routes, route_name, &selected) != 0) goto done;
        preferred = llm_video_input_kind(&selected);
        run.models[preferred] = selected;
    }
    root = existing_path(workspace && *workspace ? workspace : ".");
    if (!root || strlen(root) + 200 >= sizeof directory) goto done;
    snprintf(relative, sizeof relative, "manju/ep%02d", episode);
    path_join(directory, sizeof directory, root, relative);
    char episode_dir[VIDEO_PATH_CAP];
    snprintf(episode_dir, sizeof episode_dir, "%s", directory);
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
    if (reference_set && *reference_set) {
        references = manju_references_load(root, reference_set);
        if (!references || !manju_references_resolve(references, board)) {
            error = "无法读取人物参考图，或参考图名称对应多名角色";
            goto done;
        }
    }
    path_join(path, sizeof path, episode_dir, "characters/characters.json");
    if ((!references || use_generated_references) && inside(root, path)) {
        size_t asset_bytes = 0;
        int extra = 0;
        char *json = fs_read_file_slice(path, 0, 512 * 1024, &asset_bytes, &extra);
        if (json && !extra && asset_bytes == strlen(json)) assets = cJSON_ParseWithOpts(json, NULL, 1);
        free(json);
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
        /* Reuse the nearest clip for unspecified cards to avoid extra cloud calls. */
        if ((!i || last) && ((!visual || !*visual) || (!names && preferred != LLM_VIDEO_TEXT))) continue;
        int mode = preferred;
        if (names && shot) {
            const char *explicit_mode = field(shot, "video_mode");
            if (explicit_mode) mode = video_kind(explicit_mode);
            else if (field(shot, "first_frame")) mode = LLM_VIDEO_FIRST_FRAME;
            else if (cJSON_GetObjectItemCaseSensitive(shot, "reference_images") ||
                     (assets && cJSON_GetArraySize(cJSON_GetObjectItemCaseSensitive(shot, "character_ids"))))
                mode = LLM_VIDEO_REFERENCE;
            if (mode >= 0 && !field(shot, "first_frame") &&
                !cJSON_GetObjectItemCaseSensitive(shot, "reference_images") &&
                shot_has_uploaded_references(board, shot, references)) mode = LLM_VIDEO_REFERENCE;
        }
        if (mode < 0 || !run.models[mode].model ||
            clip_media(&run.clips[i], mode, shot, assets, references, root, episode_dir, board, visual) != 0) {
            strbuf_appendf(&report, "镜头 %d 的视频模式无效、首帧生成失败或缺少对应角色的参考素材。", i);
            goto done;
        }
        run.clips[i].model = &run.models[mode];
        requested++;
    }
    snprintf(relative, sizeof relative, "manju/ep%02d/clips", episode);
    path_join(directory, sizeof directory, root, relative);
    if (fs_mkdirs(directory) != 0 || !inside(root, directory) ||
        !(canonical_directory = existing_path(directory))) {
        error = "无法准备短视频目录";
        goto done;
    }
    run.directory = canonical_directory;
    run.workspace = root;
    run.episode_dir = episode_dir;
    run.requested = requested;
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
    if (run.ready != requested) {
        strbuf_appendf(&report, "短视频 %d/%d 准备完成，已提交 %d 次。继续时复用有效片段和任务编号。",
                       run.ready, requested, run.requests);
        for (int i = 0; i < run.total; i++)
            if (run.clips[i].error[0])
                strbuf_appendf(&report, "\n镜头 %d：%s", i, run.clips[i].error);
        goto done;
    }
    if (!run.clips[0].model) {
        strcpy(run.clips[0].file, run.clips[1].file);
        run.clips[0].model = run.clips[1].model;
    }
    if (!run.clips[count + 1].model) {
        strcpy(run.clips[count + 1].file, run.clips[count].file);
        run.clips[count + 1].model = run.clips[count].model;
    }
    char fingerprint[17];
    hash_hex(fingerprint, hash64(text, bytes));
    doc = cJSON_CreateObject();
    cJSON *entries = doc ? cJSON_AddArrayToObject(doc, "clips") : NULL;
    if (!entries || !cJSON_AddNumberToObject(doc, "ep", episode) ||
        !cJSON_AddStringToObject(doc, "title", title) ||
        !cJSON_AddStringToObject(doc, "video_model", names ? "mixed" : run.models[preferred].model) ||
        !cJSON_AddStringToObject(doc, "storyboard_fingerprint", fingerprint) ||
        !cJSON_AddNumberToObject(doc, "clip_seconds", seconds)) goto done;
    for (int i = 0; i < run.total; i++) {
        cJSON *item = cJSON_CreateObject();
        if (!item || !cJSON_AddStringToObject(item, "file", run.clips[i].file) ||
            !cJSON_AddStringToObject(item, "role", !i ? "title" : i == run.total - 1 ? "end" : "shot") ||
            !cJSON_AddStringToObject(item, "kind", run.clips[i].kind) ||
            !cJSON_AddStringToObject(item, "model", run.clips[i].model->model) ||
            !cJSON_AddItemToArray(entries, item)) {
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
                   names ? "混合视频路由" : run.models[preferred].model, requested, seconds, run.requests, path);
    rc = 0;
done:
    *result = report.len ? strbuf_detach(&report) : xstrdup(error);
    strbuf_free(&report);
    for (int i = 0; run.clips && i < run.total; i++) {
        free(run.clips[i].prompt);
        free(run.clips[i].first_frame_prompt);
        for (size_t k = 0; k < run.clips[i].media_count; k++) free(run.clips[i].media[k]);
    }
    free(run.clips);
    free(manifest);
    free(text);
    free(canonical_directory);
    free(root);
    cJSON_Delete(doc);
    cJSON_Delete(board);
    cJSON_Delete(assets);
    cJSON_Delete(references);
    for (int i = 0; i < 3; i++) route_clear(&run.models[i]);
    return rc;
}

int manju_video_episode_mixed(router *routes, const char *const names[3],
                              const char *image_route, const char *workspace, int episode,
                              int seconds, const char *reference_set, int use_generated_references,
                              int (*abort_check)(void *),
                              manju_video_progress progress, void *context, char **result) {
    return video_episode(routes, NULL, names, image_route, workspace,
                         episode, seconds, reference_set, use_generated_references,
                         abort_check, progress, context, result);
}

int manju_video_episode_selected(router *routes, const char *route_name, const char *image_route,
                                 const char *workspace, int episode, int seconds,
                                 const char *reference_set, int use_generated_references,
                                 int (*abort_check)(void *), manju_video_progress progress,
                                 void *context, char **result) {
    return video_episode(routes, route_name, NULL, image_route, workspace, episode, seconds,
                         reference_set, use_generated_references, abort_check, progress, context, result);
}

int manju_video_episode(router *routes, const char *route_name, const char *workspace,
                        int episode, int seconds, int (*abort_check)(void *),
                        manju_video_progress progress, void *context, char **result) {
    return video_episode(routes, route_name, NULL, NULL, workspace, episode, seconds,
                         NULL, 0, abort_check, progress, context, result);
}
