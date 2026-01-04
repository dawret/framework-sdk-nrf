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

import sys
import shutil
import filecmp
from pathlib import Path

from platformio.proc import exec_command
from platformio.package import version
import SCons.Builder

Import("env")


class BuildEnvironment:
    def __init__(self, project_dir: Path, source_dir: Path, build_dir: Path, sdk):
        self.project_dir = project_dir
        self.source_dir = source_dir
        self.build_dir = build_dir
        self.app_dir = project_dir / "app"
        self.sdk = sdk
        self.reconfigure_required = False

    def run(self, cmd: list[str], cwd=None):
        if not cwd:
            cwd = self.sdk.sdk_path
        ret = exec_command(cmd, env=self.sdk.env, cwd=cwd)
        if ret["returncode"] != 0:
            raise RuntimeError(
                f"Command {' '.join(cmd)} failed:\n{ret['out']}\n{ret['err']}"
            )
        return (ret["out"], ret["err"])

    def _is_reconfigure_required(self):
        if self.sdk.fresh_install or self.reconfigure_required:
            return True
        cmake_cache_file = self.build_dir / "CMakeCache.txt"
        if not cmake_cache_file.is_file():
            return True
        build_ninja_file = self.build_dir / "build.ninja"
        if not build_ninja_file.is_file():
            return True
        pm_static_file = self.project_dir / "app" / "pm_static.yml"
        if (
            pm_static_file.is_file()
            and pm_static_file.stat().st_mtime > cmake_cache_file.stat().st_mtime
        ):
            # Reconfigure if pm_static.yml has changed
            return True
        return False

    def _generate_project_files(
        self, build_flags: list[str], link_flags: list[str], source_files: list[str]
    ):
        self.app_dir.mkdir(parents=True, exist_ok=True)
        cmake_file = self.app_dir / "CMakeLists.txt"
        cmake_tpl = f"""
cmake_minimum_required(VERSION 3.20.0)

set(Zephyr_DIR "$ENV{{ZEPHYR_BASE}}/share/zephyr-package/cmake/")

find_package(Zephyr)

project({self.project_dir.name})

SET(CMAKE_CXX_FLAGS  "${{CMAKE_CXX_FLAGS}} {' '.join(build_flags)}")
SET(CMAKE_C_FLAGS  "${{CMAKE_C_FLAGS}} {' '.join(build_flags)}")
zephyr_ld_options({' '.join(link_flags)})

target_sources(app PRIVATE {" ".join(source_files)})
target_include_directories(app PRIVATE ../src)
"""

        app_tpl = """
#include <zephyr.h>

void main(void)
{
}
"""
        if not cmake_file.is_file() or cmake_file.read_text() != cmake_tpl:
            cmake_file.write_text(cmake_tpl)
            self.reconfigure_required = True
        if not any(self.source_dir.iterdir()):
            main_c_file = self.source_dir / "main.c"
            main_c_file.parent.mkdir(parents=True, exist_ok=True)
            main_c_file.write_text(app_tpl)
            self.reconfigure_required = True

    def _set_extra_cmake_args(self, cmake_extra_args: list[str]):
        try:
            old_args, _ = self.run(["west", "config", "build.cmake-args"])
            old_args = old_args.strip().split()
            if sorted(old_args) == sorted(cmake_extra_args):
                return
        except Exception:
            pass

        print("Setting extra CMake args:", cmake_extra_args)
        self.run(
            [
                "west",
                "config",
                "build.cmake-args",
                "--",
                " ".join(cmake_extra_args),
            ]
        )
        self.reconfigure_required = True

    def build(
        self,
        board: str,
        build_flags: list[str],
        link_flags: list[str],
        source_files: list[str],
        package_dir: Path,
        config_path: Path,
        cmake_extra_args: list[str] = [],
        sysbuild: bool = True,
        pristine: bool = False,
        verbose: bool = False,
    ):
        self._generate_project_files(build_flags, link_flags, source_files)

        menuconfig_file = self.app_dir / "menuconfig.conf"
        if menuconfig_file.is_file():
            cmake_extra_args = [
                f"-DOVERLAY_CONFIG:FILEPATH={menuconfig_file}"
            ] + cmake_extra_args
        cmake_extra_args = [
            f"-DPIO_PACKAGES_DIR:PATH={package_dir}",
            f"-DDOTCONFIG={config_path}",
        ] + cmake_extra_args
        self._set_extra_cmake_args(cmake_extra_args)

        west_cmd = [
            "west",
            "build",
            "--sysbuild" if sysbuild else "--no-sysbuild",
            "--pristine" if self._is_reconfigure_required() else "--pristine=auto",
            "-b",
            board,
            "-d",
            str(self.build_dir),
            str(self.app_dir),
        ]
        print("Building nRF Connect SDK application...")
        if verbose:
            print(" ".join(map(str, west_cmd)))

        out, err = self.run(west_cmd)
        if verbose:
            print(out)
            print(err)


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


def c_flags_from_env(env):
    return [x for x in env.get("BUILD_FLAGS", [])]


def link_flags_from_env(env):
    return [x for x in env.get("BUILD_FLAGS", []) if x.startswith("-Wl,")]


def get_source_files(env):
    files = env.get("PIOBUILDFILES_FINAL")
    if env.get("PIOBUILDLIBS_FINAL"):
        files.extend(env.get("PIOBUILDLIBS_FINAL"))
    files.sort()
    return files


# builder used to override the usual executable binary construction
def dontGenerateProgram(build_env: BuildEnvironment, target, source, env):
    pristine = env.GetProjectOption("pristine", "False")
    sysbuild = env.GetProjectOption("sysbuild", "True")
    board = env.BoardConfig()
    config_path = Path(
        board.get(
            "build.zephyr.config_path",
            build_env.project_dir / f"config.{env.subst("$PIOENV")}",
        )
    )

    try:
        build_env.build(
            board=get_zephyr_target(board, env),
            build_flags=c_flags_from_env(env),
            link_flags=link_flags_from_env(env),
            source_files=get_source_files(env),
            package_dir=Path(env.subst("$PROJECT_PACKAGES_DIR")),
            config_path=config_path,
            cmake_extra_args=get_cmake_extra_args(board, env),
            sysbuild=sysbuild == "True",
            pristine=pristine == "True",
            verbose=int(ARGUMENTS.get("PIOVERBOSE", 0)) > 0,
        )
    except Exception as e:
        print(e, file=sys.stderr)
        env.Exit(1)
    shutil.copy2(
        build_env.build_dir / "app" / "zephyr" / "zephyr.elf",
        build_env.project_dir / str(target[0]),
    )

    return None


def nop(target, source, env):
    return None


def setup_build(build_env):
    firmware_elf = build_env.build_dir / "firmware.elf"
    if firmware_elf.exists():
        firmware_elf.unlink()

    env["BUILDERS"]["Object"] = SCons.Builder.Builder(action=obj)
    env["CCCOM"] = Action(lib)
    env["ARCOM"] = Action(nop)
    env["RANLIBCOM"] = Action(nop)
    env["ENV"] = build_env.sdk.env.copy()
    ProgramScanner = SCons.Scanner.Prog.ProgramScanner()
    env["BUILDERS"]["Program"] = SCons.Builder.Builder(
        action=lambda target, source, env: dontGenerateProgram(
            build_env, target, source, env
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


def flash_dfu(build_env, *args, **kwargs):
    nrfutil = build_env.sdk.nrfutil
    fw_hex = build_env.build_dir / "merged.hex"
    if not fw_hex.is_file():
        raise RuntimeError(f"Firmware file {fw_hex} not found")
    fw_zip = build_env.build_dir / "merged.zip"
    nrfutil.create_dfu_package(fw_hex, fw_zip)
    if not fw_zip.is_file():
        raise RuntimeError(f"DFU package file {fw_zip} not found")
    if "UPLOAD_PORT" not in kwargs["env"]:
        raise RuntimeError("UPLOAD_PORT is not set")
    nrfutil.flash_dfu_package(
        str(kwargs["env"]["UPLOAD_PORT"]),
        str(kwargs["env"].get("UPLOAD_SPEED", "115200")),
        fw_zip,
    )


def flash_pyocd(build_env, *args, **kwargs):
    try:
        build_env.run(["west", "flash", "-d", str(build_env.build_dir), "-r", "pyocd"])
    except Exception as e:
        print(e, file=sys.stderr)
        env.Exit(1)


def install_sdk(install_dir: Path, version: str, platform):
    import nrfutil

    exe = nrfutil.setup(platform, install_dir)
    return exe.install_sdk(version)


try:
    print("Running nrfutil SDK setup...")
    platform = env.PioPlatform()
    sdk_version = version.get_original_version(
        platform.get_package_version("framework-zephyr").split("+")[0]
    )
    nrfutil_root = Path(platform.get_package_dir("tool-nordic-nrfutil"))
    if not nrfutil_root.is_dir():
        raise RuntimeError("nrfutil directory not found")
    sys.path.append(str(nrfutil_root))
    framework_dir = Path(platform.get_package_dir("framework-zephyr"))
    if not framework_dir.is_dir():
        raise RuntimeError("Framework directory not found")
    sdk = install_sdk(framework_dir / "nrfutil_sdk", f"v{sdk_version}", platform)
    build_env = BuildEnvironment(
        project_dir=Path(env.subst("$PROJECT_DIR")),
        source_dir=Path(env.subst("$PROJECT_SRC_DIR")),
        build_dir=Path(env.subst("$BUILD_DIR")),
        sdk=sdk,
    )
    setup_build(build_env)

    env.AddCustomTarget(
        "flash_pyocd",
        None,
        lambda *args, **kwargs: flash_pyocd(build_env, *args, **kwargs),
    )
    env.AddCustomTarget(
        "flash_dfu", None, lambda *args, **kwargs: flash_dfu(build_env, *args, **kwargs)
    )

except Exception as e:
    print(e, file=sys.stderr)
    env.Exit(1)
