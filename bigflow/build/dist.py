import os
import sys
import shutil
import distutils.cmd
import tempfile
import unittest
import setuptools
import typing
import xmlrunner
import logging
import typing
import pickle
import functools

import distutils.cmd
import distutils.dist

from typing import Optional, Union
from datetime import datetime
from pathlib import Path

import bigflow.cli
import bigflow.build.pip
import bigflow.resources
import bigflow.dagbuilder

from bigflow.commons import (
    run_process,
    remove_docker_image_from_local_registry,
    get_docker_image_id,
    build_docker_image_tag,
)
from bigflow.resources import find_all_resources
from bigflow.version import get_version


__all__ = [
    '_project_setup',
    '_auto_configuration',
    'default_project_setup'
]


logger = logging.getLogger(__name__)


def run_tests(build_dir: Path, test_package: Path):
    runner = xmlrunner.XMLTestRunner(output=str(build_dir / 'junit-reports'))
    suite = unittest.TestLoader().discover(start_dir=str(test_package))
    result = runner.run(suite)
    if result.errors:
        raise ValueError('Test suite failed.')


def export_docker_image_to_file(tag: str, target_dir: Path, version: str):
    image_target_path = target_dir / f'image-{version}.tar'
    print(f'Exporting the image to file: {image_target_path}' )
    run_process(f"docker image save {get_docker_image_id(tag)} -o {image_target_path}")


def build_docker_image(project_dir: Path, tag: str):
    print('Building a Docker image. It might take a while.')
    run_process(f'docker build {project_dir} --tag {tag}')


def build_dags(
        root_package: Path,
        project_dir: Path,
        docker_repository: str,
        start_time: str,
        version: str,
        specific_workflow: typing.Optional[str] = None):
    for workflow in bigflow.cli.walk_workflows(root_package):
        if specific_workflow is not None and specific_workflow != workflow.workflow_id:
            continue
        print(f'Generating DAG file for {workflow.workflow_id}')
        bigflow.dagbuilder.generate_dag_file(
            str(project_dir),
            docker_repository,
            workflow,
            start_time,
            version,
            root_package.name)


def build_image(
        docker_repository: str,
        version: str,
        project_dir: Path,
        image_dir: Path,
        deployment_config: Path):
    os.mkdir(image_dir)
    tag = build_docker_image_tag(docker_repository, version)
    build_docker_image(project_dir, tag)
    try:
        export_docker_image_to_file(tag, image_dir, version)
        shutil.copyfile(deployment_config, image_dir / deployment_config.name)
    finally:
        remove_docker_image_from_local_registry(tag)


def build_command(
    root_package: Path,
    project_dir: Path,
    build_dir: Path,
    test_package: Path,
    dags_dir: Path,
    dist_dir: Path,
    image_dir: Path,
    eggs_dir: Path,
    deployment_config: Path,
    docker_repository: str,
    version: str,
):

    class BuildCommand(distutils.cmd.Command):
        description = 'BigFlow project build.'
        user_options = [
            ('build-dags', None, 'Builds the DAG files.'),
            ('build-package', None, 'Builds the whl package.'),
            ('build-image', None, 'Builds the Docker image.'),
            ('start-time=', None, 'DAGs start time -- given in local timezone, for example: 2020-06-27 15:00:00'),
            ('workflow=', None, 'The workflow that you want to build DAG for.'),
            ('validate-project-setup', None, 'If used, echoes a message that can be used by the CLI to determine if the setup is working.'),
        ]

        def initialize_options(self) -> None:
            self.start_time = datetime.now().strftime("%Y-%m-%d %H:00:00")
            self.build_dags = False
            self.build_package = False
            self.build_image = False
            self.workflow = None
            self.validate_project_setup = False

        def should_run_whole_build(self):
            return not (self.build_dags or self.build_package or self.build_image)

        def finalize_options(self) -> None:
            pass

        def run(self) -> None:
            if self.validate_project_setup:
                print(bigflow.cli.SETUP_VALIDATION_MESSAGE)
                return

            bigflow.cli._valid_datetime(self.start_time)   # FIXME: Don't use private functions.
            if self.build_package or self.should_run_whole_build():
                print('Building the pip package')
                clear_package_leftovers(dist_dir, eggs_dir, build_dir)
                run_tests(build_dir, test_package)
                self.run_command('sdist')

            if self.build_dags or self.should_run_whole_build():
                print('Building the dags')
                clear_dags_leftovers(dags_dir)
                build_dags(
                    root_package,
                    project_dir,
                    docker_repository,
                    self.start_time,
                    version,
                    self.workflow)

            if self.build_image or self.should_run_whole_build():
                print('Building the image')
                clear_image_leftovers(image_dir)
                build_image(
                    docker_repository,
                    version,
                    project_dir,
                    image_dir,
                    deployment_config)

    return BuildCommand


def clear_image_leftovers(image_dir: Path):
    print(f'Removing: {str(image_dir.absolute())}')
    shutil.rmtree(image_dir, ignore_errors=True)


def clear_dags_leftovers(dags_dir: Path):
    print(f'Removing: {str(dags_dir.absolute())}')
    shutil.rmtree(dags_dir, ignore_errors=True)


def clear_package_leftovers(dist_dir: Path, eggs_dir: Path, build_dir: Path):
    for to_delete in [build_dir, dist_dir, eggs_dir]:
        print(f'Removing: {str(to_delete.absolute())}')
        shutil.rmtree(to_delete, ignore_errors=True)

def _validate_deployment_config(config: dict):
    if "docker_repository" in config:
        if not config["docker_repository"].islower():
            raise ValueError("`docker_repository` variable should be in lower case")

def get_docker_repository_from_deployment_config(deployment_config_file: Path) -> str:
    try:
        config = bigflow.cli.import_deployment_config(str(deployment_config_file), 'docker_repository')
    except ValueError:
        raise ValueError(f"Can't find the specified deployment configuration: {deployment_config_file}")

    _validate_deployment_config(config.resolve())
    docker_repository = config.resolve_property('docker_repository', None)

    if docker_repository is None:
        raise ValueError(f"Can't find the 'docker_repository' property in the specified config file: {deployment_config_file}")
    return docker_repository


def secure_get_version() -> str:
    try:
        return get_version()
    except Exception as e:
        print(e)
        raise ValueError("Can't get the current package version. To use the automatic versioning, "
                         "you need to use git inside your project directory.")


def auto_configuration(*args, **kwargs):
    logger.error("Function `bigflow.build.auto_configuration` is deprecated, migrate to `bigflow.build.setup`")
    return _auto_configuration(*args, **kwargs)

def _auto_configuration(project_name: str, project_dir: Path = Path('.').parent) -> dict:
    '''
    Auto configuration for the standard BigFlow project structure (that you can generate through the CLI).
    The 'project_name' parameter should be a valid python package name.

    Example:
    project_setup(**auto_configuration('my_super_project'))
    '''

    deployment_config_file = project_dir / 'deployment_config.py'

    return {
        'project_name': project_name,
        'docker_repository': get_docker_repository_from_deployment_config(deployment_config_file),
        'root_package': project_dir / project_name,
        'project_dir': project_dir,
        'build_dir': project_dir / 'build',
        'test_package': project_dir / 'test',
        'dags_dir': project_dir / '.dags',
        'dist_dir': project_dir / 'dist',
        'image_dir': project_dir / '.image',
        'eggs_dir': project_dir / f'{project_name}.egg-info',
        'deployment_config_file': deployment_config_file,
        'version': secure_get_version(),
        'resources_dir': project_dir / 'resources',
        'project_requirements_file': project_dir / 'resources' / 'requirements.txt'
    }


def project_setup(*args, **kwargs):
    logger.error("Function `bigflow.build.project_setup` is deprecated, migrate to `bigflow.build.setup`")
    return _project_setup(*args, **kwargs)

def _project_setup(
        project_name: str,
        docker_repository: str,
        root_package: Path,
        project_dir: Path,
        build_dir: Path,
        test_package: Path,
        dags_dir: Path,
        dist_dir: Path,
        image_dir: Path,
        eggs_dir: Path,
        deployment_config_file: Path,
        version: str,
        resources_dir: Path,
        project_requirements_file: Path,
        **ignore_unknown,
) -> dict:
    '''
    This function produces arguments for setuptools.setup. The produced setup provides commands that allow you to build
    whl package, docker image and DAGs. Paired with _auto_configuration function, it provides fully automated build
    tool (accessed by CLI) for your project (that you can generate using CLI).

    Example:
    from setuptools import setup
    from bigflow.build import _project_setup, _auto_configuration

    setup(_project_setup(**_auto_configuration('my_super_project')))
    '''

    params_to_check = [
        ('project_name', project_name),
        ('docker_repository', docker_repository),
        ('root_package', root_package),
        ('project_dir', project_dir),
        ('build_dir', build_dir),
        ('test_package', test_package),
        ('dags_dir', dags_dir),
        ('dist_dir', dist_dir),
        ('image_dir', image_dir),
        ('eggs_dir', eggs_dir),
        ('deployment_config_file', deployment_config_file),
        ('version', version),
        ('resources_dir', resources_dir),
        ('project_requirements_file', project_requirements_file),
    ]
    for parameter_name, parameter_value in params_to_check:
        if parameter_value is None:
            raise ValueError(f"Parameter {parameter_name} can't be None.")

    bigflow.build.pip.maybe_recompile_requirements_file(project_requirements_file)

    return {
        'name': project_name,
        'version': version,
        'packages': setuptools.find_packages(exclude=['test']),
        'install_requires': bigflow.resources.read_requirements(project_requirements_file),
        'data_files': [
            ('resources', list(bigflow.resources.find_all_resources(resources_dir))),
        ],
        'cmdclass': {
            'build_project': build_command(
                root_package,
                project_dir,
                build_dir,
                test_package,
                dags_dir,
                dist_dir,
                image_dir,
                eggs_dir,
                deployment_config_file,
                docker_repository,
                version)
        }
    }


def default_project_setup(project_name: str, project_dir: Path = Path('.').parent):
    return setup(name=project_name, project_dir=project_dir)


def _build_setuptools_spec(
    *,
    name: str,
    project_dir: str = ".",  # DEPRECATED
    **kwargs,
) -> dict:

    # TODO: Validate input/unknown parameters.
    project_dir = project_dir or Path(".")

    internal_config = _auto_configuration(name, Path(project_dir))
    params = {}
    for k, v in internal_config.items():
        params[k] = kwargs.pop(k, v)

    # User can overwrite setuptool.setup() args
    # TODO: Provide merge semantics for some keys (data_files, install_requires etc)
    kwargs.pop('project_name', None)
    spec = _project_setup(**{
        **internal_config,
        **kwargs,
        'project_name': name,
        'project_dir': project_dir,
    })
    spec.update((k, v) for k, v in kwargs.items() if k not in internal_config)
    return spec


def setup(**kwargs):
    _maybe_dump_setup_params(kwargs)
    spec = _build_setuptools_spec(**kwargs)
    logger.debug("setuptools.setup(**%r)", spec)
    setuptools.setup(**spec)


def ensure_setuppy(directory: typing.Optional[Path] = None) -> Path:
    directory = directory or Path(".")
    prj_setup_py = directory / "project_setup.py"
    setup_py = directory / "setup.py"

    if prj_setup_py.exists() and setup_py.exists():
        logger.error("Both files `setup.py` and `project_setup.py` exists! File `setup_project.py` renamed to `setup.py`")
        setup_py.rename(directory / "setup.py.back")
        prj_setup_py.rename(setup_py)
    elif prj_setup_py.exists():
        logger.warning("File `project_setup.py` renamed to `setup.py`")
        prj_setup_py.rename(setup_py)
    elif setup_py.exists():
        logger.debug("Found file `setup.py` - do nothing")
    else:
        raise FileNotFoundError("Not found `setup.py` or `project_setup.py`")

    return setup_py


_DUMP_PARAMS = "__bigflow_dump_params"

def read_setup_parameters(path_to_setup: Union[Path, str, None] = None) -> dict:
    """Loads `setup.py`, returns all parameters of `bigflow.build.setup`"""

    if path_to_setup is None:
        path_to_setup = ensure_setuppy()
    logger.info("Read project options from %s", path_to_setup)
    with tempfile.NamedTemporaryFile("r+b") as f:
        run_process(["python", path_to_setup, _DUMP_PARAMS, f.name])
        params = pickle.load(f)

    legacy_project_name = _read_project_name_from_setup_legacy(path_to_setup)
    if legacy_project_name and params.get('name') != legacy_project_name:
        logging.error(
            "Project name mismatch: setup.PROJECT_NAME == %r, "
            "but setup(name=%r). It is recommended to remove 'PROJECT_NAME' variable from 'project_setup.py'",
            legacy_project_name, params.get('name'))

    return params


def _maybe_dump_setup_params(params):
    if len(sys.argv) == 3 and sys.argv[1] == _DUMP_PARAMS:
        with open(sys.argv[2], 'w+b') as out:
            pickle.dump(params, out)
        sys.exit(0)


def _read_project_name_from_setup_legacy(path_to_setup: Path) -> Optional[str]:
    # FIXME: Please, remove me!

    import unittest.mock as mock
    with mock.patch('bigflow.build.dist.setup', lambda **kwargs: None):
        try:
            sys.path.insert(1, os.getcwd())
            import project_setup
            return project_setup.PROJECT_NAME
        except Exception:
            return None
