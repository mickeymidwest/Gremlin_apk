#include "ini.h"

#include <string.h>

/* Copy the run of bytes [p, end) up to the first of the stop chars into
 * dst, trimming leading/trailing spaces.  Returns dst length. */
static size_t take_token(const char *p, const char *end, const char *stops,
                         char *dst)
{
    while (p < end && (*p == ' ' || *p == '\t')) p++;
    const char *q = p;
    while (q < end && !strchr(stops, *q)) q++;
    /* trim trailing space */
    const char *r = q;
    while (r > p && (r[-1] == ' ' || r[-1] == '\t')) r--;

    size_t n = (size_t)(r - p);
    memcpy(dst, p, n);            /* BUG: dst is a fixed 64-byte scratch in the caller */
    dst[n] = '\0';
    return n;
}

int ini_count(const char *text, size_t len)
{
    int n = 0;
    const char *p = text, *end = text + len;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        const char *lend = nl ? nl : end;
        const char *s = p;
        while (s < lend && (*s == ' ' || *s == '\t')) s++;
        if (s < lend && *s != '#' && *s != ';' && *s != '[' &&
            memchr(s, '=', (size_t)(lend - s)))
            n++;
        p = nl ? nl + 1 : end;
    }
    return n;
}

char *ini_get(const char *text, size_t len,
              const char *want,
              char *out, size_t out_cap)
{
    char cur_section[64] = "";
    char key[64];
    char section_dot_key[160];

    const char *p = text, *end = text + len;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        const char *lend = nl ? nl : end;

        const char *s = p;
        while (s < lend && (*s == ' ' || *s == '\t')) s++;

        if (s < lend && *s == '[') {
            /* [section] */
            const char *close = memchr(s, ']', (size_t)(lend - s));
            if (close)
                take_token(s + 1, close, "]", cur_section);
        } else if (s < lend && *s != '#' && *s != ';') {
            const char *eq = memchr(s, '=', (size_t)(lend - s));
            if (eq) {
                take_token(s, eq, "=", key);
                size_t sl = strlen(cur_section);
                memcpy(section_dot_key, cur_section, sl);
                section_dot_key[sl] = '.';
                strcpy(section_dot_key + sl + 1, key);

                if (strcmp(section_dot_key, want) == 0) {
                    char val[256];
                    size_t vn = take_token(eq + 1, lend, "\n", val);
                    if (vn >= out_cap) vn = out_cap - 1;
                    memcpy(out, val, vn);
                    out[vn] = '\0';
                    return out;
                }
            }
        }
        p = nl ? nl + 1 : end;
    }
    return 0;
}
