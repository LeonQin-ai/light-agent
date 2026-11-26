#pragma once

#include "cJSON.h"

#define MANJU_REFERENCE_KEY_CAP 17

/* Import a bounded set of named image uploads into an immutable workspace set.
 * Returns 0 on success, 400 for invalid input, or 500 on storage failure.
 * On failure, problem points to a static diagnostic string. */
int manju_references_import(const char *workspace, const cJSON *input,
                            char key[MANJU_REFERENCE_KEY_CAP], const char **problem);
int manju_references_valid_key(const char *key);
cJSON *manju_references_load(const char *workspace, const char *key);

/* Reference names can address either a storyboard character's name or ID. */
int manju_reference_matches(const cJSON *reference, const cJSON *character);
int manju_references_cover(const cJSON *references, const cJSON *character);
/* Validate each label maps to at most one character in this episode. */
int manju_references_resolve(const cJSON *references, const cJSON *board);
