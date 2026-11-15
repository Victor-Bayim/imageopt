cmake_minimum_required(VERSION 3.16)
project(sampler_trace_ext CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(ENABLE_SYMRESOLVE "Enable symbol/stack resolving (requires libdw etc.)" OFF)

find_package(PkgConfig QUIET)

# hiredis & libbpf 先走 pkg-config（它们一般都有 .pc）
if (PkgConfig_FOUND)
  pkg_check_modules(HIREDIS hiredis)
  pkg_check_modules(LIBBPF  libbpf)
  pkg_check_modules(REDISPP redis++)
endif()

# ---- redis++: 若没有 .pc，改用手动查找（/usr/local 优先）----
if (NOT REDISPP_FOUND)
  message(STATUS "redis++ pkg-config 未找到，尝试手动查找头文件与库")
  # 头文件通常在 /usr/local/include/sw/redis++
  find_path(REDISPP_INCLUDE_DIR
    NAMES sw/redis++/redis++.h
    HINTS /usr/local/include /usr/include
  )
  # 库通常在 /usr/local/lib
  find_library(REDISPP_LIBRARY
    NAMES redis++ redispp
    HINTS /usr/local/lib /usr/lib
  )
  if (REDISPP_INCLUDE_DIR AND REDISPP_LIBRARY)
    set(REDISPP_FOUND TRUE)
    message(STATUS "redis++ headers: ${REDISPP_INCLUDE_DIR}")
    message(STATUS "redis++ library: ${REDISPP_LIBRARY}")
  else()
    message(FATAL_ERROR "未找到 redis++（libredis++）。请安装或从源码安装到 /usr/local 后重试")
  endif()
endif()

# ---- hiredis: 若没 .pc，同样手动兜底 ----
if (NOT HIREDIS_FOUND)
  message(STATUS "hiredis pkg-config 未找到，尝试手动查找")
  find_path(HIREDIS_INCLUDE_DIR
    NAMES hiredis/hiredis.h
    HINTS /usr/include /usr/local/include
  )
  find_library(HIREDIS_LIBRARY
    NAMES hiredis
    HINTS /usr/lib /usr/local/lib
  )
  if (HIREDIS_INCLUDE_DIR AND HIREDIS_LIBRARY)
    set(HIREDIS_FOUND TRUE)
    message(STATUS "hiredis headers: ${HIREDIS_INCLUDE_DIR}")
    message(STATUS "hiredis library: ${HIREDIS_LIBRARY}")
  else()
    message(FATAL_ERROR "未找到 hiredis。请安装后重试")
  endif()
endif()

# ---- libbpf: 若没 .pc，手动兜底 ----
if (NOT LIBBPF_FOUND)
  message(STATUS "libbpf pkg-config 未找到，尝试手动查找")
  find_path(LIBBPF_INCLUDE_DIR
    NAMES bpf/libbpf.h
    HINTS /usr/include /usr/local/include
  )
  find_library(LIBBPF_LIBRARY
    NAMES bpf libbpf
    HINTS /usr/lib /usr/local/lib
  )
  if (LIBBPF_INCLUDE_DIR AND LIBBPF_LIBRARY)
    set(LIBBPF_FOUND TRUE)
    message(STATUS "libbpf headers: ${LIBBPF_INCLUDE_DIR}")
    message(STATUS "libbpf library: ${LIBBPF_LIBRARY}")
  else()
    message(FATAL_ERROR "未找到 libbpf。请安装后重试")
  endif()
endif()

# prometheus-cpp 用官方 CMake 包（AUR 安装提供 pull/core）
find_package(prometheus-cpp REQUIRED COMPONENTS pull core)
find_package(CURL REQUIRED)
find_package(ZLIB REQUIRED)

add_executable(sampler_trace_ext
  sampler_trace_ext.cpp
  file_access_ebpf_trace.skel.h
)

# 头文件路径
target_include_directories(sampler_trace_ext PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/../common
  ${HIREDIS_INCLUDE_DIRS} ${HIREDIS_INCLUDE_DIR}
  ${REDISPP_INCLUDE_DIRS} ${REDISPP_INCLUDE_DIR}
  ${LIBBPF_INCLUDE_DIRS}  ${LIBBPF_INCLUDE_DIR}
)

# 链接库
target_link_libraries(sampler_trace_ext PRIVATE
  ${HIREDIS_LIBRARIES} ${HIREDIS_LIBRARY}
  ${REDISPP_LIBRARIES} ${REDISPP_LIBRARY}
  ${LIBBPF_LIBRARIES}  ${LIBBPF_LIBRARY}
  prometheus-cpp::pull
  prometheus-cpp::core
  ZLIB::ZLIB
  CURL::libcurl
  elf
  pthread
)

# 运行时 RPATH 指向仓库内可交付的 dist/lib，而不是本机 /usr/local/lib。
# 这样 formal entry 解压后即可优先解析仓库随附库文件，不再隐式依赖本机私装路径。
set(FORMAL_DELIVERY_RPATH "$ORIGIN/../lib;$ORIGIN/../../../dist/lib")

set_target_properties(sampler_trace_ext PROPERTIES
  BUILD_RPATH "${FORMAL_DELIVERY_RPATH}"
  INSTALL_RPATH "${FORMAL_DELIVERY_RPATH}"
  BUILD_WITH_INSTALL_RPATH ON
  INSTALL_RPATH_USE_LINK_PATH OFF
)

# 使用 new dtags 生成 RUNPATH
target_link_options(sampler_trace_ext PRIVATE -Wl,--enable-new-dtags)

# 可选的符号解析（默认 OFF，不去找 libdw/libsframe/libbfd）
if (ENABLE_SYMRESOLVE)
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(ELFUTILS REQUIRED libdw)
  target_link_libraries(sampler_trace_ext PRIVATE ${ELFUTILS_LIBRARIES})
  target_include_directories(sampler_trace_ext PRIVATE ${ELFUTILS_INCLUDE_DIRS})
  target_compile_definitions(sampler_trace_ext PRIVATE ENABLE_SYMRESOLVE=1)
endif()
