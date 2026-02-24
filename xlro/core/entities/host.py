#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from builtins import zip
from cgi import test
from xlro.core.util.general_utils import old_div
from builtins import object
from abc import ABCMeta, abstractmethod
from datetime import datetime, timedelta
import threading
import subprocess
import os
import time
import math
import re
import socket
from tempfile import mkdtemp
from shutil import rmtree
from typing import Optional, Tuple, Dict, Any, Union, Mapping
import logging
from xlro.core.util.common import get_path
host_logger = logging.getLogger('xlro.core.entities.host')
from concurrent.futures import ThreadPoolExecutor
from xlro.core import infra_conf
from xlro.core.entities.base import entity, SourceTypes, PropertySpec, NamedEntity, prop_loader, BaseEntity
from xlro.core.entities.sdk_base import SDKEntity, sdk_entity
from xlro.core.util.ssh import Connection, SshException, remote_python_command, execute_cmd_locally
from xlro.core.util.general_utils import wait_for_it, host_name
from xlro.core.util.ipmi import getIpmiByNode
from xlro.core.util.config_file import TextConfig, PropsConfig, ModConfig, conf_to_dict
MAX_REBOOT_TIME = 600
# TODO: move these if/when we have a cluster object
CTYPE_LOCK = threading.Lock()
FUNC2CTYPE_CACHE: Dict[str, Any] = {}
STRUCT2CTYPE_CACHE: Dict[str, dict] = {}


@entity(sourcetypes=[SourceTypes.MANAGEMENT])
class BaseService(NamedEntity): # Should be ABCMeta?
    host : 'Host' = PropertySpec('Host', key=True)

    def execute(self, cmd, inbuf=None, **kwargs):
        return self.host.connection.execute(cmd, inbuf=inbuf, **kwargs)

    def spawn(self, cmd, inbuf=None, **kwargs):
        return self.host.connection.spawn(cmd, inbuf=inbuf, **kwargs)

    @abstractmethod
    def status(self):
        pass

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def stop(self):
        pass

    @abstractmethod
    def restart(self):
        pass


@entity(sourcetypes=[SourceTypes.MANAGEMENT])
class Process(BaseService):
    startcmd : str = PropertySpec(str)

    def status(self):
        return self.execute('pgrep -a {}'.format(self.name))[2]

    def start(self):
        return self.execute('sudo {}'.format(self.startcmd))[2] # or self.name?

    def stop(self):
        return self.execute('sudo pkill {}'.format(self.name))[2]

    def kill(self, signal=9):
        return self.execute('sudo pkill -{} {}'.format(signal, self.name))[2]

    def restart(self):
        self.stop()
        self.start()


@entity(sourcetypes=[SourceTypes.MANAGEMENT])
class Service(BaseService):
    PID_DIR = "/var/run/nvmesh"
    pid_path : str = PropertySpec(str)

    class STATUS(object):
        UP = 0
        DOWN = 3
        NOT_FOUND = 4

    DEPENDENCIES_DICT = {
        'nvmeshclient': {
            'start': {'nvmeshclient', 'nvmeshcm', 'nvmeshagent'},
            'stop': {'nvmeshclient', 'nvmeshtarget', 'nvmeshtoma', 'nvmeshcm', 'nvmeshagent'},
            'restart': {'nvmeshclient'}
        },
        'nvmeshtarget': {
            'start': {'nvmeshclient', 'nvmeshtarget', 'nvmeshtoma', 'nvmeshcm', 'nvmeshagent'},
            'stop': {'nvmeshtarget', 'nvmeshtoma'},
            'restart': {'nvmeshtarget'}
        },
        'nvmeshmgr': {
            'start': {'nvmeshmgr'},
            'stop': {'nvmeshmgr'},
            'restart': {'nvmeshmgr'}
        },
        'nvmeshtoma': {
            'start': {'nvmeshclient', 'nvmeshtarget', 'nvmeshtoma', 'nvmeshcm', 'nvmeshagent'},
            'stop': {'nvmeshtarget', 'nvmeshtoma'},
            'restart': {'nvmeshtoma'}
        },
        'nvmeshcm': {
            'start': {'nvmeshcm'},
            'stop': {'nvmeshclient', 'nvmeshtarget', 'nvmeshtoma', 'nvmeshcm', 'nvmeshagent'},
            'restart': {'nvmeshcm'}
        },
        'nvmeshagent': {
            'start': {'nvmeshagent'},
            'stop': {'nvmeshclient', 'nvmeshtarget', 'nvmeshtoma', 'nvmeshcm', 'nvmeshagent'},
            'restart': {'nvmeshagent'}
        },
        'nvmeshum': {
            'start': {'nvmeshclient', 'nvmeshum'},
            'stop': {'nvmeshum'},
            'restart': {'nvmeshum'}
        }
    }

    def _service_cmd(self, subcmd='status', **kwargs):
        return self.execute('sudo timeout --foreground 3m systemctl {} {}'.format(subcmd, self.name), **kwargs)[2]

    def status(self, **kwargs):
        return self._service_cmd('status', **kwargs)

    def start(self, **kwargs):
        return self._service_cmd('start', **kwargs)

    def stop(self, **kwargs):
        return self._service_cmd('stop', **kwargs)

    def restart(self, **kwargs):
        return self._service_cmd('restart', **kwargs)

    def reset_failed(self, **kwargs):
        return self._service_cmd('reset-failed', **kwargs)

    def get_runlevel_info(self):
        return self.execute("systemctl list-unit-files | grep -i {} | awk {}".format(self.name, "'{print $2}'"))

    def enable(self):
        return self._service_cmd('enable')

    def disable(self):
        return self._service_cmd('disable')

    def get_pid(self) -> int:
        pid = int(Connection.err2exc(self.execute("sudo cat {}.pid".format(os.path.join(self.PID_DIR, self.pid_path)))))
        if pid <= 1:
            raise Exception('service pid must > 1. given pid - {}'.format(pid))
        return pid

    def kill(self, signal=9):
        return self.execute('sudo kill -{} {}'.format(signal, self.get_pid()))[2]


@entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC])
class Host(BaseEntity):
    CFGPATH = '/etc/nvmesh/nvmesh.conf'
    TRACE_CONFIG_PATH = '/var/log/nvmesh/trace.config'
    MODULE_PATH = "/sys/module"
    CORE_PATTERNS = ["/var/crash/*/dump*", "/var/crash/*/vmcore", "/var/opt/nvmesh/core*",
                     # TOMA has started dropping cores in many more cases, but it's not a real error
                     # TODO: somehow this must be restored.  Could be a new toma-core-watcher which will
                     # controlled per-test/per-disaster.
                     # "/var/lib/systemd/coredump/core.nvmeibt_toma*.lz4",
                     # "/var/lib/systemd/coredump/*nvmeibt*",
                     "/var/lib/systemd/coredump/*reactor*"]
    env_paths = None

    name : str = PropertySpec(str, key=True)
    services : Mapping[str, BaseService] = PropertySpec({'name': BaseService})
    dmi_info: dict = PropertySpec(dict) # dmidecode
    lscpu_info: dict = PropertySpec(dict) # lscpu
    info : dict = PropertySpec(dict) # uname -r
    os_info : dict = PropertySpec(dict) # /etc/os-release
    platform : str = PropertySpec(str) # deprecated in python, so going away soon
    nvmesh_config : PropsConfig = PropertySpec(PropsConfig) # Deprecated.  Doesn't handle .file and enables writes
    _nvmeshconf = None
    trace_config : TextConfig = PropertySpec(TextConfig)
    module_config : ModConfig = PropertySpec(ModConfig)
    proxy : str = PropertySpec(str, default="")
    git_info : dict = PropertySpec(dict)
    ofed_support : bool = PropertySpec(bool)
    ofed_version: str = PropertySpec(str, default="")

    def nvmeshconf(self, reload=False) -> Dict[str, Any]:
        if self._nvmeshconf is not None and not reload:
            return self._nvmeshconf

        self._nvmeshconf = {}
        conf_paths = ['/etc/nvmesh/nvmesh.conf', '/etc/nvmesh/.nvmesh.conf']
        # Enable local configuration by setting TEST_NVMESH_CONF environment variable. For testing
        test_conf_path = os.environ.get('TEST_NVMESH_CONF')
        if test_conf_path:
            conf_paths.append(test_conf_path)
        self.logger.debug(f'Loading conf from {self.name} local? {self.connection.is_local}, files: {conf_paths}')
        for conf_path in conf_paths:
            if self.connection.is_local:
                # support distroless containers with no cat
                try:
                    with open(conf_path, 'r') as fp:
                        self._nvmeshconf.update(conf_to_dict(fp.read()))
                except Exception as e:
                    self.logger.debug(f'Loading conf from {conf_path} on {self.name} failed. {repr(e)}')
            else:
                self._nvmeshconf.update(conf_to_dict(self.execute(f'cat {conf_path} 2>/dev/null')[0]))

        # For backwards compatibility
        self._nvmeshconf.setdefault('_REST_SERVERS', self._nvmeshconf.get('MANAGEMENT_SERVERS', ''))
        self.logger.debug(f'Loading conf from {self.name}. Key count: {len(self._nvmeshconf)}. REST_SERVERS: {self._nvmeshconf["_REST_SERVERS"]}')

        return self._nvmeshconf

    def __init__(self, *args, **kwargs):
        from xlro.core.entities.client import ClientNode
        from xlro.core.entities.target import Target
        self.proc_lock = threading.Lock()

        super(Host, self).__init__(*args, **kwargs)
        self.services_model = {
            ClientNode: ClientNode.build_services(self),
            Target: Target.build_services(self)
        }
        self._time_delta = None
        self._cmds_to_full_path: Dict[str, str] = {}
        self.cmd_path_cache: Dict[str, Union[str, EnvironmentError]] = {}

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(Host, cls).map_props(propmap, source_type)
        if 'name' in propmap:
            propmap['name'] = host_name(propmap['name'])
        return propmap

    @prop_loader(SourceTypes.MANAGEMENT, ['services'])
    def _load_services(self):
        return {'services': self.create_services_by_roles(self._get_roles())}

    def create_services_by_roles(self, roles):
        services: Dict[str, Service] = {}

        for role in roles:
            services.update(self.services_model[role])

        return services

    def _get_roles(self):
        from xlro.core.entities.manager import Manager
        manager = Manager.get_manager()

        return [n.__class__ for n in manager.client_nodes + manager.targets if n.host == self] # type: ignore[operator]

    def execute(self, cmd, **kwargs):
        return self.connection.execute(cmd, **kwargs)

    @classmethod
    def execute_on_all(cls, hosts, cmd, **kwargs):
        with ThreadPoolExecutor() as tpe:
            results = tpe.map(lambda host: host.execute(cmd, **kwargs), hosts)
        return dict(list(zip(hosts, results)))

    @property
    def connection(self) -> Connection:
        conn_kwargs = {'fail_is_error': False}

        if self.proxy:
            conn_kwargs['proxy'] = self.proxy
        if hasattr(self, '_is_switch') and self._is_switch:
            conn_kwargs['allow_agent'] = False
            conn_kwargs['look_for_keys'] = False

        try:
            return Connection.get_connection(self.name, **conn_kwargs)
        except socket.error:
            raise SshException('unable to connect to {}'.format(self.name))

    def reboot(self, force_level=0, wait=True):
        self.connection.execute('ls .')     # verify connected before reboot
        force_flag = '--force' * force_level
        cmd = 'sudo reboot --no-wall {}'.format(force_flag)
        # JW: I really don't understand why reconnect was 0.
        # In the off chance there's a hiccup in ssh (and it happens) we need to retry.
        # Changing to 2s (which should, be default, enable 3 retries.)
        try:
            self.connection.execute(cmd, reconnect_timeout=2, timeout=30)
        except Exception as e:
            host_logger.warning(f'Exception: {type(e)} from reboot.execute() expected.')
            raise
        if wait:
            wait_for_it(self._disconnected)
            self._reconnect_and_verify_reboot()

    def ipmi(self, cmd, wait=True):
        from xlro.infra.plugins.config_storage import config_storage
        ipmi_mgr = None
        if cmd in ['reset', 'power_cycle']:
            self.connection.execute('ls .')  # verify connected before reboot
        if config_storage.cluster.cloud.platform == 'azure':
            resource_group = config_storage.cluster.cloud.tag
            if not resource_group:
                raise Exception("Resource group was not provided in cluster_config. Unable to perform operation.")

            if cmd == 'reset' or cmd == 'power_cycle':
                cmd = 'restart'

            execute_cmd_locally('az vm {} -g {} -n {}'.format(cmd, resource_group, self.name.split('.')[0]))

        else:
            ipmi_mgr = getIpmiByNode(self.name)
            if cmd == 'reset':
                power = ipmi_mgr.reset()
            elif cmd == 'power_cycle':
                power = ipmi_mgr.powerCycle()
            elif cmd == 'on':
                power = ipmi_mgr.powerOn()
            elif cmd == 'off':
                power = ipmi_mgr.powerOff()
            else:
                raise Exception(f'cmd {cmd} is not supported')
            if power.returnCode != 0:
                raise Exception('failed to execute IPMI {} command'.format(cmd))
        if wait:
            if cmd in ['reset', 'power_cycle']:
                wait_for_it(self._disconnected)
                self._reconnect_and_verify_reboot()
            elif ipmi_mgr is not None:
                if cmd == "on":
                    wait_for_it(ipmi_mgr.is_power_on).assert_result(f"Powering on {self.name} timed out")
                else:
                    wait_for_it(ipmi_mgr.is_power_off).assert_result(f"Powering off {self.name} timed out")

    def _disconnected(self):
        try:
            self.connection.execute('ls .', reconnect_timeout=0, timeout=30)
            host_logger.debug('after reboot, host still connected')
            return False
        except Exception:
            host_logger.debug('after reboot, host is disconnected')
            return True

    def _connected(self):
        try:
            self.connection.execute('ls .', reconnect_timeout=0, timeout=30)
            host_logger.debug('check for connection after reboot, host is connected')
            return True
        except Exception:
            host_logger.debug('check for connection after reboot, host is still disconnected,')
            return False

    def _reconnect_and_verify_reboot(self):
        host_logger.info('after reboot, waiting for reconnect')
        self._wait_for_reconnect(MAX_REBOOT_TIME)
        if 'min' not in self.connection.execute('uptime | awk {}'.format("'{print $4}'"))[0] or \
                int(self.connection.execute('uptime | awk {}'.format("'{print $3}'"))[0]) > math.ceil(old_div(MAX_REBOOT_TIME,60)):
            raise Exception('failed to execute reboot/ipmi command')

    def _wait_for_reconnect(self, timeout):
        start_time = datetime.now()
        elapsed_time = 0
        while elapsed_time <= timeout:  # need the loop if the exception is in getting the connection,
                                                # otherwise the execute does the job
            try:
                self.connection.execute('ls .', reconnect_timeout=(timeout - elapsed_time))
                return
            except Exception:
                time.sleep(10)
                elapsed_time = int((datetime.now() - start_time).total_seconds())

        raise Exception('ssh connection to host {} not established {} seconds after reboot/ipmi command' \
                        .format(self.name, MAX_REBOOT_TIME))

    def check_all_services(self, ignore_services=None):
        if ignore_services is None:
            ignore_services = []
        return all(service.status() in (0, 4)
                   for service in list(self.services.values()) if service.name not in ignore_services)

    @prop_loader(SourceTypes.PROC, ['lscpu_info'])
    def load_lscpu_info(self):
        lscpu_regex = ['Socket\(s\)', 'Model[^:]', 'CPU\(s\)']
        out, err, code = self.connection.execute(f"lscpu | grep -e ^{' -e ^'.join(lscpu_regex)}")
        freq_cmd = "awk -F: '/cpu MHz/ {sum+=$2; n++} END {if(n) print \"CPU MHz:\", sum/n}' /proc/cpuinfo" # grep all cpu MHz from /proc/cpuinfo and calculate the average. Compatible with most Linux distros.
        freq_out, err, code = self.connection.execute(freq_cmd)
        out+=freq_out
        return {'lscpu_info': {k: v.lstrip() for k, v in [line.split(':', 1) for line in out.strip().split('\n')]}}

    @prop_loader(SourceTypes.PROC, ['dmi_info'])
    def load_dmi_info(self):
        dmi_args = [
            'system-manufacturer',
            'system-product-name',
            'system-version',
            'system-serial-number',
            'processor-frequency'
        ]
        out, err, code = self.connection.execute('for opt in {}; do echo $opt=$(sudo dmidecode -s $opt); done'.format(' '.join(dmi_args)))
        return {'dmi_info': {parts[0].strip(): parts[2].strip() for parts in [(line.decode() if type(line) == bytes else line).partition('=') for line in out.split('\n')[:-1]]}}

    @prop_loader(None, ['info'])
    def load_get_info(self, source=None): # pylint: disable=unused-argument
        uname_args = [
            'kernel-name',
            'nodename',
            'kernel-release',
            'kernel-version',
            'machine',
            'processor',
            'hardware-platform',
            'operating-system',
            ]
        out = self.connection.spawn('for opt in {}; do echo $opt=$(uname --$opt); done'.format(' '.join(uname_args)))[1]
        return {'info': {parts[0].strip():parts[2].strip() for parts in [(line.decode() if type(line) == bytes else line).partition('=') for line in out]}}

    @prop_loader(None, ['os_info'])
    def load_get_os_info(self, source=None): # pylint: disable=unused-argument
        out = Connection.err2exc(self.connection.execute('cat /etc/os-release'))
        # Parse fields in /etc/os-release, which is now rather globally standard on Linux
        # NOTE: Recommended to only rely on ID (simplified, lowercase name) and X_VERSION_ID (see below)
        os_info = { m.groupdict()['k']: m.groupdict()['v'] \
                for m in re.finditer(r'^\s*(?P<k>[^#\s=]+)\s*=\s*"?(?P<v>[^"#\n]*[^"#\s])', out, re.MULTILINE) }
        # Centos (maybe others) gives only major version as VERSION_ID, so we supply X_VERSION_ID as more complete version
        # I prefer we use custom keys vs. overriding standard ones, so I don't overwrite VERSION_ID
        out = self.connection.execute('grep -oP "release *\K\d+(\.\d+)?" /etc/system-release 2>/dev/null')[0].strip()
        os_info['X_VERSION_ID'] = out or os_info['VERSION_ID']
        return { 'os_info': os_info }

    @prop_loader(None, ['platform'])
    def load_platform(self, source=None): # pylint: disable=unused-argument
        return {'platform': f"{self.os_info['ID']} {self.os_info['X_VERSION_ID']}"}

    @prop_loader(None, ['nvmesh_config'])
    def load_get_config(self, source=None):  # pylint: disable=unused-argument
        return {'nvmesh_config': PropsConfig(self, self.CFGPATH).upload()}

    @prop_loader(None, ['trace_config'])
    def load_get_trace_config(self, source=None):  # pylint: disable=unused-argument
        return {'trace_config': TextConfig(self, self.TRACE_CONFIG_PATH).upload()}

    @prop_loader(None, ['module_config'])
    def load_get_module_config(self, source=None):
        return {'module_config': ModConfig(self, self.MODULE_PATH).upload()}

    @prop_loader(None, ['git_info'])
    def load_git_info(self, source=None): # pylint: disable=unused-argument
        # method returns None so it wont fail the test if poperity it N/A
        installed_nvmesh_pkgs = self.list_installed_nvmesh_rpms()
        for pkg in ['um', 'client', 'core']:
            if f'nvmesh-{pkg}' in installed_nvmesh_pkgs:
                PKG_NAME = f'nvmesh-{pkg}'
                break
        else:
            logging.warn('No nvmesh package found to extract git info')
            return {'git_info': {}}

        if "ubuntu" in self.platform:
            pkg_info_cmd = f"apt-cache show {PKG_NAME}"
        else:
            pkg_info_cmd = f"rpm -q --info {PKG_NAME}"
        out, err, code = self.connection.execute(pkg_info_cmd)
        if err or code != 0:
            logging.warn('{} failed: EXIT: {}: {}'.format(pkg_info_cmd, code, err))
            return {'git_info': {}}
        match = re.search("(\s*Version\s*:\s*(?P<version>.+)\n)(.*\n)*(\s*Branch\s*:\s*(?P<branch>.+)\n).*(\s*Commit\s*:\s*(?P<commit>.+)\n).*(\s*OFED\s*:\s*(?P<ofed>.+)\n)?", out)
        if not match:
            logging.warn("Can't parse git info fields. OUT: {}".format(out))
            return {'git info': {}}
        return {'git_info': match.groupdict()}

    def package_version(self, pkg):
        out, err, code = self.connection.execute(
            'dpkg-query --showformat="\${{Version}}" --show {pkg} 2>/dev/null || rpm -q {pkg} --queryformat "%{{VERSION}}-%{{RELEASE}}"'.format(pkg=pkg))
        if code or err:
            raise Exception('Failed to get version. code={}, err={}'.format(code, err or out))
        return out.strip()

    def list_installed_nvmesh_rpms(self):
        out, err, code = self.connection.execute(
            'dpkg -l 2>/dev/null | awk "/^ii/ {print $2}" | grep "nvmesh-" || '
            'rpm -qa "nvmesh-*" --queryformat "%{NAME}\\n"'
        )
        assert not (code or err), f'Failed to get nvmesh packages on {self.name} node. code={code}, err={err}'
        return out.split()

    PROC2RPC: Dict[str,str] = {
        'volumes/status.json':
            'sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_rpc2proc.py --method volume_status --name {name}',
        'volumes/iostats.json':
            'sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_rpc2proc.py --method volume_iostats_counters --name {name}',
        'volumes/flow_cntr.json':
            'sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_rpc2proc.py --method volume_flow_counters --name {name}',
        'volumes/blob.txt':
             'sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_rpc2proc.py --method volume_attach_info --name {name}',
        'volumes/client_processes':
             'sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_rpc2proc.py --method volume_client_processes --name {name}',
        # 'disks/status':
    }

    def _rpc_content(self, path: str) -> str:
        ''' An RPC equivalent for UM nodes '''
        try:
            matchd = re.match(r'/proc/nvmeibc/(?P<pdir>[^/]+)/(?P<name>[^/]+)/(?P<pfile>[^/]+)', path).groupdict()
            pdir = matchd['pdir']
            name = matchd['name']
            pfile = matchd['pfile']
            cmd = self.PROC2RPC[f'{pdir}/{pfile}'].format(**matchd)
            out, _, _ = self.connection.execute(cmd, success=0)
            return out
        except Exception as e:
            self.logger.info(f'Failed to get RPC equivalent of {path}. {repr(e)}')
            raise


    _proc_content_dict: Dict[str, str] = {}
    def proc_content(self, path: str, no_cache: Optional[bool] = False) -> str:
        from xlro.core.entities.client import Client
        if path in self._proc_content_dict and not no_cache:
            return self._proc_content_dict[path]
        with self.proc_lock:
            if path in self._proc_content_dict and not no_cache:
                return self._proc_content_dict[path]
            # For UMClient, we get equivalent from RPC
            if path.startswith('/proc/nvmeibc/') and Client.instance(name=self.name).isUmClient:
                out = self._rpc_content(path)
            else:
                cmd = 'sudo cat {}'.format(path)
                out, err, code = Connection.execute_on_host(self.name, cmd)
                if code != 0 or not out:
                    raise OSError('Failed to read proc. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))
            self._proc_content_dict[path] = out
            return out

    @property
    def time_delta(self):
        def clockdiff(conn):
            output, err, code = conn.execute('clockdiff {h_name} 2>/dev/null || sudo clockdiff {h_name}'.
                                                  format(h_name=self.name))
            match = re.match(r'\d+ (?P<ms>-?\d+) -?\d+', output)
            assert match, 'Failed to execute/parse clockdiff. OUT: {}'.format(output)
            self._time_delta = timedelta(milliseconds=int(match.groupdict()['ms']))

        def ntpdate(conn):
            out, err, code = conn.execute('ntpdate -q -p 1 -t 1 {}'.format(self.name))
            match = re.search(r'offset (?P<time>-?\d+.\d+) sec', out)
            assert match, "failed to parse ntpdate output {}".format(out)
            t = float(match.groupdict()['time'])
            self._time_delta = timedelta(milliseconds=int(t * 1000))

        def infra_clockdiff(conn):
            exe = get_path('{}/../util/infra_clockdiff/clockdiff'.format(os.path.dirname(__file__)))
            output, err, code = conn.execute('sudo {} -c 1 {}'.
                                                  format(exe, self.name))
            match = re.match(r'\d+ (?P<ms>-?\d+) -?\d+', output)
            assert match, 'Failed to execute/parse clockdiff. OUT: {}'.format(output)
            self._time_delta = timedelta(milliseconds=int(match.groupdict()['ms']))

        if not self._time_delta:
            ssh_opts = Connection.ssh_options(self.name)
            if 'proxycommand' in ssh_opts or 'proxy' in ssh_opts:
                self._time_delta = timedelta()
                return self._time_delta

            localhost = Host.instance(name='localhost')
            if Connection.localhostname().split('.')[1:] == self.name.split('.')[1:]:
                methods = [clockdiff, ntpdate, infra_clockdiff]
            else:
                # ntpdate will much be faster on case where not same LAN
                methods = [ntpdate, infra_clockdiff, clockdiff]

            for m in methods:
                try:
                    m(localhost)
                    return self._time_delta
                except Exception as e:
                    self.logger.debug("calc time delta using {} failed: {}".format(m.__name__, repr(e)))  # type: ignore[attr-defined]
        else:
            return self._time_delta
        raise Exception("Could not load time_delta")

    def run_sh(self, script: str, timeout: Optional[int] = None, args: str = '') -> Tuple[str, str, int]:
        EOF = 'EndOfScript'

        cmd = 'bash -s ' + args + ' <<\\' + EOF + '\n\n' + script + '\n\n' + EOF + '\n\n'

        if timeout:
            return self.connection.execute(cmd, timeout=timeout)
        return self.connection.execute(cmd)

    def cache_cmd_path(self, cmd, lookup_paths=''):
        """
        :param cmd: Command name only - without arguments
        :param lookup_paths: Colon separated paths for command lookup. example: /usr/bin:/usr/sbin:/a/b/c
        :return path: string with command path from cache
        """
        path = self.cmd_path_cache.get(cmd, None)
        if isinstance(path, Exception):
            raise path
        elif not path:
            if not self.env_paths:
                self.env_paths = Connection.err2exc(self.connection.execute(
                    r"(env ; sudo env) | grep -oP 'PATH=\K.*' | paste -sd : -")).rstrip('\n')

            lookup_paths = ':'.join([lookup_paths, self.env_paths])
            try:
                path = Connection.err2exc(self.connection.execute(
                    "PATH={} bash -c 'type -p {}'".format(lookup_paths, cmd))).rstrip('\n')
            except Exception as e:
                env_error = EnvironmentError('Command {} not found on host {} - {}'.format(cmd, self.name, repr(e)))
                self.cmd_path_cache[cmd] = env_error
                raise env_error

            self.logger.debug('Caching command path: {} for {}'.format(path, self.name))
            self.cmd_path_cache[cmd] = path

        return path

    @staticmethod
    def _set_ctypes_cache(ctypes_dict):
        for name, ctype in list(ctypes_dict.items()):
            namematch = re.match('(struct|union|enum)__(?P<name>.+)', name)
            if namematch:
                STRUCT2CTYPE_CACHE[namematch.group('name')] = ctype

    def _fetch_ofile(self, ofile, local_dir, **kwargs):
        if Connection.LOCALHOST_CHECK:
            return ofile

        suff = kwargs.get('suff', 'gz')
        remote_dir = kwargs.get('remote_dir', '/tmp/')
        ofile_name = ofile.split('/')[-1]
        local_obj_path = os.path.join(local_dir, ofile_name)
        lzip_path = '{}.{}'.format(local_obj_path, suff)
        rzip_path = '{}{}.{}'.format(remote_dir, ofile_name, suff)

        try:
            # compress a remote .ko/.o file
            self.logger.debug('Compressing {}:{} and copying locally to {}'.format(self.name, rzip_path, local_dir))
            out, err, code = self.connection.execute(
                'sudo sh -c "(ls {ofile} || {modinfo} -F filename {ofile}) | xargs -r gzip -c >{rzip_path}; test -s {rzip_path}"'.format(
                    ofile=ofile, rzip_path=rzip_path, modinfo=self.cache_cmd_path('modinfo')))
            assert code == 0, 'No gzip output. ' + (err or out)
        except Exception as e:
            self.logger.debug('Unable to compress object {}:{} - {}'.format(self.name, ofile, repr(e)))
            raise e

        # copy compressed file locally and decompress it
        self.connection.get_dir(local_dir, rzip_path)
        self.connection.execute('sudo rm {}'.format(rzip_path))
        self.logger.debug('Locally decompressing {} of {}'.format(lzip_path, self.name))
        execute_cmd_locally('gzip -df {}'.format(lzip_path))
        return local_obj_path

    def _fetch_and_resolve_ofile(self, ofile=None, **kwargs):
        from xlro.core.util.read_dwarf import resolve  # type: ignore

        local_dir = kwargs.get('local_dir', mkdtemp('_ctypes_data'))
        if ofile is None:
            ofile = infra_conf.root.tools.infra_shared_so

        try:
            local_obj_path = getattr(infra_conf.root.tools, ofile.split('/')[-1].replace('.', '_')) or \
                             self._fetch_ofile(ofile, local_dir, **kwargs)
            _, types, funcs, _ = resolve(local_obj_path)
            FUNC2CTYPE_CACHE.update(funcs.as_dict)
            self._set_ctypes_cache(types.as_dict)
        except Exception as e:
            self.logger.debug('Unable to resolve object file {} from host {} - {}'.format(ofile, self.name, repr(e)))
            raise e
        finally:
            try:
                rmtree(local_dir)
            except:
                pass

    @staticmethod
    def ctype_res_by_is_func(c_item, is_func):
        return FUNC2CTYPE_CACHE[c_item] if is_func else STRUCT2CTYPE_CACHE[c_item]

    def get_ctype(self, c_item, is_func=False, **kwargs):
        """
        A method to load ctypes from a given .o/.ko file path or a kernel module name.
        This method would also cache all structs located under the same .o file according to struct_map
        :param c_item: The required struct
        :param is_func: return a cached cfunction vs a ctype struct
        :return: ctype object of requested struct or None in case of failure to find struct
        """
        try:
            return self.ctype_res_by_is_func(c_item, is_func)
        except KeyError:
            with CTYPE_LOCK:
                try:
                    return self.ctype_res_by_is_func(c_item, is_func)
                except KeyError:
                    try:
                        self._fetch_and_resolve_ofile(**kwargs)
                        return self.ctype_res_by_is_func(c_item, is_func)
                    except Exception as e:
                        self.logger.exception('Unable to resolve struct {} from {}'.format(c_item, self.name))

            raise KeyError('Unable to fetch or resolve struct {} from cache'.format(c_item))

    @prop_loader(SourceTypes.PROC, ['ofed_support'])
    def _load_ofed_support(self):
        try:
            _, _, code = self.connection.execute('ofed_info -s')
        except:
            # TODO: Happened with sims.  Something we'll need to handle more generally
            code = 1
        return {'ofed_support': not code}

    @prop_loader(SourceTypes.PROC, ['ofed_version'])
    def _load_ofed_version(self):
        # leaving older api 'ofed-support' as someone may still be using it
        try:
            out, err, code = self.connection.execute('ofed_info -n')
            assert code == 0, "ofed_info -n on {} return {}: {}".format(self.name, code, err)
        except Exception as e:
            self.logger.debug(repr(e))
        return {'ofed_version': out.strip()}

    def syslog(self, msg, priority='debug', tag='NVMESH-INFRA'):
        from shlex import quote
        self.connection.execute(f'logger -p "{priority}" -t "{tag}" {quote(msg)}')


def main():
    from xlro.core.util.cli_util import CLIArgumentParser
    from xlro.core.util.general_utils import get_hostnames
    parser = CLIArgumentParser(require_manager=False)
    parser.add_argument('names', nargs='+')
    args = parser.parse_args()
    for host in args.names:
        print(host, 'w/ localhost - ', get_hostnames(host, skip_cache=True, allow_local=True))
        print(host, 'no localhost - ', get_hostnames(host, skip_cache=True))
        print(Host.instance(name=host).git_info)
    return 0

if __name__ == '__main__':
    import sys
    main()
    sys.exit(0)
