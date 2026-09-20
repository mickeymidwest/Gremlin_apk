/* A minimal INI-style config reader.  Lines look like:
 *
 *   # a comment
 *   [section]
 *   key = value
 *
 * ini_get() parses the whole buffer and returns the value string for a
 * "section.key" lookup (into a caller buffer), or NULL if not found.
 *
 * Fuzzing-practice target -- there is (at least one) planted memory bug on
 * the parse path.  Write a harness, don't patch the .c.
 */
#ifndef INI_H
#define INI_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Parse `text`/`len` and copy the value for `want` ("section.key") into
 * `out` (capacity `out_cap`).  Returns out on success, NULL if the key
 * isn't present or the input is malformed. */
char *ini_get(const char *text, size_t len,
              const char *want,
              char *out, size_t out_cap);

/* Count the parseable "key = value" entries in the buffer. */
int ini_count(const char *text, size_t len);

#ifdef __cplusplus
}
#endif

#endif
