/* cron.h — scheduled tasks (定时任务): periodically submit a stored prompt to
 * the scheduler as a session-tagged chat task.
 *
 * Two schedule kinds:
 *   interval: every_sec > 0 — run every N seconds
 *   daily:    at = "HH:MM"  — run once per day at that local time
 *
 * Jobs persist to <state_root>/cron.json and survive restarts. A background
 * tick thread (cron_start) wakes every 15s, submits due jobs and schedules
 * the next run. Submissions go through the normal chat path, so results show
 * up in the named session's history. */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include "light-agent.h"

typedef struct cron_mgr cron_mgr;

/* Create the manager (loads <state_root>/cron.json when present). */
cron_mgr *cron_new(scheduler *sched, const char *state_root);
void cron_free(cron_mgr *c);

/* Start the background tick thread. 0 on success. */
int cron_start(cron_mgr *c);

/* Add a job. Exactly one of every_sec (> 0) / at ("HH:MM") selects the
 * schedule kind. Returns the job id, or -1 on invalid arguments. */
long long cron_add(cron_mgr *c, const char *name, const char *prompt,
                   const char *session, int every_sec, const char *at);

/* Remove a job (0 on success, -1 unknown id). */
int cron_remove(cron_mgr *c, long long id);

/* Enable/disable without deleting (0 on success, -1 unknown id). */
int cron_set_enabled(cron_mgr *c, long long id, int enabled);
int cron_rename(cron_mgr *c, long long id, const char *name);

/* Update an existing job's content and/or schedule (issue #266). Sentinel
 * values leave a field unchanged, so prompt-only and schedule-only edits are
 * both supported:
 *   prompt  NULL or ""                      -> keep the current prompt
 *   session NULL or ""                      -> keep the current session
 *   every_sec < 0 and at NULL/""            -> keep the current schedule
 * When a schedule is supplied it must be valid exactly as for cron_add:
 * every_sec > 0 selects an interval (clearing any "HH:MM"), or a non-empty
 * valid "HH:MM" at selects daily (clearing every_sec); anything else is
 * rejected and the job is left untouched. A changed schedule recomputes
 * next_run_ms. Returns 0 on success, -1 on unknown id / bad manager,
 * -2 on invalid arguments (no partial update is applied on failure). */
int cron_update(cron_mgr *c, long long id, const char *prompt,
                const char *session, int every_sec, const char *at);

/* JSON array of jobs (caller frees): [{id,name,prompt,session,every_sec,at,
 * enabled,next_run_ms,last_run_ms}...] */
char *cron_json(const cron_mgr *c);

/* Force a due check + submission right now (used by tests; the tick thread
 * does this on its own schedule). Returns the number of jobs submitted. */
int cron_tick(cron_mgr *c);

#ifdef __cplusplus
}
#endif
