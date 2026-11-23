#pragma once

#include "llm/router.h"

/* 0 accepted; -1 definite rejection, retry is safe; -2 uncertain whether the
 * service accepted the submission, so do not automatically submit again. */
int llm_video_submit(const route *rt, const char *prompt, int seconds,
                     char task_id[128], char **error);
/* 1 ready, 0 pending, -1 definitively failed/canceled, -2 transient query
 * failure, -3 stop this run but retain the task ID. Caller frees URL/error. */
int llm_video_poll(const route *rt, const char *task_id, char **url, char **error);
/* Downloads and atomically publishes a structurally checked MP4. */
int llm_video_download(const route *rt, const char *url, const char *path, char **error);
/* Nonzero when the existing file has a complete MP4 with a video track. */
int llm_video_validate_file(const char *path);
/* Submit one short test, wait a bounded period and verify the returned bytes. */
int llm_video_probe(const route *rt, size_t *bytes, char **error);
