import json
import logging
import os

from docker import APIClient
from docker.errors import APIError, BuildError, DockerException
from hestia.logging_utils import LogLevels

from rhea.specs import UriSpec

from .exceptions import BuildException

_logger = logging.getLogger('polyaxon.dockerizer')


class DockerBuilder(object):
    LATEST_IMAGE_TAG = 'latest'
    WORKDIR = '/code'

    def __init__(self,
                 build_context,
                 image_name,
                 image_tag,
                 copy_code=True,
                 dockerfile_name='Dockerfile',
                 internal_registry=None,
                 registries=None):
        self.image_name = image_name
        self.image_tag = image_tag
        self.copy_code = copy_code

        self.build_context = build_context
        self.dockerfile_path = os.path.join(self.build_context, dockerfile_name)
        self._validate_registries(internal_registry)
        self.internal_registry = internal_registry
        self._validate_registries(registries)
        self.registries = registries
        self.docker = APIClient(version='auto')
        self.is_pushing = False

    @staticmethod
    def _validate_registries(registries):
        if not registries or isinstance(registries, UriSpec):
            return True

        for registry in registries:
            if not isinstance(registry, UriSpec):
                raise BuildException('A registry `{}` is not valid Urispec.'.format(registry))

        return True

    def get_tagged_image(self):
        return '{}:{}'.format(self.image_name, self.image_tag)

    def check_image(self):
        return self.docker.images(self.get_tagged_image())

    def clean(self):
        pass

    def login_internal_registry(self):
        if not self.internal_registry:
            return
        try:
            self.docker.login(username=self.internal_registry.user,
                              password=self.internal_registry.password,
                              registry=self.internal_registry.host,
                              reauth=True)
        except DockerException as e:
            _logger.exception('Failed to connect to registry %s\n', e)

    def login_private_registries(self):
        if not self.registries:
            return
        for registry in self.registries:
            self.docker.login(username=registry.user,
                              password=registry.password,
                              registry=registry.host,
                              reauth=True)

    def _prepare_log_lines(self, log_line):  # pylint:disable=too-many-branches
        raw = log_line.decode('utf-8').strip()
        raw_lines = raw.split('\n')
        log_lines = []
        status = True
        for raw_line in raw_lines:
            try:
                json_line = json.loads(raw_line)

                if json_line.get('error'):
                    log_lines.append('{}: {}'.format(
                        LogLevels.ERROR, str(json_line.get('error', json_line))))
                    status = False
                else:
                    if json_line.get('stream'):
                        log_lines.append('Building: {}'.format(json_line['stream'].strip()))
                    elif json_line.get('status'):
                        if not self.is_pushing:
                            self.is_pushing = True
                            log_lines.append('Pushing ...')
                    elif json_line.get('aux'):
                        log_lines.append('Pushing finished: {}'.format(json_line.get('aux')))
                    else:
                        log_lines.append(str(json_line))
            except json.JSONDecodeError:
                log_lines.append('JSON decode error: {}'.format(raw_line))
        return log_lines, status

    def _handle_logs(self, log_lines):
        for log_line in log_lines:
            print(log_line)  # pylint:disable=superfluous-parens

    def _handle_log_stream(self, stream):
        log_lines = []
        status = True
        try:
            for log_line in stream:
                new_log_lines, new_status = self._prepare_log_lines(log_line)
                log_lines += new_log_lines
                if not new_status:
                    status = new_status
                self._handle_logs(log_lines)
                log_lines = []
            if log_lines:
                self._handle_logs(log_lines)
        except (BuildError, APIError) as e:
            self._handle_logs('{}: Could not build the image, '
                              'encountered {}'.format(LogLevels.ERROR, e))
            return False

        return status

    def build(self, nocache=False, memory_limit=None):
        limits = {
            # Disable memory swap for building
            'memswap': -1
        }
        if memory_limit:
            limits['memory'] = memory_limit

        stream = self.docker.build(
            path=self.build_context,
            tag=self.get_tagged_image(),
            forcerm=True,
            rm=True,
            pull=True,
            nocache=nocache,
            container_limits=limits)
        return self._handle_log_stream(stream=stream)

    def push(self):
        stream = self.docker.push(self.image_name, tag=self.image_tag, stream=True)
        return self._handle_log_stream(stream=stream)


def _build(build_context,
           image_tag,
           image_name,
           nocache,
           internal_registry=None,
           registries=None):
    """Build necessary code for a job to run"""
    _logger.info('Starting build ...')

    # Build the image
    docker_builder = DockerBuilder(
        build_context=build_context,
        image_name=image_name,
        image_tag=image_tag,
        internal_registry=internal_registry,
        registries=registries,
    )
    docker_builder.login_internal_registry()
    docker_builder.login_private_registries()
    if docker_builder.check_image():
        # Image already built
        docker_builder.clean()
        return docker_builder
    if not docker_builder.build(nocache=nocache):
        docker_builder.clean()
        raise BuildException('The docker image could not be built.')
    return docker_builder


def build(build_context,
          image_tag,
          image_name,
          nocache,
          internal_registry=None,
          registries=None):
    """Build necessary code for a job to run"""
    docker_builder = _build(build_context=build_context,
                            image_tag=image_tag,
                            image_name=image_name,
                            nocache=nocache,
                            internal_registry=internal_registry,
                            registries=registries)
    docker_builder.clean()


def build_and_push(build_context,
                   image_tag,
                   image_name,
                   nocache,
                   internal_registry=None,
                   registries=None):
    """Build necessary code for a job to run and push it."""
    _logger.info('Starting build ...')

    # Build the image
    docker_builder = _build(build_context=build_context,
                            image_tag=image_tag,
                            image_name=image_name,
                            nocache=nocache,
                            internal_registry=internal_registry,
                            registries=registries)
    if not docker_builder.push():
        docker_builder.clean()
        raise BuildException('The docker image could not be pushed.')
    docker_builder.clean()