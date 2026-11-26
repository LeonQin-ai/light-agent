#pragma once

#include "llm/router.h"
struct strbuf;
struct cJSON;

typedef void (*manju_paint_progress)(int ready, int total, int requests, void *context);

/* Produce and validate the image assets and publish the manifest last.
 * Committed valid images are reused on resume. The caller frees *result. */
int manju_paint_episode(router *routes, const char *route_name,
                        const char *workspace, int episode,
                        int (*abort_check)(void *), manju_paint_progress progress,
                        void *context, char **result);

int manju_character_views(router *routes, const char *route_name, const char *workspace,
                          int episode, const char *reference_set, int (*abort_check)(void *),
                          manju_paint_progress progress, void *context, char **result);

/* Keep the same role descriptions in still-image and motion prompts. */
void manju_append_cast(struct strbuf *prompt, const struct cJSON *board, const struct cJSON *shot);
