#pragma once

#include "llm/router.h"

int video_seedance_submit(const route *rt, const char *prompt, int seconds,
                          char task_id[128], char **error);
int video_seedance_poll(const route *rt, const char *task_id, char **url, char **error);
