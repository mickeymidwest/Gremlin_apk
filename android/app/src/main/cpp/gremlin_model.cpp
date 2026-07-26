// JNI bridge for the phone's single offline model (a VLM, via
// llama.cpp's mtmd layer).
//
// One model, both jobs. A vision-language model IS a language model, so
// the same weights that describe a screenshot also answer a plain
// question -- there is no reason to ship two. That collapse is why this
// file has exactly ONE generate entry point with the image made
// optional, rather than separate chat and vision paths that would drift
// apart and double the native surface area.
//
// Passing zero bitmaps to mtmd_tokenize yields text-only chunks, which
// mtmd_helper_eval_chunks evaluates through plain llama_decode. So the
// text path is not a second implementation -- it is the same pipeline
// with nothing to encode.
//
// No chat history and no streaming, deliberately: native bugs here are
// process-killing crashes rather than exceptions, so the surface stays
// small.
//
// Threading: every entry point is called from one dedicated Kotlin
// thread (see LocalModel.kt), and mtmd_helper_eval_chunks is explicitly
// documented as NOT thread-safe, so there is no locking here -- the
// single-threaded contract is enforced on the Kotlin side instead.

#include <jni.h>
#include <android/log.h>

#include <string>
#include <vector>

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"

#define LOG_TAG "gremlin-model"
#define LOGi(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGe(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

static llama_model   * g_model   = nullptr;
static llama_context * g_ctx     = nullptr;
static mtmd_context  * g_mtmd    = nullptr;
static bool            g_backend_ready = false;

extern "C" JNIEXPORT void JNICALL
Java_com_gremlin_app_llama_LocalModel_nativeInit(JNIEnv *, jobject) {
    if (!g_backend_ready) {
        llama_backend_init();
        g_backend_ready = true;
    }
}

static void unload_all() {
    if (g_mtmd)  { mtmd_free(g_mtmd);      g_mtmd  = nullptr; }
    if (g_ctx)   { llama_free(g_ctx);      g_ctx   = nullptr; }
    if (g_model) { llama_model_free(g_model); g_model = nullptr; }
}

extern "C" JNIEXPORT jboolean JNICALL
Java_com_gremlin_app_llama_LocalModel_nativeLoad(
        JNIEnv *env, jobject, jstring model_path, jstring mmproj_path, jint n_threads) {
    unload_all();

    const char *model_c  = env->GetStringUTFChars(model_path, nullptr);
    const char *mmproj_c = env->GetStringUTFChars(mmproj_path, nullptr);

    bool ok = false;
    do {
        llama_model_params mparams = llama_model_default_params();
        // CPU only: no phone GPU backend is built here, and claiming GPU
        // layers without one silently falls back anyway.
        mparams.n_gpu_layers = 0;

        g_model = llama_model_load_from_file(model_c, mparams);
        if (!g_model) { LOGe("failed to load model: %s", model_c); break; }

        llama_context_params cparams = llama_context_default_params();
        // 4096 is enough for one image plus a short prompt and answer.
        // Larger contexts cost real RAM on a phone for no benefit here,
        // since this never carries a conversation.
        cparams.n_ctx     = 4096;
        cparams.n_batch   = 512;
        cparams.n_threads = n_threads > 0 ? n_threads : 4;
        cparams.n_threads_batch = cparams.n_threads;

        g_ctx = llama_init_from_model(g_model, cparams);
        if (!g_ctx) { LOGe("failed to create llama context"); break; }

        mtmd_context_params mparams_mtmd = mtmd_context_params_default();
        mparams_mtmd.use_gpu        = false;
        mparams_mtmd.print_timings  = false;
        mparams_mtmd.n_threads      = cparams.n_threads;
        // Cap image tokens: a full-resolution phone screenshot can
        // otherwise expand into more tokens than the context holds, which
        // fails at eval time with a confusing error rather than up front.
        mparams_mtmd.image_max_tokens = 1024;

        g_mtmd = mtmd_init_from_file(mmproj_c, g_model, mparams_mtmd);
        if (!g_mtmd) { LOGe("failed to load mmproj: %s", mmproj_c); break; }

        if (!mtmd_support_vision(g_mtmd)) {
            LOGe("mmproj loaded but reports no vision support");
            break;
        }

        LOGi("vision model ready");
        ok = true;
    } while (false);

    env->ReleaseStringUTFChars(model_path, model_c);
    env->ReleaseStringUTFChars(mmproj_path, mmproj_c);

    if (!ok) unload_all();
    return ok ? JNI_TRUE : JNI_FALSE;
}

extern "C" JNIEXPORT jboolean JNICALL
Java_com_gremlin_app_llama_LocalModel_nativeIsReady(JNIEnv *, jobject) {
    return (g_model && g_ctx && g_mtmd) ? JNI_TRUE : JNI_FALSE;
}

// One entry point for both jobs. `rgb` may be null for a text-only
// turn; when present it must be tightly packed RGB888 of length
// width*height*3. Kotlin does the ARGB_8888 -> RGB conversion (see
// LocalModel.kt) to avoid a second copy of a multi-megabyte screenshot.
extern "C" JNIEXPORT jstring JNICALL
Java_com_gremlin_app_llama_LocalModel_nativeGenerate(
        JNIEnv *env, jobject,
        jbyteArray rgb, jint width, jint height,
        jstring prompt, jint max_tokens) {

    if (!g_model || !g_ctx || !g_mtmd) {
        return env->NewStringUTF("");
    }

    const bool has_image = (rgb != nullptr) && width > 0 && height > 0;

    if (has_image) {
        const jsize expected = (jsize) width * height * 3;
        if (env->GetArrayLength(rgb) != expected) {
            LOGe("bad rgb buffer: got %d, expected %d", env->GetArrayLength(rgb), expected);
            return env->NewStringUTF("");
        }
    }

    jbyte *rgb_data = has_image ? env->GetByteArrayElements(rgb, nullptr) : nullptr;
    const char *prompt_c = env->GetStringUTFChars(prompt, nullptr);

    std::string out;
    mtmd_bitmap       *bitmap = nullptr;
    mtmd_input_chunks *chunks = nullptr;

    do {
        // Build the user turn. The media marker is where the image gets
        // spliced in; without it mtmd has nowhere to put the image and
        // the model answers from text alone. No marker on a text-only
        // turn -- a marker with no bitmap is a tokenize error.
        std::string user_content;
        if (has_image) {
            bitmap = mtmd_bitmap_init((uint32_t) width, (uint32_t) height,
                                      (const unsigned char *) rgb_data);
            if (!bitmap) { LOGe("mtmd_bitmap_init failed"); break; }
            user_content = std::string(mtmd_default_marker()) + "\n" + prompt_c;
        } else {
            user_content = prompt_c;
        }

        // Apply the model's OWN chat template so it gets proper turn
        // markers and reliably stops at end-of-turn. This is the fix for
        // the runaway "Gremlin. Gremlin. Gremlin." loops: fed raw text
        // with no template, a small model never emits its end token and
        // just keeps going. If the model ships no template, fall back to
        // a ChatML wrap (SmolVLM and most small instructs use ChatML).
        std::string full_prompt;
        {
            const char *tmpl = llama_model_chat_template(g_model, nullptr);
            llama_chat_message msg{"user", user_content.c_str()};
            if (tmpl) {
                int need = llama_chat_apply_template(tmpl, &msg, 1, /*add_ass*/ true, nullptr, 0);
                if (need > 0) {
                    std::vector<char> tbuf((size_t) need);
                    int wrote = llama_chat_apply_template(tmpl, &msg, 1, true, tbuf.data(), (int32_t) tbuf.size());
                    if (wrote > 0) full_prompt.assign(tbuf.data(), (size_t) wrote);
                }
            }
            if (full_prompt.empty()) {
                full_prompt = "<|im_start|>user\n" + user_content + "<|im_end|>\n<|im_start|>assistant\n";
            }
        }

        mtmd_input_text text{};
        text.text          = full_prompt.c_str();
        text.text_len      = full_prompt.size();
        text.add_special   = true;
        text.parse_special = true;

        chunks = mtmd_input_chunks_init();
        if (!chunks) { LOGe("mtmd_input_chunks_init failed"); break; }

        // Zero bitmaps is the text-only case: mtmd emits plain text
        // chunks and the helper below evaluates them with llama_decode.
        const mtmd_bitmap *bitmaps[1] = { bitmap };
        if (mtmd_tokenize(g_mtmd, chunks, &text,
                          has_image ? bitmaps : nullptr,
                          has_image ? 1 : 0) != 0) {
            LOGe("mtmd_tokenize failed");
            break;
        }

        llama_pos n_past = 0;
        if (mtmd_helper_eval_chunks(g_mtmd, g_ctx, chunks,
                                    /*n_past*/ 0, /*seq_id*/ 0,
                                    /*n_batch*/ 512, /*logits_last*/ true,
                                    &n_past) != 0) {
            LOGe("mtmd_helper_eval_chunks failed");
            break;
        }

        const llama_vocab *vocab = llama_model_get_vocab(g_model);

        // A repetition penalty is the other half of stopping the loops --
        // even with a chat template, a small model left on pure greedy
        // will happily repeat the same token/line forever. penalty_repeat
        // 1.3 over the last 256 tokens is a firm-but-not-destructive
        // setting. Vision runs cooler (lower temperature) because a
        // description should be literal and stable; chat gets a little
        // more temperature so it isn't robotic. Neither is pure greedy
        // any more.
        const float temp = has_image ? 0.3f : 0.7f;
        llama_sampler *smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
        llama_sampler_chain_add(smpl, llama_sampler_init_penalties(
            /*penalty_last_n*/ 256, /*penalty_repeat*/ 1.3f,
            /*penalty_freq*/ 0.0f, /*penalty_present*/ 0.0f));
        llama_sampler_chain_add(smpl, llama_sampler_init_top_p(0.95f, 1));
        llama_sampler_chain_add(smpl, llama_sampler_init_temp(temp));
        llama_sampler_chain_add(smpl, llama_sampler_init_dist(LLAMA_DEFAULT_SEED));

        llama_batch batch = llama_batch_init(1, 0, 1);
        const int limit = max_tokens > 0 ? max_tokens : 256;

        for (int i = 0; i < limit; i++) {
            llama_token tok = llama_sampler_sample(smpl, g_ctx, -1);
            if (llama_vocab_is_eog(vocab, tok)) break;

            char buf[256];
            const int n = llama_token_to_piece(vocab, tok, buf, sizeof(buf), 0, true);
            if (n > 0) out.append(buf, n);

            batch.n_tokens  = 1;
            batch.token[0]  = tok;
            batch.pos[0]    = n_past;
            batch.n_seq_id[0] = 1;
            batch.seq_id[0][0] = 0;
            batch.logits[0] = true;
            n_past++;

            if (llama_decode(g_ctx, batch) != 0) {
                LOGe("llama_decode failed mid-generation");
                break;
            }
        }

        llama_batch_free(batch);
        llama_sampler_free(smpl);
    } while (false);

    if (chunks) mtmd_input_chunks_free(chunks);
    if (bitmap) mtmd_bitmap_free(bitmap);

    // JNI_ABORT: the buffer was only read, so skip copying it back.
    if (rgb_data) env->ReleaseByteArrayElements(rgb, rgb_data, JNI_ABORT);
    env->ReleaseStringUTFChars(prompt, prompt_c);

    // Clear the KV cache so the next image starts clean rather than
    // attending over the previous one's tokens.
    llama_memory_clear(llama_get_memory(g_ctx), true);

    return env->NewStringUTF(out.c_str());
}

extern "C" JNIEXPORT void JNICALL
Java_com_gremlin_app_llama_LocalModel_nativeUnload(JNIEnv *, jobject) {
    unload_all();
}
