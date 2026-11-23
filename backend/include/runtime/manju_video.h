#pragma once

#include "llm/router.h"

typedef void (*manju_video_progress)(int ready, int total, int requests, void *context);

int manju_video_episode(router *routes, const char *route_name, const char *workspace,
                        int episode, int seconds, int (*abort_check)(void *),
                        manju_video_progress progress, void *context, char **result);
