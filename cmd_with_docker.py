import argparse
import configparser
import os
import platform
import subprocess
import sys
from pathlib import Path

from conans.cli.command import conan_command, OnceArgument
from conans.client.cache.cache import ClientCache


@conan_command(group="My Company commands")
def with_docker(*args, conan_api, parser):
    """
    Commands for using Conan in Docker as easily as possible
    Current implements a single subcommand "run" but potentially may implement "exec" in future
    Just put your normal conan command after with-docker run and it will be run in a container
    By default, mounts working directory and .conan directory into the container, and removes container after
    Thus all work in conan cache will be done within your host-os cache
    Cross-platform: autodetects docker image OS for setting shell and mount destinations appropriately
    Has many configuration options, CLI -> Environment Variable -> Configuration File (cmd_with_docker.conf)
    Environment variable names are listed in the help alongside the command-line arguments
    """
    conan_api.create_app()
    out = conan_api.out

    docker_config = WithDockerConfig()

    subparsers = parser.add_subparsers(dest='subcommand')
    subparsers.required = True
    parser_run = subparsers.add_parser('run',
                                       help='Run docker container with conan dirs mounted.')
    parser_run.add_argument("--docker-image",
                            metavar=docker_config["docker-image"].env_var_name,
                            action=OnceArgument,
                            help='Comma-separated list of arguments to pass to the docker command')
    parser_run.add_argument("--remove-container",
                            metavar=docker_config["remove-container"].env_var_name,
                            action=OnceArgument,
                            help='Comma-separated list of arguments to pass to the docker command')
    parser_run.add_argument("--docker-args",
                            metavar=docker_config["docker-args"].env_var_name,
                            action=OnceArgument,
                            help='Comma-separated list of arguments to pass to the docker command')
    parser_run.add_argument("--mount-conan-dirs",
                            metavar=docker_config["mount-conan-dirs"].env_var_name,
                            action=OnceArgument,
                            help='Will mount the conan directory(s) into the container')
    parser_run.add_argument("--mount-working-dir",
                            metavar=docker_config["mount-working-dir"].env_var_name,
                            action=OnceArgument,
                            help='Will mount the current working directory into the container')
    parser_run.add_argument("--container-name",
                            metavar=docker_config["container-name"].env_var_name,
                            action=OnceArgument,
                            help='Will mount the current working directory into the container.')
    parser_run.add_argument("--container-user",
                            metavar=docker_config["container-user"].env_var_name,
                            action=OnceArgument,
                            help='Will mount the current working directory into the container')
    parser_run.add_argument("conan_command",
                            nargs=argparse.REMAINDER,
                            help='The conan command to be run inside the docker container.')

    defaults = docker_config.get_reconciled_defaults()
    parser_run.set_defaults(**defaults)
    parsed_args = parser.parse_args(*args)

    if not parsed_args.docker_image:
        raise Exception('"docker-image" must be provided via either: command-line argument, env var, or config file.')
    if not parsed_args.conan_command:
        raise Exception('positional argument "conan_command" is required')

    final_conan_command = "conan " + " ".join(parsed_args.conan_command)
    client_cache = ClientCache(conan_api.app.cache_folder, conan_api.app.out)
    conan_config = conan_api.app.config
    docker_command_factory = DockerCommandFactory(conan_api, conan_config, client_cache, parsed_args)
    final_docker_command = docker_command_factory.run()
    final_command = f'{final_docker_command} "{final_conan_command}"'
    out.info(f"final_command = {final_command}")
    run_command_with_output(final_command)


class ConfigItem(object):
    def __init__(self, name, default, env_var_name, var_type):
        self.name = name
        self.default = default
        self.env_var_name = env_var_name
        self.var_type = var_type
        if var_type == bool:
            self.env_var_value = ConfigItem.truthy_value(os.getenv(env_var_name))
        else:
            self.env_var_value = os.getenv(env_var_name)

    @staticmethod
    def truthy_value(value):
        """
        The only boolean false string in python is the empty string.
        Often, we would like a more strict definition for use when
        parsing strings passed through command line
        that are intended to represent truthy values.
        this function is especially useful with argparse as it can be
        passed to the 'type' field of an argument constructor.
        """
        if type(value) == bool:
            return value
        if type(value) == str:
            truth_strings = ('true', 't', 'yes', 'y', '1')
            return value.lower() in truth_strings


class WithDockerConfig(dict):
    """
    Class declaring and reconciling defaults, env vars, and configuration file values
    """

    items = [
        ConfigItem("docker-image", None, "WITH_DOCKER_IMAGE", str),
        ConfigItem("docker-args", None, "WITH_DOCKER_ARGS", str),
        ConfigItem("remove-container", True, "WITH_DOCKER_RUN_RM", bool),
        ConfigItem("mount-conan-dirs", True, "WITH_DOCKER_MOUNT_CONAN_DIRS", bool),
        ConfigItem("mount-working-dir", True, "WITH_DOCKER_MOUNT_WORKING_DIR", bool),
        ConfigItem("container-name", None, "WITH_DOCKER_CONTAINER_NAME", str),
        ConfigItem("container-user", None, "WITH_DOCKER_CONTAINER_USER", str),
    ]

    def __init__(self):
        # Get the name of the current file, without the cmd_ prefix
        # Use that to find the .conf file
        # which matches the name of the command file without the cmd_ prefix
        super().__init__()
        command_config_filename = Path(__file__).with_suffix(".conf")
        command_name = Path(__file__).stem.replace("cmd_", "")
        defaults_dict = {item.name: str(item.default) for item in self.items if item.default is not None}
        parser = configparser.ConfigParser(defaults_dict)
        parser.read(command_config_filename)
        config = parser[command_name]
        self._parsed_config = dict(config)
        for item in self.items:
            self[item.name] = item

    def get_reconciled_defaults(self):
        defaults = {}
        for item in self.items:
            item_name_arg = item.name.replace("-", "_")
            defaults[item_name_arg] = item.env_var_value or self._parsed_config.get(item.name) or item.default
        return defaults


class DockerCommandFactory(object):
    def __init__(self, conan_api, conan_config, client_cache, parsed_args):
        self._conan_api = conan_api
        self._out = self._conan_api.app.out
        self._conan_config = conan_config
        self._conan_dir = Path(conan_config.storage_path).parent
        self._short_paths_home = conan_config.short_paths_home
        self._client_cache = client_cache
        self._parsed_args = parsed_args
        self._docker_image = self._parsed_args.docker_image
        self._docker_image_os = self.inspect_image_os()
        self._container_user = self.reconcile_container_user()
        self._container_name = self._parsed_args.container_name
        self._mount_working_dir = self._parsed_args.mount_working_dir
        self._mount_conan_dirs = self._parsed_args.mount_conan_dirs

    def run(self):
        command = ["docker run"]
        if self._conan_config:
            command.append("--rm")
        command.append(f"-w {self.container_working_dir}")
        if self._container_name:
            command.append(f"--name {self._parsed_args.container_name}")
        if self._mount_working_dir:
            command.append(f"-v {os.getcwd()}:{self.container_working_dir}")
        if self._mount_conan_dirs:
            command.append(f"-v {self._conan_dir}:{self.container_conan_dir}")
            if platform.system() == "Windows" and self._conan_config.short_paths_home:
                command.append(f"-v {self._short_paths_home}:{self.container_short_path_dir}")
        command.append(self._docker_image)
        command.append(self.container_shell)

        return " ".join(command)

    @property
    def container_working_dir(self):
        if "windows" in self._docker_image_os:
            return f"c:\\Users\\{self._container_user}\\project"
        else:
            return f"/home/{self._container_user}/project"

    @property
    def container_conan_dir(self):
        if "windows" in self._docker_image_os:
            return f"c:\\Users\\{self._container_user}\\.conan"
        else:
            return f"/home/{self._container_user}/.conan"

    @property
    def container_shell(self):
        if "windows" in self._docker_image_os:
            return "cmd /c"
        else:
            return "/bin/bash -c"

    @property
    def container_short_path_dir(self):
        return "C:\\.conan"

    def inspect_image_os(self):
        command = r'docker inspect -f "{{ .Os }}" ' + self._docker_image
        # image_os = self._conan_api.app.runner(command, output=None)
        # Use conan runner if we can figure out how to get stdout back
        image_os = run_command(command).strip()
        return image_os

    def reconcile_container_user(self):
        if self._parsed_args.container_user:
            return self._parsed_args.container_user
        else:
            if "windows" in self._docker_image_os:
                return "ContainerUser"
            else:
                return "conan"


def run_command(command, return_process_object=False, command_options=None):
    cmd_str = " ".join(command) if isinstance(command, list) else command
    command_options = command_options or {
        'check': True,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.STDOUT,
        'universal_newlines': True,
    }

    try:
        called_process = subprocess.run(cmd_str, **command_options)
    except subprocess.CalledProcessError as exc:
        raise exc
    else:
        return called_process if return_process_object else called_process.stdout


def run_command_with_output(command):
    command_options = {
        'check': False,
        'stdout': sys.stdout,
        'stderr': sys.stderr,
        'universal_newlines': True,
    }
    run_command(command, command_options=command_options)
