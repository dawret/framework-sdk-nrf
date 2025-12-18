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
import sys
import shutil
import json
import filecmp
from pathlib import Path
from platform import machine

from platformio.package import version
from platformio.compat import IS_WINDOWS
from platformio import fs
from platformio.proc import exec_command
import SCons.Builder

Import("env")

platform = env.PioPlatform()
board = env.BoardConfig()

FRAMEWORK_VERSION = platform.get_package_version("framework-zephyr").split("+")[0]
NRFUTIL_ROOT = Path(platform.get_package_dir("nordic-nrfutil"))

PROJECT_DIR = Path(env.subst("$PROJECT_DIR"))
PROJECT_SRC_DIR = Path(env.subst("$PROJECT_SRC_DIR"))
BUILD_DIR = Path(env.subst("$BUILD_DIR"))
BUILD_FLAGS = env.get("BUILD_FLAGS")
BUILD_TYPE = env.subst("$BUILD_TYPE")

FRAMEWORK_DIR = Path(platform.get_package_dir("framework-zephyr"))
assert FRAMEWORK_DIR.is_dir()


def install_sdk(install_dir):
    import nrfutil

    exe = nrfutil.setup(install_dir)
    return exe.install_sdk("v3.2.0")


def run_west_build(
    sdk,
    project_dir: Path,
    build_dir: Path,
    sdk_dir: Path,
    config_path: Path,
    pio_packages_path: Path,
    board,
    cmake_extra_args=[],
    pristine=False,
    sysbuild=False,
    verbose=False,
):
    print("Running west build")
    app_dir = project_dir / "zephyr"
    west_cmd = ["python", "-m", "west", "build"]
    if sysbuild:
        print("Building in sysbuild mode")
        west_cmd += ["--sysbuild"]
    if pristine:
        print("Pristine build: cleaning build directory")
        west_cmd += ["--pristine"]
    west_cmd += [
        "-d",
        build_dir,
        "-b",
        board,
        app_dir,
        "--",
        f"-DPIO_PACKAGES_DIR:PATH={pio_packages_path}",
        f"-DDOTCONFIG={config_path}",
    ]

    menuconfig_file = app_dir / "menuconfig.conf"
    if menuconfig_file.is_file():
        print(f"Adding -DOVERLAY_CONFIG:FILEPATH={menuconfig_file}")
        west_cmd += [f"-DOVERLAY_CONFIG:FILEPATH={menuconfig_file}"]

    west_cmd += cmake_extra_args

    #if verbose:
    print(" ".join(map(str, west_cmd)))

    result = exec_command(west_cmd, env=sdk.env, cwd=sdk_dir)
    if result["returncode"] != 0:
        raise RuntimeError(f"West build failed:\n{result['out']}\n{result['err']}")

    if verbose:
        print(result["out"])
        print(result["err"])


def create_default_project_files(
    source_files, project_dir: Path, project_source_dir: Path
):
    build_flags = ""
    if BUILD_FLAGS:
        build_flags = " ".join(BUILD_FLAGS)
    link_flags = ""
    if BUILD_FLAGS:
        link_flags = " ".join([item for item in BUILD_FLAGS if item.startswith("-Wl,")])

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

project({project_dir.name})

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

    cmake_tmp_file = project_dir / "zephyr" / "CMakeLists.tmp"
    cmake_txt_file = project_dir / "zephyr" / "CMakeLists.txt"
    cmake_txt_file.parent.mkdir(parents=True, exist_ok=True)
    with cmake_tmp_file.open("w") as fp:
        fp.write(cmake_tpl)
    if not cmake_txt_file.is_file() or not filecmp.cmp(cmake_tmp_file, cmake_txt_file):
        shutil.move(cmake_tmp_file, cmake_txt_file)
    else:
        shutil.rmtree(cmake_tmp_file, ignore_errors=True)

    main_c_file = project_source_dir / "main.c"
    main_c_file.parent.mkdir(parents=True, exist_ok=True)
    if not any(main_c_file.parent.iterdir()):
        # create an empty file to make CMake happy during first init
        with open(main_c_file, "w") as fp:
            fp.write(app_tpl)


def get_zephyr_target(board_config, env):
    return board_config.get("build.zephyr.variant", env.subst("$BOARD").lower())


def get_cmake_extra_args(board_config, env):
    if board_config.get("build.zephyr.cmake_extra_args", ""):
        import click.parser

        return click.parser.split_arg_string(board.get("build.zephyr.cmake_extra_args"))
    return []


# builder used to override the usual object file construction
def obj(target, source, env):
    DefaultEnvironment().Append(PIOBUILDFILES_FINAL=[source[0].abspath])
    return None


# builder used to override the usual library construction
def lib(target, source, env):
    DefaultEnvironment().Append(PIOBUILDLIBS_FINAL=[source[0].abspath])
    return None


# builder used to override the usual executable binary construction
def dontGenerateProgram(
    sdk, project_dir, project_source_dir, build_dir, target, source, env
):
    files = env.get("PIOBUILDFILES_FINAL")
    if env.get("PIOBUILDLIBS_FINAL"):
        files.extend(env.get("PIOBUILDLIBS_FINAL"))
    files.sort()
    try:
        create_default_project_files(files, project_dir, project_source_dir)
        pristine = env.GetProjectOption("pristine", "False")
        sysbuild = env.GetProjectOption("sysbuild", "False")
        verbose = int(ARGUMENTS.get("PIOVERBOSE", 0))
        config_path = Path(
            board.get(
                "build.zephyr.config_path",
                project_dir / f"config.{env.subst('$PIOENV')}",
            )
        )
        pio_packages_path = Path(env.subst("$PROJECT_PACKAGES_DIR"))
        run_west_build(
            sdk,
            project_dir,
            build_dir,
            sdk.sdk_path,
            config_path=config_path,
            pio_packages_path=pio_packages_path,
            board=get_zephyr_target(board, env),
            cmake_extra_args=get_cmake_extra_args(board, env),
            pristine=pristine=="True",
            sysbuild=sysbuild=="True",
            verbose=verbose,
        )
    except Exception as e:
        print(e, file=sys.stderr)
        env.Exit(1)
    shutil.move(
        build_dir / "zephyr" / "zephyr" / "zephyr.elf", project_dir / str(target[0])
    )

    return None


def nop(target, source, env):
    return None


def setup_build(sdk):
    # print("\n".join(list(f"{k}: {v}" for k, v in env.items())))
    firmware_elf = BUILD_DIR / "firmware.elf"
    if firmware_elf.exists():
        firmware_elf.unlink()

    env["BUILDERS"]["Object"] = SCons.Builder.Builder(action=obj)
    env["CCCOM"] = Action(lib)
    env["ARCOM"] = Action(nop)
    env["RANLIBCOM"] = Action(nop)
    env["ENV"] = sdk.env.copy()
    ProgramScanner = SCons.Scanner.Prog.ProgramScanner()
    env["BUILDERS"]["Program"] = SCons.Builder.Builder(
        action=lambda target, source, env: dontGenerateProgram(
            sdk, PROJECT_DIR, PROJECT_SRC_DIR, BUILD_DIR, target, source, env
        ),
        target_scanner=ProgramScanner,
    )
    env.Replace(
        SIZEPROGREGEXP=r"^(?:text|_TEXT_SECTION_NAME_2|sw_isr_table|devconfig|rodata|\.ARM.exidx)\s+(\d+).*",
        SIZEDATAREGEXP=r"^(?:datas|bss|noinit|initlevel|_k_mutex_area|_k_stack_area)\s+(\d+).*",
        SIZETOOL="arm-zephyr-eabi-size",
        OBJCOPY="arm-zephyr-eabi-objcopy",
        GDB="arm-zephyr-eabi-gdb",
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


try:
    print("Running nrfutil SDK setup...")
    sys.path.append(str(NRFUTIL_ROOT))
    sdk = install_sdk(FRAMEWORK_DIR / "nrfutil_sdk")
    setup_build(sdk)
except Exception as e:
    print(e, file=sys.stderr)
    env.Exit(1)
env.AddCustomTarget("flash_pyocd", None, flash_pyocd)
