cmake_minimum_required(VERSION 3.22.1)
project("gremlin-model")

# llama.cpp lives at android/llama.cpp (git submodule, pinned -- see
# ../../../../.gitmodules). Path is relative to this file:
# app/src/main/cpp -> app/src/main -> app/src -> app -> android
set(LLAMA_ROOT ${CMAKE_CURRENT_LIST_DIR}/../../../../llama.cpp)

if(NOT EXISTS ${LLAMA_ROOT}/CMakeLists.txt)
    message(FATAL_ERROR
        "llama.cpp submodule is empty at ${LLAMA_ROOT}. "
        "Run: git submodule update --init --recursive")
endif()

# Only the libraries are wanted -- building llama.cpp's CLI tools,
# examples, tests and server on an Android cross-compile is a large pile
# of build time for binaries that can never run here anyway.
set(LLAMA_BUILD_TOOLS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER   OFF CACHE BOOL "" FORCE)

# OFF, verified rather than assumed: no source compiled into the mtmd
# target (mtmd.cpp, mtmd-helper.cpp, mtmd-image.cpp, mtmd-audio.cpp,
# clip.cpp, models/*.cpp) includes anything from common/, and this JNI
# file uses no common_* helper either. Leaving it ON built a 4.3MB
# libllama-common.so that Gradle then packaged into the APK even though
# nothing linked it -- Gradle sweeps up every .so in the build tree, so
# an unused library is not free. Also skips vendor/cpp-httplib, which
# llama.cpp only adds under this same flag.
set(LLAMA_BUILD_COMMON   OFF CACHE BOOL "" FORCE)

# The supported way to get libmtmd without the rest of tools/ -- llama.cpp
# added this hook specifically for packaging mtmd for language bindings,
# and it only fires when LLAMA_BUILD_TOOLS is OFF, which is our case.
#
# Using it instead of add_subdirectory(tools/mtmd) directly also removes
# a real trap: tools/mtmd/CMakeLists.txt reads ${LLAMA_INSTALL_VERSION},
# which the root sets as a plain (non-cache) variable. Adding tools/mtmd
# from OUR scope left that empty -- an odd argument count and a hard
# "set_target_properties called with incorrect number of arguments"
# failure. This hook adds it from the root's own scope, where the value
# actually exists, so the problem can't arise.
set(LLAMA_BUILD_MTMD ON CACHE BOOL "" FORCE)

# MTMD_VIDEO defaults ON and shells out to an ffmpeg binary -- there
# isn't one on a phone, and video isn't wanted here. Set before the
# subdirectory is added; a CACHE var set afterwards would be read too
# late to matter.
set(MTMD_VIDEO OFF CACHE BOOL "" FORCE)

# GGML_NATIVE must be OFF for a cross-compile: left on, ggml probes the
# *build machine's* CPU (a CI runner) and bakes in instructions the
# phone may not have, which fails at runtime rather than at build time.
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)

add_subdirectory(${LLAMA_ROOT} ${CMAKE_CURRENT_BINARY_DIR}/llama.cpp)

add_library(gremlin-model SHARED gremlin_model.cpp)

target_include_directories(gremlin-model PRIVATE
    ${LLAMA_ROOT}/include
    ${LLAMA_ROOT}/ggml/include
    ${LLAMA_ROOT}/tools/mtmd
)

target_compile_features(gremlin-model PRIVATE cxx_std_17)

# Deliberately NOT linking llama.cpp's common library. Two reasons, and
# CI caught the first: the target is called `llama-common`, not `common`
# (so `-lcommon` simply doesn't exist), and this JNI file uses no
# common_* helper at all -- only llama.h and the mtmd headers. mtmd
# itself links what it needs (ggml, llama, Threads), so there's nothing
# to inherit either. Linking a library we don't use would just be a
# rename waiting to break again.
target_link_libraries(gremlin-model
    mtmd
    llama
    ggml
    android
    log
)
