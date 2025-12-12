# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import subprocess
import sys
import shutil
import json
import semantic_version
import filecmp
from platform import machine

from platformio.package import version
from platformio.compat import IS_WINDOWS
from platformio import fs
from platformio.proc import exec_command
import SCons.Builder

Import("env")

platform = env.PioPlatform()
board = env.BoardConfig()

ZEPHYR_ENV_VERSION = "1.0.0"
FRAMEWORK_VERSION = platform.get_package_version("framework-zephyr").split('+')[0]
TOOLCHAIN_VERSION = version.get_original_version(platform.get_package_version("toolchain-gccarmnoneeabi").split('+')[0])
TOOLCHAIN_ROOT = os.path.join(platform.get_package_dir("toolchain-gccarmnoneeabi"), "zephyr-sdk-0.%s" %TOOLCHAIN_VERSION)

PROJECT_DIR = env.subst("$PROJECT_DIR")
PROJECT_SRC_DIR = env.subst("$PROJECT_SRC_DIR")
BUILD_DIR = env.subst("$BUILD_DIR")
BUILD_FLAGS = env.get("BUILD_FLAGS")
BUILD_TYPE = env.subst("$BUILD_TYPE")
CMAKE_API_DIR = os.path.join(BUILD_DIR, ".cmake", "api", "v1")
CMAKE_API_QUERY_DIR = os.path.join(CMAKE_API_DIR, "query")
CMAKE_API_REPLY_DIR = os.path.join(CMAKE_API_DIR, "reply")

FRAMEWORK_DIR = platform.get_package_dir("framework-zephyr")
assert os.path.isdir(FRAMEWORK_DIR)

LOCAL_BIN = os.path.join(FRAMEWORK_DIR, "bin")

def is_cmake_reconfigure_required():
    cmake_cache_file = os.path.join(BUILD_DIR, "CMakeCache.txt")
    cmake_txt_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt")
    cmake_preconf_dir = os.path.join(BUILD_DIR, "zephyr", "include", "generated")
    cmake_preconf_misc = os.path.join(BUILD_DIR, "zephyr", "misc", "generated")
    zephyr_prj_conf = os.path.join(PROJECT_DIR, "zephyr", "prj.conf")

    for d in (CMAKE_API_REPLY_DIR, cmake_preconf_dir, cmake_preconf_misc):
        if not os.path.isdir(d) or not os.listdir(d):
            return True
    if not os.path.isfile(cmake_cache_file):
        return True
    if not os.path.isfile(os.path.join(BUILD_DIR, "build.ninja")):
        return True
    if os.path.getmtime(cmake_txt_file) > os.path.getmtime(cmake_cache_file):
        return True
    if os.path.isfile(zephyr_prj_conf) and os.path.getmtime(
        zephyr_prj_conf
    ) > os.path.getmtime(cmake_cache_file):
        return True
    if os.path.getmtime(FRAMEWORK_DIR) > os.path.getmtime(cmake_cache_file):
        return True

    return False

def populate_zephyr_env_vars(zephyr_env):
    zephyr_env["Zephyr-sdk_DIR"] = os.path.join(TOOLCHAIN_ROOT, "cmake/")
    zephyr_env["ZEPHYR_BASE"] = os.path.join(FRAMEWORK_DIR, "zephyr")

    additional_packages = [
        platform.get_package_dir("tool-ninja"),
        LOCAL_BIN,
    ]

    zephyr_env["PATH"] = os.pathsep.join(additional_packages)

def run_cmake():
    print("Reading CMake configuration")

    CONFIG_PATH = board.get(
        "build.zephyr.config_path",
        os.path.join(PROJECT_DIR, "config.%s" % env.subst("$PIOENV")),
    )

    python_executable = env.get("PYTHONEXE")
    semver = semantic_version.Version(framework_zephyr_version)
    cmake_cmd = [
        os.path.join(platform.get_package_dir("tool-cmake") or "", "bin", "cmake"),
        "-S",
        os.path.join(PROJECT_DIR, "zephyr"),
        "-B",
        BUILD_DIR,
        "-GNinja",
        "-DBOARD=%s" % get_zephyr_target(board),
        "-DCMAKE_DISABLE_FIND_PACKAGE_ZephyrBuildConfiguration=TRUE",
        "-DCMAKE_DISABLE_FIND_PACKAGE_ZephyrAppConfiguration=TRUE",
        f"-DNCS_VERSION_MAJOR={semver.major}",
        f"-DNCS_VERSION_MINOR={semver.minor}",
        f"-DNCS_VERSION_PATCH={semver.patch}",
        f"-DNCS_VERSION_EXTRA=",
        "-DPYTHON_EXECUTABLE:FILEPATH=%s" % python_executable,
        "-DPython3_EXECUTABLE:FILEPATH=%s" % python_executable,
        "-DPIO_PACKAGES_DIR:PATH=%s" % env.subst("$PROJECT_PACKAGES_DIR"),
        "-DDOTCONFIG=" + CONFIG_PATH,
        # "-DBUILD_VERSION=zephyr-v" + FRAMEWORK_VERSION.split(".")[1],
        "-DWEST_PYTHON=%s" % python_executable,
    ]

    menuconfig_file = os.path.join(PROJECT_DIR, "zephyr", "menuconfig.conf")
    if os.path.isfile(menuconfig_file):
        print("Adding -DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)
        cmake_cmd.append("-DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)

    if board.get("build.zephyr.cmake_extra_args", ""):
        cmake_cmd.extend(
            click.parser.split_arg_string(board.get("build.zephyr.cmake_extra_args"))
        )

    # Run Zephyr in an isolated environment with specific env vars
    zephyr_env = os.environ.copy()
    populate_zephyr_env_vars(zephyr_env)

    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        print(cmake_cmd)

    result = exec_command(cmake_cmd, env=zephyr_env)
    if result["returncode"] != 0:
        sys.stderr.write(result["out"] + "\n")
        sys.stderr.write(result["err"])
        env.Exit(1)

    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        print(result["out"])
        print(result["err"])

def create_default_project_files(source_files):
    build_flags = ""
    if BUILD_FLAGS:
        build_flags = " ".join(BUILD_FLAGS)
    link_flags = ""
    if BUILD_FLAGS:
        link_flags = " ".join([item for item in BUILD_FLAGS if item.startswith('-Wl,')])

    paths = []
    for lb in env.GetLibBuilders():
        if not lb.dependent:
            continue
        lb.env.PrependUnique(CPPPATH=lb.get_include_dirs())
        paths.extend(lb.env["CPPPATH"])
    DefaultEnvironment().Replace(__PIO_LIB_BUILDERS=None)

    if len(paths):
        build_flags += " " + " ".join([f'\\"-I{path}\\"' for path in paths])

    cmake_tpl = f"""
cmake_minimum_required(VERSION 3.20.0)

set(Zephyr_DIR "$ENV{{ZEPHYR_BASE}}/share/zephyr-package/cmake/")

find_package(Zephyr)

project({os.path.basename(PROJECT_DIR)})

SET(CMAKE_CXX_FLAGS  "${{CMAKE_CXX_FLAGS}} {build_flags}")
SET(CMAKE_C_FLAGS  "${{CMAKE_C_FLAGS}} {build_flags}")
zephyr_ld_options({link_flags})

target_sources(app PRIVATE {" ".join(source_files)})
target_include_directories(app PRIVATE ../src)
"""

    app_tpl = """
#include <zephyr.h>

void main(void)
{
}
"""

    cmake_tmp_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.tmp")
    cmake_txt_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt")
    if not os.path.isdir(os.path.dirname(cmake_tmp_file)):
        os.makedirs(os.path.dirname(cmake_tmp_file))
    with open(cmake_tmp_file, "w") as fp:
        fp.write(cmake_tpl)
    if not os.path.isfile(cmake_txt_file) or not filecmp.cmp(cmake_tmp_file, cmake_txt_file):
        shutil.move(cmake_tmp_file, cmake_txt_file)
    else:
        os.remove(cmake_tmp_file)

    if not os.listdir(os.path.join(PROJECT_SRC_DIR)):
        # create an empty file to make CMake happy during first init
        with open(os.path.join(PROJECT_SRC_DIR, "main.c"), "w") as fp:
            fp.write(app_tpl)

def get_cmake_code_model(source_files):
    create_default_project_files(source_files)
    if is_cmake_reconfigure_required():
        # Explicitly clean build folder to avoid cached values
        if os.path.isdir(CMAKE_API_DIR):
            fs.rmtree(BUILD_DIR)
        query_file = os.path.join(CMAKE_API_QUERY_DIR, "codemodel-v2")
        if not os.path.isfile(query_file):
            os.makedirs(os.path.dirname(query_file))
            open(query_file, "a").close()  # create an empty file
        run_cmake()

    if not os.path.isdir(CMAKE_API_REPLY_DIR) or not os.listdir(CMAKE_API_REPLY_DIR):
        sys.stderr.write("Error: Couldn't find CMake API response file\n")
        env.Exit(1)

    codemodel = {}
    for target in os.listdir(CMAKE_API_REPLY_DIR):
        if target.startswith("codemodel-v2"):
            with open(os.path.join(CMAKE_API_REPLY_DIR, target), "r") as fp:
                codemodel = json.load(fp)

    assert codemodel["version"]["major"] == 2
    return codemodel

def get_zephyr_target(board_config):
    return board_config.get("build.zephyr.variant", env.subst("$BOARD").lower())

def correct_escape_sequences(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
    corrected_content = content.replace("re.split('\\s+', line)", "re.split('\\\\s+', line)")
    with open(file_path, 'w') as file:
        file.write(corrected_content)

def install_python_package(package_name, package_source=None, version_spec=None):
    if not package_source:
        package_source = package_name
    try:
        __import__(package_name)
    except ModuleNotFoundError:
        if shutil.which("uv"):
            pip_cmd = "uv pip"
        else:
            pip_cmd = f"$PYTHONEXE -m pip"
        if env.Execute(f"{pip_cmd} -q install --break-system-packages {package_source}{version_spec}"):
            env.Exit(1)

install_python_package("west", version_spec="==1.2.0")
install_python_package("cbor2", version_spec="==5.6.5")


if machine() == 'x86_64':
    install_python_package("pyocd", package_source="git+https://github.com/tomaszduda23/pyOCD", version_spec="@949193f7cbf09081f8e46d6b9d2e4a79e536997e")

framework_zephyr_version = version.get_original_version(FRAMEWORK_VERSION)

if not os.path.isdir(os.path.join(FRAMEWORK_DIR, ".west")):
    if env.Execute(f"$PYTHONEXE -m west init -m https://github.com/nrfconnect/sdk-nrf --mr v{framework_zephyr_version} {FRAMEWORK_DIR}"):
        env.Exit(1)
WEST_UPDATED = os.path.join(FRAMEWORK_DIR, "west_updated")
if not os.path.isfile(WEST_UPDATED):
    python_executable = env.get("PYTHONEXE")
    west_update_cmd = [
        python_executable,
        "-m",
        "west",
        "update",
    ]
    result = exec_command(west_update_cmd, cwd=FRAMEWORK_DIR)
    if result["returncode"] != 0:
        sys.stderr.write(result["out"] + "\n")
        sys.stderr.write(result["err"])
        env.Exit(1)

    correct_escape_sequences(os.path.join(FRAMEWORK_DIR, 'zephyr/scripts/build/uf2conv.py'))

    open(WEST_UPDATED, "x")

toolchain_install_script = os.path.join(platform.get_package_dir("toolchain-gccarmnoneeabi"), "install.py")
if os.path.isfile(toolchain_install_script):
    if env.Execute(f"$PYTHONEXE {toolchain_install_script}"):
        env.Exit(1)

os.makedirs(LOCAL_BIN, exist_ok=True)
#need git in path
GIT_PATH = os.path.join(LOCAL_BIN, "git")
if not os.path.isfile(GIT_PATH):
    os.symlink(shutil.which("git"), GIT_PATH)
#add ccache
CCACHE_PATH = os.path.join(LOCAL_BIN, "ccache")
if not os.path.isfile(CCACHE_PATH):
    CCACHE_PATH_SRC = shutil.which("ccache")
    if CCACHE_PATH_SRC:
        os.symlink(CCACHE_PATH_SRC, CCACHE_PATH)

paths = [
    os.path.join(TOOLCHAIN_ROOT, "arm-zephyr-eabi", "bin"),
    platform.get_package_dir("tool-ninja"),
]
if os.environ.get("PATH"):
    paths.append(os.environ.get("PATH"))
os.environ["PATH"] = os.pathsep.join(paths)
os.environ["ZEPHYR_BASE"] = FRAMEWORK_DIR

FIRMWARE_ELF = os.path.join(BUILD_DIR, "firmware.elf")
# make sure that dontGenerateProgram is called.
# probably there is better way to do so...
if os.path.exists(FIRMWARE_ELF):
    os.remove(FIRMWARE_ELF)

# builder used to override the usual object file construction
def obj(target, source, env):
    DefaultEnvironment().Append(PIOBUILDFILES_FINAL= [source[0].abspath])
    return None

# builder used to override the usual library construction
def lib(target, source, env):
    DefaultEnvironment().Append(PIOBUILDLIBS_FINAL= [source[0].abspath])
    return None

# builder used to override the usual executable binary construction
def dontGenerateProgram(target, source, env):
    files = env.get("PIOBUILDFILES_FINAL")
    if env.get("PIOBUILDLIBS_FINAL"):
        files.extend(env.get("PIOBUILDLIBS_FINAL"))
    files.sort()
    get_cmake_code_model(files)
    build_cmd = [
        "ninja",
        "-C",
        BUILD_DIR,
    ]
    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        build_cmd += ["-v"]
    if env.Execute(" ".join(build_cmd)):
        env.Exit(1)
    shutil.move(os.path.join(BUILD_DIR, "zephyr", "zephyr.elf"), FIRMWARE_ELF)

    return None

def nop(target, source, env):
    return None

env['BUILDERS']['Object'] = SCons.Builder.Builder(action = obj)
env['CCCOM'] = Action(lib)
env['ARCOM'] = Action(nop)
env['RANLIBCOM'] = Action(nop)
ProgramScanner = SCons.Scanner.Prog.ProgramScanner()
env['BUILDERS']['Program'] = SCons.Builder.Builder(action = dontGenerateProgram, target_scanner=ProgramScanner)

env.Replace(
    SIZEPROGREGEXP=r"^(?:text|_TEXT_SECTION_NAME_2|sw_isr_table|devconfig|rodata|\.ARM.exidx)\s+(\d+).*",
    SIZEDATAREGEXP=r"^(?:datas|bss|noinit|initlevel|_k_mutex_area|_k_stack_area)\s+(\d+).*",
    SIZETOOL="arm-zephyr-eabi-size",
    OBJCOPY="arm-zephyr-eabi-objcopy",
)

def flash_pyocd(*args, **kwargs):
    flash_cmd = [
        "$PYTHONEXE",
        "-m",
        "west",
        "flash",
        "-d",
        BUILD_DIR,
        "-r",
        "pyocd",
    ]
    if env.Execute(" ".join(flash_cmd)):
        env.Exit(1)

env.AddCustomTarget("flash_pyocd", None, flash_pyocd)
