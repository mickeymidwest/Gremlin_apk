cmake_minimum_required(VERSION 3.22.1)
project("gremlin-vision")

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
# mtmd needs common/ for its helpers, so this one stays ON even though
# LLAMA_BUILD_TOOLS is off.
set(LLAMA_BUILD_COMMON   ON  CACHE BOOL "" FORCE)

# GGML_NATIVE must be OFF for a cross-compile: left on, ggml probes the
# *build machine's* CPU (a CI runner) and bakes in instructions the
# phone may not have, which fails at runtime rather than at build time.
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)

add_subdirectory(${LLAMA_ROOT} ${CMAKE_CURRENT_BINARY_DIR}/llama.cpp)

# mtmd is the multimodal layer (image -> embeddings the language model
# can attend over). LLAMA_BUILD_TOOLS=OFF above means tools/ isn't added
# automatically, so pull in just this one target.
#
# MTMD_VIDEO defaults ON and shells out to an ffmpeg binary -- there
# isn't one on a phone, and video isn't wanted here, so it's forced off
# before the subdirectory is added (a CACHE var set afterwards would be
# read too late to matter).
set(MTMD_VIDEO OFF CACHE BOOL "" FORCE)

# tools/mtmd/CMakeLists.txt does:
#     set_target_properties(mtmd PROPERTIES VERSION ${LLAMA_INSTALL_VERSION} ...)
# and llama.cpp's root CMakeLists sets LLAMA_INSTALL_VERSION as a plain
# (non-cache) variable. add_subdirectory() gives that root its own child
# scope, so the value never propagates back up here -- leaving VERSION
# with an empty value, an odd argument count, and a hard configure
# failure: "set_target_properties called with incorrect number of
# arguments". Defining it in this scope is what makes mtmd usable as a
# nested subdirectory at all. The value is only shared-library version
# metadata and is irrelevant to an Android .so.
if(NOT DEFINED LLAMA_INSTALL_VERSION)
    set(LLAMA_INSTALL_VERSION 0.0.0)
endif()

add_subdirectory(${LLAMA_ROOT}/tools/mtmd ${CMAKE_CURRENT_BINARY_DIR}/mtmd)

add_library(gremlin-vision SHARED gremlin_vision.cpp)

target_include_directories(gremlin-vision PRIVATE
    ${LLAMA_ROOT}/include
    ${LLAMA_ROOT}/ggml/include
    ${LLAMA_ROOT}/common
    ${LLAMA_ROOT}/tools/mtmd
)

target_compile_features(gremlin-vision PRIVATE cxx_std_17)

# Deliberately NOT linking llama.cpp's common library. Two reasons, and
# CI caught the first: the target is called `llama-common`, not `common`
# (so `-lcommon` simply doesn't exist), and this JNI file uses no
# common_* helper at all -- only llama.h and the mtmd headers. mtmd
# itself links what it needs (ggml, llama, Threads), so there's nothing
# to inherit either. Linking a library we don't use would just be a
# rename waiting to break again.
target_link_libraries(gremlin-vision
    mtmd
    llama
    ggml
    android
    log
)
