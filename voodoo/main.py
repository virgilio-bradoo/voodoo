#!/usr/bin/env python
# coding: utf-8

from plumbum import cli, local
from plumbum.cmd import git, docker, grep, sed
from plumbum.commands.modifiers import FG, TF, BG
from plumbum.cli.terminal import choose
import logging
import os
import sys
from compose.cli.command import get_project
from compose.project import OneOffFilter
from compose.parallel import parallel_kill
import yaml
from .hook import Deploy, InitRunDev, GenerateDevComposeFile

compose = local['docker-compose']


DEFAULT_CONF = {
    "shared_eggs": True,
    "shared_gems": True,
    "odoo": "https://github.com/oca/ocb.git",
    "template": "https://github.com/akretion/voodoo-template.git",
    "env": "dev",
}

DOCKER_COMPOSE_PATH = '%s.docker-compose.yml'


class Voodoo(cli.Application):
    PROGNAME = "voodoo"
    VERSION = "1.0"

    dryrun = cli.Flag(["dry-run"], help="Dry run mode")

    def _run(self, cmd, retcode=FG):
        """Run a command in a new process and log it"""
        logging.info(cmd)
        if (self.dryrun):
            print cmd
            return True
        return cmd & retcode

    def _exec(self, cmd, args=[]):
        """Run a command in the same process and log it
        this will replace the current process by the cmd"""
        logging.info([cmd, args])
        if (self.dryrun):
            print "os.execvpe (%s, %s, env)" % (cmd, [cmd] + args)
            return True
        os.execvpe(cmd, [cmd] + args, local.env)

    def _get_home(self):
        return os.path.expanduser("~")

    def __init__(self, executable):
        super(Voodoo, self).__init__(executable)
        self.home = self._get_home()
        self.shared_folder = os.path.join(self.home, '.voodoo', 'shared')
        config_path = os.path.join(self.home, '.voodoo', 'config.yml')

        # Read existing configuration
        if os.path.isfile(config_path):
            config_file = open(config_path, 'r')
            config = yaml.safe_load(config_file)
        else:
            config = {}

        # Update configuration with default value and remove dead key
        new_config = DEFAULT_CONF.copy()
        for key, value in DEFAULT_CONF.items():
            if key in config:
                new_config[key] = config[key]
            # Set Configuration
            setattr(self, key, new_config[key])

        # Update config file if needed
        if new_config != config:
            print ("The Voodoo Configuration have been updated, "
                   "please take a look to the new config file")
            if not os.path.exists(self.shared_folder):
                os.makedirs(self.shared_folder)
            config_file = open(config_path, 'w')
            config_file.write(yaml.dump(new_config, default_flow_style=False))
            print "Update default config file at %s" % config_path

    @cli.switch("--verbose", help="Verbose mode")
    def set_log_level(self):
        logging.root.setLevel(logging.INFO)
        logging.info('Verbose mode activated')


class VoodooSub(cli.Application):

    def _exec(self, *args, **kwargs):
        self.parent._exec(*args, **kwargs)

    def _run(self, *args, **kwargs):
        self.parent._run(*args, **kwargs)

    def _get_main_compose_file(self):
        for fname in ['docker-compose.yml', 'prod.docker-compose.yml']:
            if os.path.isfile(fname):
                return fname
        print "No docker.compose.yml or prod.docker.compose.yml found"
        sys.exit(0)

    def _get_main_service(self):
        dc_fname = self._get_main_compose_file()
        dc_file = open(dc_fname, 'r')
        config = yaml.safe_load(dc_file)
        for name, vals in config['services'].items():
            if vals.get('labels', {}).get('main_service') == "True":
                return name
        print (
            'No main service found, please define one in %s'
            'by adding the following label : main_service: "True"'
            'to your main service' )
        sys.exit(0)

    def run_hook(self, cls):
        for subcls in cls.__subclasses__():
            if subcls._service == self.main_service:
                return subcls(self).run()

    def __init__(self, *args, **kwargs):
        super(VoodooSub, self).__init__(*args, **kwargs)
        if args and args[0] == 'voodoo new':
            return
        self.config_path = DOCKER_COMPOSE_PATH % self.parent.env
        self.main_service = self._get_main_service()
        if self.parent.env == 'dev':
            if not os.path.isfile(self.config_path):
                self.run_hook(GenerateDevComposeFile)
        self.compose = compose['-f', self.config_path]


@Voodoo.subcommand("deploy")
class VoodooDeploy(VoodooSub):
    """Deploy your application"""
    def main(self):
        self.run_hook(Deploy)


@Voodoo.subcommand("run")
class VoodooRun(VoodooSub):
    """Start services and enter in your dev container"""

    def main(self, *args):
        if self.parent.env == 'dev':
            self.run_hook(InitRunDev)
        # Remove useless dead container before running a new one
        self._run(self.compose['rm', '--all', '-f'])
        self._exec('docker-compose', [
            '-f', self.config_path,
            'run', '--service-ports',
            self.main_service, 'bash'])


@Voodoo.subcommand("open")
class VoodooOpen(VoodooSub):
    """Open a new session inside your dev container"""

    def main(self, *args):
        project = get_project('.', [self.config_path])
        container = project.containers(
            service_names=[self.main_service], one_off=OneOffFilter.include)
        if container:
            self._exec('docker',
                       ["exec", "-ti", container[0].name, "bash"])
        else:
            logging.error("No container found for the service odoo "
                      "in the project %s" % project.name)


@Voodoo.subcommand("kill")
class VoodooKill(VoodooSub):
    """Kill all running container of the project"""

    def main(self, *args):
        # docker compose do not kill the container odoo as is was run
        # manually, so we implement our own kill
        project = get_project('.', config_path=[self.config_path])
        containers = project.containers(one_off=OneOffFilter.include)
        parallel_kill(containers, {'signal': 'SIGKILL'})


@Voodoo.subcommand("new")
class VoodooNew(VoodooSub):
    """Create a new project"""

    def main(self, name):
        # TODO It will be better to use autocompletion
        # see plumbum and argcomplete
        # https://github.com/tomerfiliba/plumbum/blob/master/plumbum
        # /cli/application.py#L341
        # And https://github.com/kislyuk/argcomplete/issues/116
        self._run(git["clone", self.parent.template, name])
        with local.cwd(name):
            get_version = (git['branch', '-a']
                | grep['remote']
                | grep['-v', 'HEAD']
                | sed['s/remotes\/origin\///g'])
            versions = [v.strip() for v in get_version().split('\n')]
        versions.sort()
        version = choose(
            "Select your template?",
            versions,
            default = "9.0")
        with local.cwd(name):
            self._run(git["checkout", version])


@Voodoo.subcommand("inspect")
class VoodooInspect(VoodooSub):
    """Simple Inspection of network will return ip and hostname"""

    def main(self):
        project = get_project('.', config_path=[self.config_path])
        network = project.networks.networks['default'].inspect()
        print "Network name : %s" % network['Name']
        for uid, container in network['Containers'].items():
            print "%s : %s" % (container['Name'], container['IPv4Address'])


class VoodooForward(VoodooSub):
    _cmd = None

    def main(self, *args):
        return self._run(self.compose[self._cmd])


@Voodoo.subcommand("build")
class VoodooBuild(VoodooForward):
    """Build or rebuild services"""
    _cmd = "build"


@Voodoo.subcommand("up")
class VoodooUp(VoodooForward):
    """Start all services"""
    _cmd = "up"


@Voodoo.subcommand("down")
class VoodooDown(VoodooForward):
    """Stop all services"""
    _cmd = "down"


@Voodoo.subcommand("ps")
class VoodooPs(VoodooForward):
    """List containers"""
    _cmd = "ps"


@Voodoo.subcommand("logs")
class VoodooLogs(VoodooForward):
    """View output from containers"""
    _cmd = "logs"


@Voodoo.subcommand("pull")
class VoodooPull(VoodooForward):
    """Pulls service images"""
    _cmd = "pull"


def main():
    Voodoo.run()
