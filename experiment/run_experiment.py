#!/usr/bin/env python3
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Creates a dispatcher VM in GCP and sends it all the files and configurations
it needs to begin an experiment."""

import argparse
import multiprocessing
import os
import re
import subprocess
import sys
import tarfile
from typing import Dict, List
import yaml

from common import benchmark_utils
from common import experiment_utils
from common import filesystem
from common import fuzzer_utils
from common import gcloud
from common import filestore_utils
from common import logs
from common import new_process
from common import utils
from common import yaml_utils
from experiment import stop_experiment
from src_analysis import experiment_changes

BENCHMARKS_DIR = os.path.join(utils.ROOT_DIR, 'benchmarks')
FUZZERS_DIR = os.path.join(utils.ROOT_DIR, 'fuzzers')
OSS_FUZZ_PROJECTS_DIR = os.path.join(utils.ROOT_DIR, 'third_party', 'oss-fuzz',
                                     'projects')
FUZZER_NAME_REGEX = re.compile(r'^[a-z0-9_]+$')
EXPERIMENT_CONFIG_REGEX = re.compile(r'^[a-z0-9-]{0,30}$')
FILTER_SOURCE_REGEX = re.compile(r'('
                                 r'^\.git/|'
                                 r'^\.pytype/|'
                                 r'^\.venv/|'
                                 r'^.*\.pyc$|'
                                 r'^__pycache__/|'
                                 r'.*~$|'
                                 r'\#*\#$|'
                                 r'\.pytest_cache/|'
                                 r'.*/test_data/|'
                                 r'^third_party/oss-fuzz/build/|'
                                 r'^docker/generated.mk$|'
                                 r'^docs/)')

CONFIG_DIR = 'config'


def read_and_validate_experiment_config(config_filename: str) -> Dict:
    """Reads |config_filename|, validates it, finds as many errors as possible,
    and returns it."""
    config = yaml_utils.read(config_filename)
    filestore_params = {'experiment_filestore', 'report_filestore'}
    cloud_config = {'cloud_compute_zone'}
    string_params = cloud_config.union(filestore_params)
    int_params = {'trials', 'max_total_time'}
    required_params = int_params.union(filestore_params)

    local_experiment = config.get('local_experiment', False)
    if not local_experiment:
        required_params = required_params.union(cloud_config)

    valid = True
    if 'cloud_experiment_bucket' in config or 'cloud_web_bucket' in config:
        logs.error('"cloud_experiment_bucket" and "cloud_web_bucket" are now '
                   '"experiment_filestore" and "report_filestore".')

    for param in required_params:
        if param not in config:
            valid = False
            logs.error('Config does not contain "%s".', param)
            continue

        value = config[param]
        if param in int_params and not isinstance(value, int):
            valid = False
            logs.error('Config parameter "%s" is "%s". It must be an int.',
                       param, value)
            continue

        if param in string_params and (not isinstance(value, str) or
                                       value != value.lower()):
            valid = False
            logs.error(
                'Config parameter "%s" is "%s". It must be a lowercase string.',
                param, str(value))
            continue

        if param not in filestore_params:
            continue

        if local_experiment and not value.startswith('/'):
            valid = False
            logs.error(
                'Config parameter "%s" is "%s". Local experiments only support '
                'using Posix file systems as filestores.', param, value)
            continue

        if not local_experiment and not value.startswith('gs://'):
            valid = False
            logs.error(
                'Config parameter "%s" is "%s". '
                'It must start with gs:// when running on Google Cloud.', param,
                value)

    if not valid:
        raise ValidationError('Config: %s is invalid.' % config_filename)
    return config


class ValidationError(Exception):
    """Error validating user input to this program."""


def get_directories(parent_dir):
    """Returns a list of subdirectories in |parent_dir|."""
    return [
        directory for directory in os.listdir(parent_dir)
        if os.path.isdir(os.path.join(parent_dir, directory))
    ]


def validate_benchmarks(benchmarks: List[str]):
    """Parses and validates list of benchmarks."""
    for benchmark in set(benchmarks):
        if benchmarks.count(benchmark) > 1:
            raise Exception('Benchmark "%s" is included more than once.' %
                            benchmark)


def validate_fuzzer(fuzzer: str):
    """Parses and validates a fuzzer name."""
    if not re.match(FUZZER_NAME_REGEX, fuzzer):
        raise Exception(
            'Fuzzer "%s" may only contain lowercase letters, numbers, '
            'or underscores.' % fuzzer)

    fuzzers_directories = get_directories(FUZZERS_DIR)
    if fuzzer not in fuzzers_directories:
        raise Exception('Fuzzer "%s" does not exist.' % fuzzer)


def validate_fuzzer_config(fuzzer_config):
    """Validate |fuzzer_config|."""
    allowed_fields = ['name', 'env', 'fuzzer']
    if 'fuzzer' not in fuzzer_config:
        raise Exception('Fuzzer configuration must include the "fuzzer" field.')

    for key in fuzzer_config:
        if key not in allowed_fields:
            raise Exception('Invalid entry "%s" in fuzzer configuration.' % key)

    if ('env' in fuzzer_config and not isinstance(fuzzer_config['env'], dict)):
        raise Exception('Fuzzer environment "env" must be a dict.')

    name = fuzzer_config.get('name')
    if name:
        if not re.match(FUZZER_NAME_REGEX, name):
            raise Exception(
                'The "name" option may only contain lowercase letters, '
                'numbers, or underscores.')

    fuzzer = fuzzer_config.get('fuzzer')
    if fuzzer:
        validate_fuzzer(fuzzer)


def validate_experiment_name(experiment_name: str):
    """Validate |experiment_name| so that it can be used in creating
    instances."""
    if not re.match(EXPERIMENT_CONFIG_REGEX, experiment_name):
        raise Exception('Experiment name "%s" is invalid. Must match: "%s"' %
                        (experiment_name, EXPERIMENT_CONFIG_REGEX.pattern))


def set_up_experiment_config_file(config):
    """Set up the config file that will actually be used in the
    experiment (not the one given to run_experiment.py)."""
    filesystem.recreate_directory(CONFIG_DIR)
    experiment_config_filename = os.path.join(CONFIG_DIR, 'experiment.yaml')
    with open(experiment_config_filename, 'w') as experiment_config_file:
        yaml.dump(config, experiment_config_file, default_flow_style=False)


def check_no_local_changes():
    """Make sure that there are no uncommitted changes."""
    assert not subprocess.check_output(
        ['git', 'diff'],
        cwd=utils.ROOT_DIR), 'Local uncommitted changes found, exiting.'


def get_git_hash():
    """Return the git hash for the last commit in the local repo."""
    output = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                     cwd=utils.ROOT_DIR)
    return output.strip().decode('utf-8')


def get_full_fuzzer_name(fuzzer_config):
    """Get the full fuzzer name in the form <base fuzzer>_<variant name>."""
    if 'name' not in fuzzer_config:
        return fuzzer_config['fuzzer']
    return fuzzer_config['fuzzer'] + '_' + fuzzer_config['name']


def set_up_fuzzer_config_files(fuzzer_configs):
    """Write configurations specified by |fuzzer_configs| to yaml files that
    will be used to store configurations."""
    if not fuzzer_configs:
        raise Exception('Need to provide either a list of fuzzers or '
                        'a list of fuzzer configs.')
    fuzzer_config_dir = os.path.join(CONFIG_DIR, 'fuzzer-configs')
    filesystem.recreate_directory(fuzzer_config_dir)
    for fuzzer_config in fuzzer_configs:
        # Validate the fuzzer yaml attributes e.g. fuzzer, env, etc.
        validate_fuzzer_config(fuzzer_config)
        config_file_name = os.path.join(fuzzer_config_dir,
                                        get_full_fuzzer_name(fuzzer_config))
        yaml_utils.write(config_file_name, fuzzer_config)


def start_experiment(experiment_name: str, config_filename: str,
                     benchmarks: List[str], fuzzer_configs: List[dict]):
    """Start a fuzzer benchmarking experiment."""
    check_no_local_changes()

    validate_experiment_name(experiment_name)
    validate_benchmarks(benchmarks)

    config = read_and_validate_experiment_config(config_filename)
    config['benchmarks'] = ','.join(benchmarks)
    config['experiment'] = experiment_name
    config['git_hash'] = get_git_hash()

    set_up_experiment_config_file(config)
    set_up_fuzzer_config_files(fuzzer_configs)

    # Make sure we can connect to database.
    local_experiment = config.get('local_experiment', False)
    if not local_experiment:
        if 'POSTGRES_PASSWORD' not in os.environ:
            raise Exception('Must set POSTGRES_PASSWORD environment variable.')
        gcloud.set_default_project(config['cloud_project'])

    start_dispatcher(config, CONFIG_DIR)


def start_dispatcher(config: Dict, config_dir: str):
    """Start the dispatcher instance and run the dispatcher code on it."""
    dispatcher = get_dispatcher(config)
    # Is dispatcher code being run manually (useful for debugging)?
    manual_experiment = os.getenv('MANUAL_EXPERIMENT')
    if not manual_experiment:
        dispatcher.create_async()
    copy_resources_to_bucket(config_dir, config)
    if not manual_experiment:
        dispatcher.start()


def copy_resources_to_bucket(config_dir: str, config: Dict):
    """Copy resources the dispatcher will need for the experiment to the
    experiment_filestore."""

    def filter_file(tar_info):
        """Filter out unnecessary directories."""
        if FILTER_SOURCE_REGEX.match(tar_info.name):
            return None
        return tar_info

    # Set environment variables to use corresponding filestore_utils.
    os.environ['EXPERIMENT_FILESTORE'] = config['experiment_filestore']
    os.environ['EXPERIMENT'] = config['experiment']
    experiment_filestore_path = experiment_utils.get_experiment_filestore_path()

    base_destination = os.path.join(experiment_filestore_path, 'input')

    # Send the local source repository to the cloud for use by dispatcher.
    # Local changes to any file will propagate.
    source_archive = 'src.tar.gz'
    with tarfile.open(source_archive, 'w:gz') as tar:
        tar.add(utils.ROOT_DIR, arcname='', recursive=True, filter=filter_file)
    filestore_utils.cp(source_archive, base_destination + '/', parallel=True)
    os.remove(source_archive)

    # Send config files.
    destination = os.path.join(base_destination, 'config')
    filestore_utils.rsync(config_dir, destination, parallel=True)


class BaseDispatcher:
    """Class representing the dispatcher."""

    def __init__(self, config: Dict):
        self.config = config
        self.instance_name = experiment_utils.get_dispatcher_instance_name(
            config['experiment'])
        self.process = None

    def create_async(self):
        """Creates the dispatcher asynchronously."""
        raise NotImplementedError

    def start(self):
        """Start the experiment on the dispatcher."""
        raise NotImplementedError


class LocalDispatcher:
    """Class representing the local dispatcher."""

    def __init__(self, config: Dict):
        self.config = config
        self.instance_name = experiment_utils.get_dispatcher_instance_name(
            config['experiment'])
        self.process = None

    def create_async(self):
        """Noop in local experiments."""

    def start(self):
        """Start the experiment on the dispatcher."""
        experiment_filestore_path = os.path.abspath(
            self.config['experiment_filestore'])
        filesystem.create_directory(experiment_filestore_path)
        sql_database_arg = 'SQL_DATABASE_URL=sqlite:///{}'.format(
            os.path.join(experiment_filestore_path, 'local.db'))

        base_docker_tag = experiment_utils.get_base_docker_tag(
            self.config['cloud_project'])
        set_instance_name_arg = 'INSTANCE_NAME={instance_name}'.format(
            instance_name=self.instance_name)
        set_experiment_arg = 'EXPERIMENT={experiment}'.format(
            experiment=self.config['experiment'])
        set_cloud_project_arg = 'CLOUD_PROJECT={cloud_project}'.format(
            cloud_project=self.config['cloud_project'])
        shared_experiment_filestore_arg = '{0}:{0}'.format(
            self.config['experiment_filestore'])
        set_experiment_filestore_arg = (
            'EXPERIMENT_FILESTORE={experiment_filestore}'.format(
                experiment_filestore=self.config['experiment_filestore']))
        shared_report_filestore_arg = '{0}:{0}'.format(
            self.config['report_filestore'])
        set_report_filestore_arg = (
            'REPORT_FILESTORE={report_filestore}'.format(
                report_filestore=self.config['report_filestore']))
        docker_image_url = '{base_docker_tag}/dispatcher-image'.format(
            base_docker_tag=base_docker_tag)
        command = [
            'docker',
            'run',
            '-ti',
            '--rm',
            '-v',
            '/var/run/docker.sock:/var/run/docker.sock',
            '-v',
            shared_experiment_filestore_arg,
            '-v',
            shared_report_filestore_arg,
            '-e',
            set_instance_name_arg,
            '-e',
            set_experiment_arg,
            '-e',
            set_cloud_project_arg,
            '-e',
            sql_database_arg,
            '-e',
            set_experiment_filestore_arg,
            '-e',
            set_report_filestore_arg,
            '-e',
            'LOCAL_EXPERIMENT=True',
            '--cap-add=SYS_PTRACE',
            '--cap-add=SYS_NICE',
            '--name=dispatcher-container',
            docker_image_url,
            '/bin/bash',
            '-c',
            'rsync -r '
            '"${EXPERIMENT_FILESTORE}/${EXPERIMENT}/input/" ${WORK} && '
            'mkdir ${WORK}/src && '
            'tar -xvzf ${WORK}/src.tar.gz -C ${WORK}/src && '
            'source "${WORK}/.venv/bin/activate" && '
            'pip3 install -r "${WORK}/src/requirements.txt" && '
            'PYTHONPATH=${WORK}/src python3 '
            '${WORK}/src/experiment/dispatcher.py || '
            '/bin/bash'  # Open shell if experiment fails.
        ]
        return new_process.execute(command, write_to_stdout=True)


class GoogleCloudDispatcher(BaseDispatcher):
    """Class representing the dispatcher instance on Google Cloud."""

    def create_async(self):
        """Creates the instance asynchronously."""
        self.process = multiprocessing.Process(
            target=gcloud.create_instance,
            args=(self.instance_name, gcloud.InstanceType.DISPATCHER,
                  self.config))
        self.process.start()

    def start(self):
        """Start the experiment on the dispatcher."""
        # TODO(metzman): Replace this workflow with a startup script so we don't
        # need to SSH into the dispatcher.
        self.process.join()  # Wait for dispatcher instance.
        # Check that we can SSH into the instance.
        gcloud.robust_begin_gcloud_ssh(self.instance_name,
                                       self.config['cloud_compute_zone'])

        base_docker_tag = experiment_utils.get_base_docker_tag(
            self.config['cloud_project'])
        cloud_sql_instance_connection_name = (
            self.config['cloud_sql_instance_connection_name'])

        command = (
            'echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope && '
            'docker run --rm '
            '-e INSTANCE_NAME="{instance_name}" '
            '-e EXPERIMENT="{experiment}" '
            '-e CLOUD_PROJECT="{cloud_project}" '
            '-e EXPERIMENT_FILESTORE="{experiment_filestore}" '
            '-e POSTGRES_PASSWORD="{postgres_password}" '
            '-e CLOUD_SQL_INSTANCE_CONNECTION_NAME='
            '"{cloud_sql_instance_connection_name}" '
            '--cap-add=SYS_PTRACE --cap-add=SYS_NICE '
            '-v /var/run/docker.sock:/var/run/docker.sock '
            '--name=dispatcher-container '
            '{base_docker_tag}/dispatcher-image '
            '/work/startup-dispatcher.sh'
        ).format(
            instance_name=self.instance_name,
            postgres_password=os.environ['POSTGRES_PASSWORD'],
            experiment=self.config['experiment'],
            # TODO(metzman): Create a function that sets env vars based on
            # the contents of a dictionary, and use it instead of hardcoding
            # the configs we use.
            cloud_project=self.config['cloud_project'],
            experiment_filestore=self.config['experiment_filestore'],
            cloud_sql_instance_connection_name=(
                cloud_sql_instance_connection_name),
            base_docker_tag=base_docker_tag,
        )
        return gcloud.ssh(self.instance_name,
                          command=command,
                          zone=self.config['cloud_compute_zone'])


def get_dispatcher(config: Dict) -> BaseDispatcher:
    """Return a dispatcher object created from the right class (i.e. dispatcher
    factory)."""
    if config.get('local_experiment'):
        return LocalDispatcher(config)
    return GoogleCloudDispatcher(config)


def main():
    """Run an experiment in the cloud."""
    logs.initialize()

    parser = argparse.ArgumentParser(
        description='Begin an experiment that evaluates fuzzers on one or '
        'more benchmarks.')

    all_benchmarks = benchmark_utils.get_all_benchmarks()
    all_fuzzers = fuzzer_utils.get_fuzzer_names()

    parser.add_argument('-b',
                        '--benchmarks',
                        help='Benchmark names. All of them by default.',
                        nargs='+',
                        required=False,
                        default=all_benchmarks,
                        choices=all_benchmarks)
    parser.add_argument('-c',
                        '--experiment-config',
                        help='Path to the experiment configuration yaml file.',
                        required=True)
    parser.add_argument('-e',
                        '--experiment-name',
                        help='Experiment name.',
                        required=True)
    fuzzers_group = parser.add_mutually_exclusive_group()
    fuzzers_group.add_argument('-f',
                               '--fuzzers',
                               help='Fuzzers to use.',
                               nargs='+',
                               required=False,
                               default=None,
                               choices=all_fuzzers)
    fuzzers_group.add_argument('-fc',
                               '--fuzzer-configs',
                               help='Fuzzer configurations to use.',
                               nargs='+',
                               required=False,
                               default=[])
    fuzzers_group.add_argument('-cf',
                               '--changed-fuzzers',
                               help=('Use fuzzers that have changed since the '
                                     'last experiment. The last experiment is '
                                     'determined by the database your '
                                     'experiment uses, not necessarily the '
                                     'fuzzbench service'),
                               action='store_true',
                               required=False)

    args = parser.parse_args()

    if args.fuzzer_configs:
        fuzzer_configs = [
            yaml_utils.read(fuzzer_config)
            for fuzzer_config in args.fuzzer_configs
        ]
    else:
        if args.changed_fuzzers:
            fuzzers = experiment_changes.get_fuzzers_changed_since_last()
            if not fuzzers:
                logs.error('No fuzzers changed since last experiment. Exiting.')
                return 1
        else:
            fuzzers = args.fuzzers
        fuzzer_configs = fuzzer_utils.get_fuzzer_configs(fuzzers)

    start_experiment(args.experiment_name, args.experiment_config,
                     args.benchmarks, fuzzer_configs)
    if not os.getenv('MANUAL_EXPERIMENT'):
        stop_experiment.stop_experiment(args.experiment_name,
                                        args.experiment_config)
    return 0


if __name__ == '__main__':
    sys.exit(main())
