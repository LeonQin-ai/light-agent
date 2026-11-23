#pragma once

#include "llm/llm.h"

/* Read a chapter within the workspace, adapt it with the selected client and
 * publish manju/epNN/shots.json. A valid storyboard with the same input and
 * model is reusable. Returns 0 on success, -1 otherwise; caller frees result. */
int manju_story_episode(llm *client, const char *workspace, const char *chapter_path, int episode,
                        int clip_seconds,
                        int (*abort_check)(void *), void *context, int *requests, char **result);
