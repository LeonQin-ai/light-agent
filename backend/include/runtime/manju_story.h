#pragma once

#include "llm/llm.h"

typedef enum {
    MANJU_STORY_VIDEO_SINGLE,
    MANJU_STORY_VIDEO_MIXED,
    MANJU_STORY_VIDEO_WITH_CHARACTER_VIEWS
} manju_story_video_mode;

/* Read a chapter within the workspace, adapt it with the selected client and
 * publish manju/epNN/shots.json. A valid storyboard with the same input and
 * model is reusable. Returns 0 on success, -1 otherwise; caller frees result. */
int manju_story_episode(llm *client, const char *workspace, const char *chapter_path, int episode,
                        int clip_seconds, manju_story_video_mode video_mode,
                        int (*abort_check)(void *), void *context, int *requests, char **result);
