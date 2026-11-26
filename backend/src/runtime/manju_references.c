#ifndef _WIN32
#define _XOPEN_SOURCE 700
#endif
#include "runtime/manju_references.h"
#include "api/websocket.h"
#include "infra/util.h"
#include "llm/image.h"
#include "os/os_fs.h"
#include "os/os_time.h"

#include <ctype.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define REF_PATH_CAP 2048
#define REF_IMAGE_MAX (4u * 1024u * 1024u)
#define REF_TOTAL_MAX (12u * 1024u * 1024u)
#define REF_COUNT_MAX 9
#define REF_MANIFEST_MAX (16u * 1024u)
#define REF_DIR "manju/references"

typedef struct reference_image {
    unsigned char *data;
    size_t size;
    char file[28];
} reference_image;

static const char *field(const cJSON *o, const char *key) {
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, key));
}

int manju_references_valid_key(const char *key) {
    return key && strlen(key) == MANJU_REFERENCE_KEY_CAP - 1 &&
           strspn(key, "0123456789abcdef") == MANJU_REFERENCE_KEY_CAP - 1;
}

static int valid_name(const char *name) {
    size_t size = name ? strlen(name) : 0;
    if (!size || size > 160 || !str_utf8_valid_n(name, (long long)size) ||
        isspace((unsigned char)name[0]) || isspace((unsigned char)name[size - 1])) return 0;
    for (size_t i = 0; i < size; i++)
        if (iscntrl((unsigned char)name[i])) return 0;
    return 1;
}

static char *canonical(const char *path) {
#ifdef _WIN32
    return fs_windows_final_path(path);
#else
    return realpath(path, NULL);
#endif
}

static int within(const char *base, const char *path) {
    size_t n = base ? strlen(base) : 0;
    return n && path && !strncmp(base, path, n) &&
           (base[n - 1] == '/' || path[n] == '/' || !path[n]);
}

static char *reference_directory(const char *workspace, int create) {
    char *root = canonical(workspace);
    char path[REF_PATH_CAP];
    if (!root || strlen(root) + 140 >= sizeof path) { free(root); return NULL; }
    path_join(path, sizeof path, root, "manju");
    if (!fs_exists(path) && create) fs_mkdirs(path);
    char *manju = canonical(path);
    if (!within(root, manju) || strlen(manju) + 140 >= sizeof path || !fs_is_dir(path)) {
        free(manju);
        free(root);
        return NULL;
    }
    path_join(path, sizeof path, manju, "references");
    if (!fs_exists(path) && create) fs_mkdirs(path);
    char *directory = canonical(path);
    if (!within(manju, directory) || strlen(directory) + 100 >= sizeof path || !fs_is_dir(path)) {
        free(directory);
        directory = NULL;
    }
    free(manju);
    free(root);
    return directory;
}

static int decode_image(const char *url, reference_image *image) {
    static const struct { const char *mime, *extension; } types[] = {
        {"image/png", ".png"}, {"image/jpeg", ".jpg"}, {"image/webp", ".webp"}
    };
    for (size_t i = 0; url && i < sizeof types / sizeof *types; i++) {
        char prefix[40];
        int start = snprintf(prefix, sizeof prefix, "data:%s;base64,", types[i].mime);
        if (strncmp(url, prefix, (size_t)start)) continue;
        const char *encoded = url + start;
        size_t n = strlen(encoded);
        if (!n || n % 4 || n > ((REF_IMAGE_MAX + 2) / 3) * 4) return 0;
        size_t symbols = strspn(encoded, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/");
        size_t padding = n - symbols;
        if (padding > 2 || strspn(encoded + symbols, "=") != padding) return 0;
        size_t capacity = n / 4 * 3 - padding;
        if (capacity == 0 || capacity > REF_IMAGE_MAX) return 0;
        image->data = malloc(capacity);
        if (!image->data) return 0;
        if (base64_decode(encoded, image->data, capacity, &image->size) != 0 || image->size != capacity)
            return 0;
        const char *mime = llm_image_mime(image->data, image->size);
        if (!mime || strcmp(mime, types[i].mime)) return 0;
        char hash[MANJU_REFERENCE_KEY_CAP];
        hash_hex(hash, hash64(image->data, image->size));
        snprintf(image->file, sizeof image->file, "%s%s", hash, types[i].extension);
        return 1;
    }
    return 0;
}

/* Never overwrite different content even if two inputs share a fingerprint.
 * The manifest is published only after every image is present. */
static int write_immutable(const char *directory, const char *file, const void *data, size_t n) {
    static atomic_uint sequence;
    char path[REF_PATH_CAP], tmp[REF_PATH_CAP + 80];
    path_join(path, sizeof path, directory, file);
    if (fs_exists(path)) {
        char *actual = canonical(path);
        size_t length = 0;
        int more = 0;
        char *prior = within(directory, actual) && !fs_is_dir(actual) ?
            fs_read_file_slice(actual, 0, n, &length, &more) : NULL;
        int equal = prior && !more && length == n && !memcmp(prior, data, n);
        free(prior);
        free(actual);
        return equal;
    }
    snprintf(tmp, sizeof tmp, "%s.ref-%lld-%u.tmp", path,
             (long long)time_now_us(), atomic_fetch_add(&sequence, 1));
    if (fs_exists(tmp)) return 0;
    int ok = fs_write_file(tmp, data, n) == 0 && fs_file_size(tmp) == (long long)n &&
             fs_replace_file(tmp, path) == 0;
    if (!ok) fs_remove(tmp);
    return ok;
}

int manju_references_import(const char *workspace, const cJSON *input,
                            char key[MANJU_REFERENCE_KEY_CAP], const char **problem) {
    reference_image images[REF_COUNT_MAX] = {0};
    cJSON *manifest = NULL;
    char *json = NULL, *directory = NULL;
    int count = cJSON_GetArraySize(input), status = 400;
    size_t total = 0;
    *key = 0;
    *problem = "人物参考图需为 1–9 张 PNG、JPEG 或 WebP，每张至多 4 MiB，合计至多 12 MiB";
    if (!cJSON_IsArray(input) || count < 1 || count > REF_COUNT_MAX) goto done;
    manifest = cJSON_CreateArray();
    if (!manifest) { status = 500; goto done; }
    for (int i = 0; i < count; i++) {
        const cJSON *source = cJSON_GetArrayItem(input, i);
        const char *name = field(source, "name");
        if (!valid_name(name)) { *problem = "请为参考图填写角色名称，名称不能超过 160 个 UTF-8 字节"; goto done; }
        reference_image *image = &images[i];
        if (!decode_image(field(source, "data_url"), image) || image->size > REF_TOTAL_MAX - total)
            goto done;
        total += image->size;
        cJSON *entry = cJSON_CreateObject();
        if (!entry || !cJSON_AddStringToObject(entry, "name", name) ||
            !cJSON_AddStringToObject(entry, "file", image->file) || !cJSON_AddItemToArray(manifest, entry)) {
            cJSON_Delete(entry);
            status = 500;
            goto done;
        }
    }
    status = 500;
    json = cJSON_PrintUnformatted(manifest);
    directory = json ? reference_directory(workspace, 1) : NULL;
    *problem = "无法保存人物参考图，请检查工作区权限或文件冲突";
    if (!directory) goto done;
    /* Protect the compare-then-publish sequence across concurrent imports. */
    static atomic_flag publishing = ATOMIC_FLAG_INIT;
    while (atomic_flag_test_and_set(&publishing)) time_sleep_ms(5);
    int ok = 1;
    for (int i = 0; ok && i < count; i++)
        ok = write_immutable(directory, images[i].file, images[i].data, images[i].size);
    if (ok) {
        size_t size = strlen(json);
        hash_hex(key, hash64(json, size));
        char basename[32];
        snprintf(basename, sizeof basename, "%s.json", key);
        if (write_immutable(directory, basename, json, size)) status = 0;
    }
    atomic_flag_clear(&publishing);
done:
    if (status) *key = 0;
    for (int i = 0; i < REF_COUNT_MAX; i++) free(images[i].data);
    cJSON_Delete(manifest);
    free(json);
    free(directory);
    return status;
}

cJSON *manju_references_load(const char *workspace, const char *key) {
    if (!manju_references_valid_key(key)) return NULL;
    char *directory = reference_directory(workspace, 0);
    if (!directory) return NULL;
    char basename[32], path[REF_PATH_CAP];
    snprintf(basename, sizeof basename, "%s.json", key);
    path_join(path, sizeof path, directory, basename);
    char *actual = canonical(path);
    size_t size = 0;
    int more = 0;
    char *json = within(directory, actual) && !fs_is_dir(actual) ?
        fs_read_file_slice(actual, 0, REF_MANIFEST_MAX, &size, &more) : NULL;
    char hash[MANJU_REFERENCE_KEY_CAP];
    if (json) hash_hex(hash, hash64(json, size));
    cJSON *result = json && !more && size == strlen(json) && !strcmp(hash, key) ?
        cJSON_ParseWithOpts(json, NULL, 1) : NULL;
    if (!cJSON_IsArray(result) || cJSON_GetArraySize(result) < 1 ||
        cJSON_GetArraySize(result) > REF_COUNT_MAX) goto invalid;
    cJSON *entry;
    cJSON_ArrayForEach(entry, result) {
        const char *file = field(entry, "file");
        if (!valid_name(field(entry, "name")) || !file || strlen(file) < 20 ||
            strspn(file, "0123456789abcdef") != 16 ||
            (strcmp(file + 16, ".png") && strcmp(file + 16, ".jpg") && strcmp(file + 16, ".webp"))) goto invalid;
        path_join(path, sizeof path, directory, file);
        char *image = canonical(path);
        size_t bytes = 0;
        int extra = 0;
        char *data = within(directory, image) && !fs_is_dir(image) ?
            fs_read_file_slice(image, 0, REF_IMAGE_MAX, &bytes, &extra) : NULL;
        if (data) hash_hex(hash, hash64(data, bytes));
        int valid = data && bytes && !extra && !strncmp(hash, file, 16) &&
            cJSON_AddStringToObject(entry, "path", image);
        free(data);
        free(image);
        if (!valid) goto invalid;
    }
    goto done;
invalid:
    cJSON_Delete(result);
    result = NULL;
done:
    free(json);
    free(actual);
    free(directory);
    return result;
}

int manju_reference_matches(const cJSON *reference, const cJSON *character) {
    const char *wanted = field(reference, "name");
    const char *name = field(character, "name"), *id = field(character, "id");
    return wanted && ((name && !strcmp(name, wanted)) || (id && !strcmp(id, wanted)));
}

int manju_references_cover(const cJSON *references, const cJSON *character) {
    const cJSON *entry;
    cJSON_ArrayForEach(entry, references)
        if (manju_reference_matches(entry, character)) return 1;
    return 0;
}

int manju_references_resolve(const cJSON *references, const cJSON *board) {
    const cJSON *entry, *cast = cJSON_GetObjectItemCaseSensitive(board, "characters");
    cJSON_ArrayForEach(entry, references) {
        int matches = 0;
        const cJSON *character;
        cJSON_ArrayForEach(character, cast) matches += manju_reference_matches(entry, character);
        if (matches > 1) return 0;
    }
    return 1;
}
