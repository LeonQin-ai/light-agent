/* Regression: formatting-only JSON changes must not let a repeated write
 * plan consume the full planning budget. */
#include "action/tools.h"
#include "cognition/reasoning.h"
#include "llm/llm.h"
#include "os/os_fs.h"
#include "os/os_proc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"

/* The legacy protocol requires an explicit completion declaration; plain
 * prose is only commentary. Fixtures therefore mark their intended final
 * answer, exactly like a real model answering under this protocol. */
static void fixture_final_decision(const llm_request *req, llm_response *resp) {
    if (!req->structured_decision || !resp->content || resp->tool_actions_json) return;
    const char *p = resp->content;
    while (*p==' ' || *p=='\n' || *p=='\r' || *p=='\t') p++;
    if (*p=='[' || *p=='{' || *p=='<') return;
    cJSON *array=cJSON_CreateArray(), *call=cJSON_CreateObject();
    cJSON_AddStringToObject(call,"tool","complete_task");
    cJSON *args=cJSON_AddObjectToObject(call,"args");
    cJSON_AddStringToObject(args,"status","complete");
    cJSON_AddBoolToObject(args,"requires_tools",0);
    cJSON_AddStringToObject(args,"answer",resp->content);
    cJSON_AddObjectToObject(args,"acceptance");
    cJSON_AddItemToArray(array,call);
    char *encoded=cJSON_PrintUnformatted(array);
    cJSON_Delete(array);
    if (encoded) { free(resp->content); resp->content=encoded; }
}

typedef struct cycle_fixture {
    int calls;
} cycle_fixture;

static char *copy_text(const char *text) {
    size_t n = strlen(text) + 1;
    char *copy = malloc(n);
    if (copy) memcpy(copy, text, n);
    return copy;
}

static int cycle_chat(llm *self, const llm_request *req, llm_response *resp) {
    (void)req;
    cycle_fixture *fixture = self ? self->impl : NULL;
    if (!fixture) return -1;
    fixture->calls++;
    if (fixture->calls <= 3) {
        const char *plan = fixture->calls % 2
            ? "[{\"tool\":\"file_write\",\"args\":{\"path\":\"repeat.txt\",\"content\":\"same\"}}]"
            : "[{\"tool\":\"file_write\",\"args\":{\"content\":\"same\",\"path\":\"repeat.txt\"}}]";
        resp->content = copy_text(plan);
    } else {
        resp->content = copy_text("检测到重复写入后已停止，任务未完成。");
    }
    return resp->content ? 0 : -1;
}

typedef struct recovery_fixture {
    int calls;
    int failed_actions;
} recovery_fixture;

typedef struct repeated_read_fixture {
    int calls;
} repeated_read_fixture;

static int repeated_read_chat(llm *self, const llm_request *req, llm_response *resp) {
    (void)req;
    repeated_read_fixture *fixture = self ? self->impl : NULL;
    if (!fixture) return -1;
    fixture->calls++;
    const char *answer = fixture->calls <= 6
        ? "[{\"tool\":\"file_read\",\"args\":{\"path\":\"same.txt\"}}]"
        : fixture->calls == 7
            ? "[{\"tool\":\"file_read\",\"args\":{\"path\":\"new.txt\"}}]"
            : "复核完成：已继续读取另一份证据。";
    resp->content = copy_text(answer);
    if (fixture->calls > 7) fixture_final_decision(req, resp);
    return resp->content ? 0 : -1;
}

static int recovery_chat(llm *self, const llm_request *req, llm_response *resp) {
    (void)req;
    recovery_fixture *fixture = self ? self->impl : NULL;
    if (!fixture) return -1;
    fixture->calls++;
    const char *answer = NULL;
    switch (fixture->calls) {
    case 1: answer = "[{\"tool\":\"file_read\",\"args\":{\"path\":\"same.txt\"}}]"; break;
    case 2: answer = "[{\"tool\":\"fixture_failure\",\"args\":{}}]"; break;
    case 3: answer = "I'll investigate the failed check now."; break;
    case 4: answer = "[{\"tool\":\"fixture_failure\",\"args\":{}}]"; break;
    default: answer = "失败原因已核对并继续执行成功"; break;
    }
    resp->content = copy_text(answer);
    if (fixture->calls >= 5) fixture_final_decision(req, resp);
    return resp->content ? 0 : -1;
}

typedef struct probe_fixture {
    int calls;
    int shell_execs;
} probe_fixture;

/* Round 2 repeats the round-1 probe with no intervening write, so the runtime
 * must reuse the observation. Round 3 writes a file, invalidating the cache,
 * so the round-4 probe has to execute again (#272). */
static int probe_chat(llm *self, const llm_request *req, llm_response *resp) {
    (void)req;
    probe_fixture *fixture = self ? self->impl : NULL;
    if (!fixture) return -1;
    fixture->calls++;
    const char *plan = NULL;
    switch (fixture->calls) {
    case 1: plan = "[{\"tool\":\"shell\",\"args\":{\"command\":\"git status --short && git rev-parse HEAD\"}}]"; break;
    case 2: plan = "[{\"tool\":\"shell\",\"args\":{\"command\":\"git status --short && git rev-parse HEAD\"}}]"; break;
    case 3: plan = "[{\"tool\":\"file_write\",\"args\":{\"path\":\"probe-invalidate.txt\",\"content\":\"changed\"}}]"; break;
    case 4: plan = "[{\"tool\":\"shell\",\"args\":{\"command\":\"git status --short && git rev-parse HEAD\"}}]"; break;
    default: plan = "探针复用验证完成。"; break;
    }
    resp->content = copy_text(plan);
    if (fixture->calls >= 5) fixture_final_decision(req, resp);
    return resp->content ? 0 : -1;
}

static tool_result *probe_shell(const tool *self, const tool_ctx *ctx, const char *args) {
    (void)ctx;
    (void)args;
    probe_fixture *fixture = self ? self->ud : NULL;
    if (fixture) fixture->shell_execs++;
    return tool_result_new(1, "workspace root");
}

typedef struct timeout_fixture {
    int calls;
    int nudge_seen;
    int prompt_ok;
} timeout_fixture;

/* Two distinctly-named verification scripts time out; the runtime must nudge
 * once instead of letting the planner write endless replacements (#273). It
 * must also describe the actual shell backend in the system prompt (#274). */
static int timeout_chat(llm *self, const llm_request *req, llm_response *resp) {
    timeout_fixture *fixture = self ? self->impl : NULL;
    if (!fixture) return -1;
    fixture->calls++;
    char shell_desc[128];
    proc_shell_describe(shell_desc, sizeof(shell_desc));
    for (size_t i = 0; i < req->num_messages; i++) {
        const char *role = req->messages[i].role;
        const char *body = req->messages[i].content ? req->messages[i].content : "";
        if (role && !strcmp(role, "system")) {
            if (strstr(body, "Evidence parity") && strstr(body, shell_desc))
                fixture->prompt_ok = 1;
        }
        if (strstr(body, "验证脚本已连续超时两次"))
            fixture->nudge_seen = 1;
    }
    const char *answer = NULL;
    switch (fixture->calls) {
    case 1: answer = "[{\"tool\":\"shell\",\"args\":{\"command\":\"sleep 61\"}}]"; break;
    case 2: answer = "[{\"tool\":\"shell\",\"args\":{\"command\":\"sleep 62\"}}]"; break;
    default: answer = "已改用原子工具分步验证。"; break;
    }
    resp->content = copy_text(answer);
    if (fixture->calls >= 3) fixture_final_decision(req, resp);
    return resp->content ? 0 : -1;
}

static tool_result *timeout_shell(const tool *self, const tool_ctx *ctx, const char *args) {
    (void)ctx;
    (void)args;
    timeout_fixture *fixture = self ? self->ud : NULL;
    (void)fixture;
    return tool_result_new(0, "[timeout] verification script exceeded the command budget");
}

static tool_result *fixture_failure(const tool *self, const tool_ctx *ctx, const char *args) {
    (void)ctx;
    (void)args;
    recovery_fixture *fixture = self ? self->ud : NULL;
    if (fixture && fixture->failed_actions++ > 0)
        return tool_result_new(1, "fixture command succeeded on retry");
    return tool_result_new(0, "fixture failure: command exited with status 1");
}

static int check(int condition, const char *label) {
    if (condition) {
        printf("PASS %s\n", label);
        return 0;
    }
    printf("FAIL %s\n", label);
    return 1;
}

int main(void) {
    static const llm_vtable vtable = {.chat = cycle_chat};
    cycle_fixture fixture = {0};
    llm provider = {.vt = &vtable, .provider = "fixture", .model = "fixture", .impl = &fixture};
    const char *workspace = "build/test-plan-cycle-work";
    char file_path[256];
    snprintf(file_path, sizeof(file_path), "%s/repeat.txt", workspace);
    fs_mkdirs(workspace);
    fs_remove(file_path);

    tool_registry *registry = tool_registry_new();
    if (!registry || tool_register(registry, tool_file_write()) != 0) {
        tool_registry_free(registry);
        return 2;
    }

    reasoning_config cfg = {0};
    cfg.llm = &provider;
    cfg.tools = registry;
    cfg.workspace = workspace;
    cfg.max_rounds = -1;
    reasoning *engine = reasoning_new(&cfg);
    if (!engine) {
        tool_registry_free(registry);
        return 2;
    }

    char *answer = NULL;
    int rc = reasoning_run(engine, "创建 repeat.txt 并写入 same", &answer);
    char *written = fs_read_file(file_path);
    int failures = 0;
    failures += check(rc != 0, "stalled run is marked incomplete");
    failures += check(answer && strstr(answer, "任务未完成") != NULL,
                      "user receives an incomplete result");
    failures += check(fixture.calls == 4,
                      "three repeated plans stop before the unlimited round budget");
    failures += check(written && strcmp(written, "same") == 0,
                      "the first successful write remains available");

    free(written);
    free(answer);
    reasoning_free(engine);
    tool_registry_free(registry);
    fs_remove(file_path);
    fs_remove(workspace);

    /* Re-reading the same source may be a legitimate recovery step in a
     * multi-step review. Allow the planner's duplicate-read recovery window
     * to run instead of aborting after only two skipped read rounds. */
    const char *read_workspace = "build/test-plan-repeat-read";
    char same_read_path[256], new_read_path[256];
    snprintf(same_read_path, sizeof(same_read_path), "%s/same.txt", read_workspace);
    snprintf(new_read_path, sizeof(new_read_path), "%s/new.txt", read_workspace);
    fs_mkdirs(read_workspace);
    fs_write_file(same_read_path, "same evidence", 13);
    fs_write_file(new_read_path, "additional evidence", 19);
    repeated_read_fixture repeated = {0};
    llm repeated_provider = {
        .vt = &(const llm_vtable){.chat = repeated_read_chat},
        .provider = "fixture", .model = "fixture", .impl = &repeated
    };
    tool_registry *read_registry = tool_registry_new();
    if (!read_registry || tool_register(read_registry, tool_file_read()) != 0) {
        tool_registry_free(read_registry);
        fs_remove(same_read_path); fs_remove(new_read_path); fs_remove(read_workspace);
        return 2;
    }
    reasoning_config read_cfg = {0};
    read_cfg.llm = &repeated_provider;
    read_cfg.tools = read_registry;
    read_cfg.workspace = read_workspace;
    read_cfg.max_rounds = -1;
    reasoning *read_engine = reasoning_new(&read_cfg);
    if (!read_engine) {
        tool_registry_free(read_registry);
        fs_remove(same_read_path); fs_remove(new_read_path); fs_remove(read_workspace);
        return 2;
    }
    char *read_answer = NULL;
    int read_rc = reasoning_run(read_engine, "复核源码并根据证据汇报", &read_answer);
    failures += check(read_rc == 0 && read_answer && strstr(read_answer, "复核完成"),
                      "duplicate-read recovery can continue to a later valid evidence step");
    failures += check(repeated.calls == 9,
                      "planner does not abort after two repeated-read rounds");
    free(read_answer);
    reasoning_free(read_engine);
    tool_registry_free(read_registry);
    fs_remove(same_read_path); fs_remove(new_read_path); fs_remove(read_workspace);

    /* A prior successful read must not make a later failed action's
     * work-in-progress narration look like a completed answer. */
    const char *recovery_workspace = "build/test-plan-recovery";
    char same_path[256], new_path[256];
    snprintf(same_path, sizeof(same_path), "%s/same.txt", recovery_workspace);
    snprintf(new_path, sizeof(new_path), "%s/new.txt", recovery_workspace);
    fs_mkdirs(recovery_workspace);
    fs_write_file(same_path, "old evidence", 12);
    fs_write_file(new_path, "new evidence", 12);
    recovery_fixture recovery = {0};
    llm recovery_provider = {
        .vt = &(const llm_vtable){.chat = recovery_chat},
        .provider = "fixture", .model = "fixture", .impl = &recovery
    };
    tool_registry *recovery_registry = tool_registry_new();
    tool failure_tool = {
        .name = "fixture_failure", .description = "deterministic failure fixture",
        .execute = fixture_failure, .ud = &recovery
    };
    if (!recovery_registry || tool_register(recovery_registry, tool_file_read()) != 0 ||
        tool_register(recovery_registry, &failure_tool) != 0) {
        tool_registry_free(recovery_registry);
        fs_remove(same_path); fs_remove(new_path); fs_remove(recovery_workspace);
        return 2;
    }
    reasoning_config recovery_cfg = {0};
    recovery_cfg.llm = &recovery_provider;
    recovery_cfg.tools = recovery_registry;
    recovery_cfg.workspace = recovery_workspace;
    recovery_cfg.max_rounds = -1;
    reasoning *recovery_engine = reasoning_new(&recovery_cfg);
    if (!recovery_engine) {
        tool_registry_free(recovery_registry);
        fs_remove(same_path); fs_remove(new_path); fs_remove(recovery_workspace);
        return 2;
    }
    char *recovery_answer = NULL;
    int recovery_rc = reasoning_run(recovery_engine, "检查两份状态并汇报结论", &recovery_answer);
    failures += check(recovery_rc == 0 && recovery_answer &&
                      strstr(recovery_answer, "失败原因已核对并继续执行成功"),
                      "planner resumes after narration following a failed action");
    failures += check(recovery.failed_actions == 2 && recovery.calls == 6,
                      "recovery retries the failed action and continues after it succeeds");
    free(recovery_answer);
    reasoning_free(recovery_engine);
    tool_registry_free(recovery_registry);
    fs_remove(same_path); fs_remove(new_path); fs_remove(recovery_workspace);

    /* Read-only probe reuse (#272): an identical successful shell probe is
     * skipped while the workspace is unchanged, and re-executed once a write
     * invalidates the cache. */
    const char *probe_workspace = "build/test-plan-probe-reuse";
    char invalidate_path[256];
    snprintf(invalidate_path, sizeof(invalidate_path), "%s/probe-invalidate.txt", probe_workspace);
    fs_mkdirs(probe_workspace);
    fs_remove(invalidate_path);
    probe_fixture probe = {0};
    llm probe_provider = {
        .vt = &(const llm_vtable){.chat = probe_chat},
        .provider = "fixture", .model = "fixture", .impl = &probe
    };
    tool probe_tool = {
        .name = "shell", .description = "read-only probe fixture",
        .execute = probe_shell, .ud = &probe
    };
    tool_registry *probe_registry = tool_registry_new();
    if (!probe_registry || tool_register(probe_registry, &probe_tool) != 0 ||
        tool_register(probe_registry, tool_file_write()) != 0) {
        tool_registry_free(probe_registry);
        fs_remove(invalidate_path); fs_remove(probe_workspace);
        return 2;
    }
    reasoning_config probe_cfg = {0};
    probe_cfg.llm = &probe_provider;
    probe_cfg.tools = probe_registry;
    probe_cfg.workspace = probe_workspace;
    probe_cfg.max_rounds = -1;
    reasoning *probe_engine = reasoning_new(&probe_cfg);
    if (!probe_engine) {
        tool_registry_free(probe_registry);
        fs_remove(invalidate_path); fs_remove(probe_workspace);
        return 2;
    }
    char *probe_answer = NULL;
    int probe_rc = reasoning_run(probe_engine, "检查工作区状态并汇报", &probe_answer);
    failures += check(probe_rc == 0 && probe_answer && strstr(probe_answer, "探针复用验证完成"),
                      "probe-reuse run reaches a final answer");
    char probe_label[128];
    snprintf(probe_label, sizeof(probe_label),
             "identical probe is reused until a write invalidates it (execs=%d calls=%d)",
             probe.shell_execs, probe.calls);
    failures += check(probe.shell_execs == 2 && probe.calls >= 5, probe_label);
    free(probe_answer);
    reasoning_free(probe_engine);
    tool_registry_free(probe_registry);
    fs_remove(invalidate_path); fs_remove(probe_workspace);

    /* Two timed-out verification scripts trigger exactly one "stop writing
     * replacement scripts" nudge (#273), and the planner system prompt names
     * the backend shell the runtime actually uses (#274). */
    const char *timeout_workspace = "build/test-plan-timeout";
    fs_mkdirs(timeout_workspace);
    timeout_fixture timeout_fx = {0};
    llm timeout_provider = {
        .vt = &(const llm_vtable){.chat = timeout_chat},
        .provider = "fixture", .model = "fixture", .impl = &timeout_fx
    };
    tool timeout_tool = {
        .name = "shell", .description = "timeout fixture",
        .execute = timeout_shell, .ud = &timeout_fx
    };
    tool_registry *timeout_registry = tool_registry_new();
    if (!timeout_registry || tool_register(timeout_registry, &timeout_tool) != 0) {
        tool_registry_free(timeout_registry);
        fs_remove(timeout_workspace);
        return 2;
    }
    reasoning_config timeout_cfg = {0};
    timeout_cfg.llm = &timeout_provider;
    timeout_cfg.tools = timeout_registry;
    timeout_cfg.workspace = timeout_workspace;
    timeout_cfg.max_rounds = -1;
    reasoning *timeout_engine = reasoning_new(&timeout_cfg);
    if (!timeout_engine) {
        tool_registry_free(timeout_registry);
        fs_remove(timeout_workspace);
        return 2;
    }
    char *timeout_answer = NULL;
    int timeout_rc = reasoning_run(timeout_engine, "运行验证脚本并汇报结论", &timeout_answer);
    failures += check(timeout_fx.nudge_seen == 1,
                      "two timed-out scripts trigger the one-shot replacement-script nudge");
    failures += check(timeout_fx.prompt_ok == 1,
                      "planner system prompt names the runtime shell and evidence parity");
    (void)timeout_rc;
    free(timeout_answer);
    reasoning_free(timeout_engine);
    tool_registry_free(timeout_registry);
    fs_remove(timeout_workspace);
    printf("plan cycle regression: %s\n", failures ? "FAIL" : "PASS");
    return failures ? 1 : 0;
}
