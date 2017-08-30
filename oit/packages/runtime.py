import os
import click
import tempfile
import shutil
import atexit
import yaml

from common import assert_dir, Dir
from image import ImageMetadata
from model import Model
from multiprocessing import Lock


# Registered atexit to close out debug/record logs
def close_file(f):
    f.close()


def remove_tmp_working_dir(runtime):
    if runtime.remove_tmp_working_dir:
        shutil.rmtree(runtime.working_dir)
    else:
        click.echo("Temporary working directory preserved by operation: %s" % runtime.working_dir)


class Runtime(object):

    # Use any time it is necessary to synchronize feedback from multiple threads.
    mutex = Lock()

    # Serialize access to the debug_log and console
    log_lock = Lock()

    def __init__(self, metadata_dir, working_dir, group, branch, user, verbose):
        self._verbose = verbose
        self.metadata_dir = metadata_dir
        self.working_dir = working_dir

        self.remove_tmp_working_dir = False
        self.group = group
        self.distgits_dir = None
        self.distgit_branch = branch

        self.record_log = None
        self.record_log_path = None

        self.debug_log = None
        self.debug_log_path = None

        # Any lines we want to be in all image metadata yaml files
        self.global_yaml_lines = []

        self.user = user

        # Map of dist-git repo name -> ImageMetadata object. Populated when group is set.
        self.image_map = {}

        # Map of source code repo aliases (e.g. "ose") to a path on the filesystem where it has been cloned.
        # See registry_repo.
        self.source_alias = {}

        # Map of stream alias to image name.
        self.stream_alias_overrides = {}

        self.initialized = False

        # Will be loaded with the streams.yml Model
        self.streams = None

    def initialize(self):

        if self.initialized:
            return

        # We could mark these as required and the click library would do this for us,
        # but this seems to prevent getting help from the various commands (unless you
        # specify the required parameters). This can probably be solved more cleanly, but TODO
        if self.distgit_branch is None:
            click.echo("Branch must be specified")
            exit(1)

        if self.group is None:
            click.echo("Group must be specified")
            exit(1)

        assert_dir(self.metadata_dir, "Invalid metadata-dir directory")

        if self.working_dir is None:
            self.working_dir = tempfile.mkdtemp(".tmp", "oit-")
            # This can be set to False by operations which want the working directory to be left around
            self.remove_tmp_working_dir = True
            atexit.register(remove_tmp_working_dir, self)
        else:
            assert_dir(self.working_dir, "Invalid working directory")

        self.distgits_dir = os.path.join(self.working_dir, "distgits")
        if not os.path.isdir(self.distgits_dir):
            os.mkdir(self.distgits_dir)

        self.debug_log_path = os.path.join(self.working_dir, "debug.log")
        self.debug_log = open(self.debug_log_path, 'a')
        atexit.register(close_file, self.debug_log)

        self.record_log_path = os.path.join(self.working_dir, "record.log")
        self.record_log = open(self.record_log_path, 'a')
        atexit.register(close_file, self.record_log)

        group_dir = os.path.join(self.metadata_dir, "groups", self.group)
        assert_dir(group_dir, "Cannot find group directory")

        self.info("Searching group directory: %s" % group_dir)
        with Dir(group_dir):
            for distgit_repo_name in [x for x in os.listdir(".") if os.path.isdir(x)]:
                self.image_map[distgit_repo_name] = ImageMetadata(
                    self, distgit_repo_name, distgit_repo_name)

        if len(self.image_map) == 0:
            raise IOError("No image metadata directories found within: %s" % group_dir)

        # Read in the streams definite for this group if one exists
        streams_path = os.path.join(group_dir, "streams.yml")
        if os.path.isfile(streams_path):
            with open(streams_path, "r") as s:
                self.streams = Model(yaml.load(s.read()))

    def verbose(self, message):
        with self.log_lock:
            if self._verbose:
                click.echo(message)
            self.debug_log.write(message + "\n")
            self.debug_log.flush()

    def info(self, message, debug=None):
        if self._verbose:
            if debug is not None:
                self.verbose("%s [%s]" % (message, debug))
            else:
                self.verbose(message)
        else:
            with self.log_lock:
                click.echo(message)

    def images(self):
        return self.image_map.values()

    def register_source_alias(self, alias, path):
        self.info("Registering source alias %s: %s" % (alias, path))
        path = os.path.abspath(path)
        assert_dir(path, "Error registering source alias %s" % alias)
        self.source_alias[alias] = path

    def register_stream_alias(self, alias, image):
        self.info("Registering image stream alias override %s: %s" % (alias, image))
        self.stream_alias_overrides[alias] = image

    def add_record(self, record_type, **kwargs):
        """
        Records an action taken by oit that needs to be communicated to outside systems. For example,
        the update a Dockerfile which needs to be reviewed by an owner. Each record is encoded on a single
        line in the record.log. Records cannot contain line feeds -- if you need to communicate multi-line
        data, create a record with a path to a file in the working directory.
        :param record_type: The type of record to create.
        :param kwargs: key/value pairs

        A record line is designed to be easily parsed and formatted as:
        record_type|key1=value1|key2=value2|...|
        """

        # Multiple image build processes could be calling us with action simultaneously, so
        # synchronize output to the file.
        with self.mutex:
            record = "%s|" % record_type
            for k, v in kwargs.iteritems():
                assert("\n" not in str(k))
                # Make sure the values have no linefeeds as this would interfere with simple parsing.
                v = str(v).replace("\n", " ;;; ").replace("\r", "")
                record += "%s=%s|" % (k, v)

            # Add the record to the file
            self.record_log.write("%s\n" % record)
            self.record_log.flush()

    def resolve_image(self, distgit_name):
        if distgit_name not in self.image_map:
            raise IOError("Unable to find image metadata in group: %s" % distgit_name)
        return self.image_map[distgit_name]

    def resolve_stream(self, stream_name):

        # If the stream has an override from the command line, return it.
        if stream_name in self.stream_alias_overrides:
            return self.stream_alias_overrides[stream_name]

        if stream_name not in self.streams:
            raise IOError("Unable to find definition for stream: %s" % stream_name)

        return self.streams[stream_name]