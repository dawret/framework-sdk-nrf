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
import semantic_version
import filecmp
from platform import machine
from pathlib import Path
import yaml
import jinja2

from platformio.package import version
from platformio.compat import IS_WINDOWS
from platformio import fs
from platformio.proc import exec_command
import SCons.Builder

Import("env")

platform = env.PioPlatform()
board = env.BoardConfig()

ZEPHYR_ENV_VERSION = "1.0.0"
FRAMEWORK_VERSION = platform.get_package_version("framework-zephyr").split("+")[0]
TOOLCHAIN_VERSION = version.get_original_version(
    platform.get_package_version("toolchain-gccarmnoneeabi").split("+")[0]
)
TOOLCHAIN_ROOT = os.path.join(
    platform.get_package_dir("toolchain-gccarmnoneeabi"),
    "zephyr-sdk-0.%s" % TOOLCHAIN_VERSION,
)

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


def check_command(ret, msg):
    if ret["returncode"] != 0:
        raise RuntimeError(f"{msg}:\nstdout: {ret['out']}\nstderr: {ret['err']}")
    return (ret["out"], ret["err"])


class BuildEnvironment:
    def __init__(
        self, project_dir: Path, source_dir: Path, build_dir: Path, framework_dir: Path
    ):
        self.venv_path = None
        self.env = {}
        self.project_dir = project_dir
        self.source_dir = source_dir
        self.build_dir = build_dir
        self.framework_dir = framework_dir

    def _python(self):
        if not self.venv_path:
            raise RuntimeError("Virtual environment is not created yet")
        if os.name == "nt":
            return self.venv_path / "Scripts" / "python.exe"
        return self.venv_path / "bin" / "python"

    def create_venv(self, path: Path):
        if path.exists():
            return

        ret = exec_command([sys.executable, "-m", "venv", path])
        check_command(ret, f"Failed to create virtual environment at {path}")
        self.venv_path = path

    def install_requirements(self, requirements_file: Path):
        ret = exec_command(
            [self._python(), "-m", "pip", "install", "-r", requirements_file]
        )
        check_command(ret, f"Failed to install dependencies from {requirements_file}")

    def run_python(self, args, cwd: Path | None = None):
        cmd = [self._python()] + args
        ret = exec_command(cmd, cwd=cwd, env=self.env)
        out, err = check_command(ret, f"Failed to run command: {' '.join(cmd)}")
        return (out, err)

    def load_env_from_script(self, script_path: Path):
        ret = exec_command(["/bin/bash", "-c", f"source {script_path} && env"], env={})
        out, _ = check_command(ret, f"Failed to source {script_path}")
        self.set_envs(
            {
                key: value
                for key, _, value in (line.partition("=") for line in out.splitlines())
            }
        )

    def set_envs(self, env_vars: dict):
        for key, value in env_vars.items():
            self.set_env(key, value)

    def set_env(self, key: str, value: str):
        if key in self.env:
            if key == "PATH":
                self.env[key] = os.pathsep.join(
                    value.split(os.pathsep) + self.env[key].split(os.pathsep)
                )
            else:
                raise RuntimeError(f"Environment variable {key} is already set")
        else:
            self.env[key] = value


class ZephyrSdk:
    def __init__(self, build_env, version: str):
        self.workspace_dir = build_env.project_dir
        self.app_dir = self.workspace_dir / "app"
        self.zephyr_dir = self.workspace_dir / "zephyr"
        self.build_env = build_env
        self.version = version
        self.modules = []
        self.need_reconfigure = False
        templates_dir = self.build_env.framework_dir / "templates"
        self.jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_dir),
        )

    def add_modules(self, modules: list):
        self.modules += modules

    def _generate_project_files(self, build_flags, link_flags, source_files):
        context = {
            "build_flags": build_flags,
            "link_flags": link_flags,
            "source_files": source_files,
            "project_name": self.build_env.project_dir.name,
        }

        self.app_dir.mkdir(parents=True, exist_ok=True)

        cmake_txt_tpl = self.jinja_env.get_template("CMakeLists.txt.j2").render(context)
        cmake_txt_path = self.app_dir / "CMakeLists.txt"
        if (not cmake_txt_path.exists()) or (
            cmake_txt_path.read_text() != cmake_txt_tpl
        ):
            cmake_txt_path.write_text(cmake_txt_tpl)
            self.need_reconfigure = True

        if not any(self.build_env.source_dir.iterdir()):
            app_main_tpl = self.jinja_env.get_template("app_main.c.j2").render(context)
            app_main_path = self.build_env.source_dir / "main.c"
            app_main_path.write_text(app_main_tpl)

    def _is_reconfigure_required(self):
        if self.need_reconfigure:
            return True
        west_yml_path = self.app_dir / "west.yml"
        if west_yml_path.stat().st_mtime > self.build_env.build_dir.stat().st_mtime:
            # Reconfigure after west.yml changes
            return True
        cmake_txt_path = self.app_dir / "CMakeLists.txt"
        cmake_cache_path = self.build_env.build_dir / "CMakeCache.txt"
        if cmake_txt_path.stat().st_mtime > cmake_cache_path.stat().st_mtime:
            # Reconfigure after CMakeLists.txt changes
            return True
        return False

    def _generate_west_config(self):
        context = {
            "modules": self.modules,
            "version": self.version,
        }
        west_yml_tpl = self.jinja_env.get_template("west.yml.j2").render(context)
        west_yml_path = self.app_dir / "west.yml"
        if (not west_yml_path.exists()) or (west_yml_path.read_text() != west_yml_tpl):
            west_yml_path.write_text(west_yml_tpl)
            self.need_reconfigure = True

    def install(self):
        # self._generate_project_files(build_flags, link_flags, source_files)
        self._generate_west_config()
        if not (self.build_env.project_dir / ".west").exists():
            self.build_env.run_python(
                [
                    "-m",
                    "west",
                    "init",
                    "-l",
                    self.app_dir.relative_to(self.workspace_dir),
                ],
                cwd=self.workspace_dir,
            )
        self.build_env.run_python(
            ["-m", "west", "update", "--narrow", "--fetch-opt=--depth=1"],
            cwd=self.workspace_dir,
        )
        self.build_env.run_python(
            ["-m", "west", "packages", "pip", "--install"], cwd=self.workspace_dir
        )
        self.build_env.load_env_from_script(self.zephyr_dir / "zephyr-env.sh")
        self.build_env.set_env("ZEPHYR_BASE", str(self.zephyr_dir))

    def build(
        self,
        board,
        build_flags,
        link_flags,
        source_files,
        package_dir,
        config_path,
        sysbuild=False,
        cmake_extra_args=[],
        verbose=False,
    ):
        self._generate_project_files(build_flags, link_flags, source_files)
        menuconfig_file = self.app_dir / "menuconfig.conf"
        west_cmd = [
            "west",
            "build",
            "--sysbuild" if sysbuild else "",
            "-p" if self._is_reconfigure_required() else "",
            "-b",
            board,
            "-d",
            self.build_env.build_dir,
            self.app_dir,
            "--",
            f"-DPIO_PACKAGES_DIR:PATH={package_dir}",
            f"-DDOTCONFIG={config_path}",
            (
                f"-DOVERLAY_CONFIG:FILEPATH={menuconfig_file}"
                if menuconfig_file.is_file()
                else ""
            ),
        ] + cmake_extra_args
        out, err = self.build_env.run_python(west_cmd, cwd=self.workspace_dir)
        if verbose:
            print(out)
            print(err)

    def merge_mcuboot(self, target):
        script_path = self.zephyr_dir / "scripts" / "build" / "mergehex.py"
        mcuboot_hex = self.build_env.build_dir / "mcuboot" / "zephyr" / "zephyr.hex"
        app_hex = self.build_env.build_dir / "app" / "zephyr" / "zephyr.hex"
        if not mcuboot_hex.is_file():
            raise RuntimeError(f"Cannot find MCUboot hex file at {mcuboot_hex}")
        if not app_hex.is_file():
            raise RuntimeError(f"Cannot find application hex file at {app_hex}")
        self.build_env.run_python(
            [script_path, "-o", target, mcuboot_hex, app_hex],
            cwd=self.build_env.build_dir,
        )

    def copy_app_elf(self, target):
        app_elf = self.build_env.build_dir / "app" / "zephyr" / "zephyr.elf"
        if not app_elf.is_file():
            raise RuntimeError(f"Cannot find application ELF file at {app_elf}")
        shutil.copy2(app_elf, target)


def run_west_build():
    print("Reading CMake configuration")

    CONFIG_PATH = board.get(
        "build.zephyr.config_path",
        os.path.join(PROJECT_DIR, "config.%s" % env.subst("$PIOENV")),
    )

    python_executable = env.get("PYTHONEXE")
    west_cmd = [
        python_executable,
        "-m",
        "west",
        "build",
        "--sysbuild",
        "-b",
        get_zephyr_target(board),
        "-d",
        BUILD_DIR,
        os.path.join(PROJECT_DIR, "app"),
        "--",
        "-DPIO_PACKAGES_DIR:PATH=%s" % env.subst("$PROJECT_PACKAGES_DIR"),
        "-DDOTCONFIG=" + CONFIG_PATH,
    ]

    menuconfig_file = os.path.join(PROJECT_DIR, "app", "menuconfig.conf")
    if os.path.isfile(menuconfig_file):
        print("Adding -DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)
        west_cmd.append("-DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)

    if board.get("build.zephyr.cmake_extra_args", ""):
        west_cmd.extend(
            click.parser.split_arg_string(board.get("build.zephyr.cmake_extra_args"))
        )

    # Run Zephyr in an isolated environment with specific env vars
    zephyr_env = populate_zephyr_env_vars({})

    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        print(west_cmd)

    result = exec_command(west_cmd, env=zephyr_env, cwd=PROJECT_DIR)
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

find_package(Zephyr REQUIRED HINTS $ENV{{ZEPHYR_BASE}})

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

    cmake_tmp_file = os.path.join(PROJECT_DIR, "app", "CMakeLists.tmp")
    cmake_txt_file = os.path.join(PROJECT_DIR, "app", "CMakeLists.txt")
    if not os.path.isdir(os.path.dirname(cmake_tmp_file)):
        os.makedirs(os.path.dirname(cmake_tmp_file))
    with open(cmake_tmp_file, "w") as fp:
        fp.write(cmake_tpl)
    if not os.path.isfile(cmake_txt_file) or not filecmp.cmp(
        cmake_tmp_file, cmake_txt_file
    ):
        shutil.move(cmake_tmp_file, cmake_txt_file)
    else:
        os.remove(cmake_tmp_file)

    if not os.listdir(os.path.join(PROJECT_SRC_DIR)):
        # create an empty file to make CMake happy during first init
        with open(os.path.join(PROJECT_SRC_DIR, "main.c"), "w") as fp:
            fp.write(app_tpl)


def get_zephyr_target(board_config):
    return board_config.get("build.zephyr.variant", env.subst("$BOARD").lower())


def correct_escape_sequences(file_path):
    with open(file_path, "r") as file:
        content = file.read()
    corrected_content = content.replace(
        "re.split('\\s+', line)", "re.split('\\\\s+', line)"
    )
    with open(file_path, "w") as file:
        file.write(corrected_content)


def obj(target, source, env):
    DefaultEnvironment().Append(PIOBUILDFILES_FINAL=[source[0].abspath])
    return None


def lib(target, source, env):
    DefaultEnvironment().Append(PIOBUILDLIBS_FINAL=[source[0].abspath])
    return None


def nop(target, source, env):
    return None


def c_flags_from_env(env):
    return [x for x in env.get("BUILD_FLAGS", [])]


def link_flags_from_env(env):
    return [x for x in env.get("BUILD_FLAGS", []) if x.startswith("-Wl,")]


def dontGenerateProgram(zephyr, target, source, env):
    import click

    files = env.get("PIOBUILDFILES_FINAL")
    if env.get("PIOBUILDLIBS_FINAL"):
        files.extend(env.get("PIOBUILDLIBS_FINAL"))
    files.sort()
    config_path = board.get(
        "build.zephyr.config_path",
        zephyr.build_env.project_dir / f"config.{env.subst("$PIOENV")}",
    )
    cmake_extra_args = click.parser.split_arg_string(
        board.get("build.zephyr.cmake_extra_args", "")
    )

    zephyr.build(
        board=get_zephyr_target(board),
        build_flags=c_flags_from_env(env),
        link_flags=link_flags_from_env(env),
        source_files=files,
        package_dir=env.subst("$PROJECT_PACKAGES_DIR"),
        config_path=config_path,
        sysbuild=True,
        cmake_extra_args=cmake_extra_args,
        verbose=int(ARGUMENTS.get("PIOVERBOSE", 0)) > 0,
    )
    zephyr.merge_mcuboot(zephyr.build_env.build_dir / "merged.hex")
    zephyr.copy_app_elf(zephyr.build_env.build_dir / "firmware.elf")
    return None


def flash_pyocd(zephyr, *args, **kwargs):
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


def setup_builder(zephyr, env):
    FIRMWARE_ELF = Path(BUILD_DIR) / "firmware.elf"
    if FIRMWARE_ELF.is_file():
        FIRMWARE_ELF.unlink()

    env["BUILDERS"]["Object"] = SCons.Builder.Builder(action=obj)
    env["CCCOM"] = Action(lib)
    env["ARCOM"] = Action(nop)
    env["RANLIBCOM"] = Action(nop)
    ProgramScanner = SCons.Scanner.Prog.ProgramScanner()
    env["BUILDERS"]["Program"] = SCons.Builder.Builder(
        action=lambda target, source, env: dontGenerateProgram(
            zephyr, target, source, env
        ),
        target_scanner=ProgramScanner,
    )

    env.Replace(
        SIZEPROGREGEXP=r"^(?:text|_TEXT_SECTION_NAME_2|sw_isr_table|devconfig|rodata|\.ARM.exidx)\s+(\d+).*",
        SIZEDATAREGEXP=r"^(?:datas|bss|noinit|initlevel|_k_mutex_area|_k_stack_area)\s+(\d+).*",
        SIZETOOL="arm-zephyr-eabi-size",
        OBJCOPY="arm-zephyr-eabi-objcopy",
    )

    env.AddCustomTarget("flash_pyocd", None, lambda *args, **kwargs: flash_pyocd(zephyr, *args, **kwargs))


def main():
    build_env = BuildEnvironment(
        Path(PROJECT_DIR), Path(PROJECT_SRC_DIR), Path(BUILD_DIR), Path(FRAMEWORK_DIR)
    )
    venv_path = build_env.project_dir / ".venv"
    build_env.create_venv(venv_path)
    build_env.install_requirements(build_env.framework_dir / "requirements.txt")
    build_env.run_python(
        [Path(platform.get_package_dir("toolchain-gccarmnoneeabi")) / "install.py"]
    )
    build_env.set_env("ZEPHYR_SDK_INSTALL_DIR", TOOLCHAIN_ROOT)
    zephyr = ZephyrSdk(build_env, "v4.3.0")
    zephyr.install()
