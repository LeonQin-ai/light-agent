#pragma once

#include "llm/router.h"

typedef struct llm_image_result {
    unsigned char *data;
    size_t len;
    const char *mime;
    char *error;
    int provider_status;
} llm_image_result;

/* Generate one image. size may be NULL or WxH. Free the result on all paths. */
int llm_image_generate(const route *rt, const char *prompt, const char *size, llm_image_result *out);
/* Returns a borrowed MIME type when the bytes contain a complete image. */
const char *llm_image_mime(const unsigned char *data, size_t len);
void llm_image_result_free(llm_image_result *out);
