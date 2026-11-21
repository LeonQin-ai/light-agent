#pragma once

#include "llm/router.h"

typedef void (*manju_paint_progress)(int ready, int total, int requests, void *context);

/* Produce and validate the image assets and publish the manifest last.
 * Committed valid images are reused on resume. The caller frees *result. */
int manju_paint_episode(router *routes, const char *route_name,
                        const char *workspace, int episode,
                        int (*abort_check)(void *), manju_paint_progress progress,
                        void *context, char **result);
