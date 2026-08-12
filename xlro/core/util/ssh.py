#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division

import warnings
from builtins import map
from builtins import str
from builtins import range
from xlro.core.util.general_utils import old_div
from builtins import object
from io import StringIO
import paramiko
import os
import socket
import subprocess
import threading
import time
import logging
import inspect
import stat
import psutil
import sys
import shlex
import json
import re
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from datetime import datetime
from typing import Any,Container,Dict,List,Optional,Tuple,Union,Sequence, IO
from threading import Timer
from xlro.core.util.general_utils import IDAdapter
from xlro.core import infra_conf

# I think we log the Paramiko errors
from xlro.core.util.thread_manager import ThreadPoolManager

logging.getLogger('paramiko').setLevel('CRITICAL')

class paramikoFilter(object):
    def filter(self, record):
        return not record.msg.endswith('Administratively prohibited')


class urlibFilter(logging.Filter):
    def filter(self, record):
        return not record.msg.startswith('Connection pool is full')


# logging.getLogger('paramiko.transport').addFilter(paramikoFilter())
logging.getLogger('urllib3').setLevel('WARN')
logging.getLogger('urllib3').addFilter(urlibFilter(name="connection_pool_filter"))
LOGNAME = 'xlro.core.util.connection'
conn_logger = logging.getLogger(LOGNAME)

from xlro.core.util.general_utils import host_name, wait_for_it, host_aliases

MAX_REBOOT_TIME = 600
DEFAULT_RECONNECTION_TIMEOUT = 60
PY3 = sys.version_info[0] == 3

POPEN_LOCK = threading.Lock() # Seems popen() (incredibly) isn't so thread-safe.  BTW: changing subprocess to subprocess32 made things worse

class SshException(Exception):
    pass

class CmdException(Exception):
    pass


# TODO - import this function at core.util.install_nvmesh.py and replace
def execute_cmd_locally(cmd: str, timeout: Optional[int] = None) -> Tuple[str, str, int]:
    cmdline = cmd.strip().partition('\n')[0]
    conn_logger.info('Executing locally: {}'.format(cmdline))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True)
    start = time.time()

    try:
        if timeout:
            p_timer = Timer(timeout, p.kill, [p])
            p_timer.start()
        stdout, stderr = p.communicate()
        if PY3:
            return (stdout.decode() if stdout else "", stderr.decode() if stderr else "", p.returncode)
        return (stdout, stderr, p.returncode)
    finally:
        if timeout:
            p_timer.cancel()
        conn_logger.info('Exit {} : [{:.3f}s] {}'.format(p.returncode, time.time()-start, cmdline))

def local_execute(cmd, **kwargs):
    ''' Execute via Connection.execute() for consistency of params, features and implementation.

        NOTE: changes in default behavior from legacy execute_cmd_locally():
        - uses default execute() timeout, not None.  If you want no timeout, pass timeout=0 (or None)
        - keeps stdout/stderr seperate, not joined.  If you want them joined, pass stderr=subprocess.STDOUT
    '''
    return Connection.get_connection('localhost').execute(cmd, **kwargs)

def writeall(stream, buf):
    written = True
    # Unforunately, paramiko.ChannelFile doesn't return # of bytes, so if written is None, we can only hope...
    while written and buf:
        written = stream.write(buf)
        buf = buf[written:]


class RemotePopen(object):
    """
    same interface as subprocess.Popen() so other utilities can use either local or remote commands
    """
    LC_ENVARS = {k: v for k, v in os.environ.items() if k in ['LC_NAME', 'LC_IDENTIFICATION']}

    def __init__(self, connection, cmd, inbuf=None, reconnect_timeout=None, **kwargs):
        # Maybe we should ALWAYS use get_pty, or long-running commands will be left on the server...
        self.retry_delay = 0.5
        self.reconnect_timeout = reconnect_timeout if reconnect_timeout is not None else DEFAULT_RECONNECTION_TIMEOUT
        self._connection = connection
        self._cmd = cmd
        self._logcmd = cmd.partition('\n')[0] # [:30]?
        self.logger = IDAdapter(logging.getLogger('{}.RemotePopen'.format(LOGNAME)))
        self.logger.info('Executing: <{}> {}'.format(connection.host, self._logcmd))
        self.stdin, self.stdout, self.stderr = self._exec_command(cmd, **kwargs)
        self._start_time = time.time()
        if inbuf is not None:
            writeall(self.stdin, inbuf)
            self.stdin.close()
            self.stdin.channel.shutdown(1)
        self.pid = None
        self.return_code = None
        self._communicated = None
        self._done_checks = 0
        self._rc_lock = threading.Lock()

    def _ex_str(self, e):
        return 'Exception ({}) {} Cmd: <{}> {}'.format(type(e), e, self._connection.host, self._logcmd)

    @staticmethod
    def run_cmd_on_paramiko_client(conn, cmd, timeout=20, socket_timeout=60, **kwargs):
        # I think it's reasonable to wait 20 seconds to connect, but no more.
        kwargs.setdefault('timeout', timeout)

        # add all LC* to cmd remote environment
        environment = kwargs.pop('environment', {})
        for k, v in RemotePopen.LC_ENVARS.items():
            environment.setdefault(k, v)

        stdin, stdout, stderr = conn.exec_command(cmd, environment=environment, **kwargs)
        # exec_command uses timeout for BOTH connection and read/write timeout. We ONLY want connect timeout.
        # Only option seems to be to reset the channel timeouts afterwards...
        stdout.channel.settimeout(socket_timeout)
        stderr.channel.settimeout(socket_timeout)
        return (stdin, stdout, stderr)

    def _exec_command(self, cmd, **kwargs):
        time_started = datetime.now()
        self._connection.incr_active()
        retry = 0
        while True:
            if retry:
                self.logger.debug('Retry #{}...'.format(retry))
            retry += 1
            try:
                return self.run_cmd_on_paramiko_client(self._connection.client, cmd, **kwargs)
            except (paramiko.SSHException, EOFError, socket.error, AttributeError) as ex:
                # TODO: we really need to investigate the error conditions and respond appropriately
                if isinstance(ex, AttributeError) and 'open_session' not in str(ex):
                    self.logger.warn(self._ex_str(ex))
                    raise ex

                self.logger.debug('Recoverable? (retry={}) {}'.format(retry, self._ex_str(ex)))

                # before reconnect we are trying to use old ssh connections first
                host_clients = self._connection.host_to_paramiko_clients[self._connection.host]
                clients_to_free = []
                for c in host_clients[:]:
                    try:
                        new_kwargs = kwargs.copy()
                        new_kwargs["timeout"] = 3
                        ret = self.run_cmd_on_paramiko_client(c, cmd, **new_kwargs)
                        self.logger.debug("reuse {} for host {}".format(c, self._connection.host))
                        return ret
                    except Exception:
                        if not c.get_transport() or not c.get_transport().is_alive():
                            clients_to_free.append(c)

                self.logger.debug("cant reuse any of {}, freeing {}".format(host_clients, clients_to_free))
                for c in clients_to_free:
                    try:
                        host_clients.remove(c)
                        c.close()
                    except Exception:
                        # as this can be done in threaded env host_clients may change until we try to remove it
                        pass

            # Why pop? Can't we just reset the conn.client?
                # Connection.cache.pop(self._connection.host, None)

                # The client might have parallel channel's open.
                # self._connection.client.close()
                elapsed_time = (datetime.now() - time_started).total_seconds()
                if elapsed_time >= self.reconnect_timeout:
                    self.logger.info('Timeout after {}s >= {}s. {}'.format(elapsed_time, self.reconnect_timeout,
                                        self._ex_str(ex)))
                    self._connection.decr_active()
                    raise SshException('connection to host {} is disconnected, failed to execute {} after {} seconds.' \
                                       .format(self._connection.host, cmd, elapsed_time))
                delay = min(self.retry_delay, self.reconnect_timeout - elapsed_time)
                # self.logger.debug('disconnect from {}, retry in {} secs'.format(self._connection.host, delay))
                self._connection._need_to_reconnect = True
                time.sleep(delay)
                self.retry_delay *= 2
                try:
                    self._connection.reconnect()
                except:
                    self.logger.info('Reconnect attempt failed. ' + self._ex_str(ex))
            except Exception as e:
                self.logger.warn('Unexpected ' +  self._ex_str(e))
                self._connection.decr_active()
                raise

    def send_signal(self, signal):
        pass

    def kill(self):
        self.logger.info('Killing: <{}> {}'.format(self._connection.host, self._logcmd))
        self._connection.decr_active()
        self.logger.debug("closing channels")
        self.stdout.channel.close()
        self.stderr.channel.close()
        self.logger.debug("channels closed")

    def terminate(self):
        self.kill()

    def poll(self):
        if self.return_code is None and self.stdout.channel.exit_status_ready():
            return self.wait()
        return self.return_code

    def done(self):
        ''' Like poll(), but also returns True if the output/error channels are closed. '''
        def _done():
            return self.return_code is not None or self.stdout.channel.exit_status_ready() \
                   or (self.stdout.channel.eof_received and self.stderr.channel.eof_received)

        is_done = _done()
        if not is_done:
            self._done_checks += 1
            if self._done_checks % 100 == 0:
                # As long running commands may freeze due to kernel crash we send some garbage to check connection
                self.stdout.channel.transport.send_ignore()
                is_done = _done()
        return is_done

    def wait(self):
        if self.return_code is None:
            with self._rc_lock:
                if self.return_code is None:
                    self.return_code = self.stdout.channel.recv_exit_status()
                    self._connection.decr_active()
                    self.logger.info('Exit {} : <{}> [{:.3f}s] {}'.format(self.return_code, self._connection.host,
                            time.time()-self._start_time, self._logcmd))
        return self.return_code

    def communicate(self, inbuf=None):
        if self._communicated:
            return self._communicated
        if inbuf:
            writeall(self.stdin, inbuf)
        self.stdin.close()
        self._communicated = (self.stdout.read(), self.stderr.read())
        self.wait()
        return self._communicated

class LocalPopen(subprocess.Popen):
    DEVNULL = open(os.devnull, 'w')
    _distroless_mode = None
    
    @classmethod
    def _check_distroless(cls):
        """Check once if running in a distroless container (no shell available)."""
        if cls._distroless_mode is None:
            cls._distroless_mode = not os.path.exists('/bin/sh')
            if cls._distroless_mode:
                conn_logger.info('Distroless mode detected: running without shell')
        return cls._distroless_mode
    
    def __init__(self, cmd, *args, **kwargs):
        # TODO: We should get inbuf to work...
        self._logcmd = cmd.partition('\n')[0] # [:30]?
        self.logger = IDAdapter(logging.getLogger('{}.LocalPopen'.format(LOGNAME)))
        self.logger.info('Executing: <{}> {}'.format('localhost-via-popen', self._logcmd))
        self.return_code = None
        
        if self._check_distroless():
            kwargs['shell'] = False
            if isinstance(cmd, str):
                cmd = shlex.split(cmd)
        
        with POPEN_LOCK:
            super(LocalPopen, self).__init__(cmd, bufsize=0, *args, preexec_fn=os.setpgrp, **kwargs) # type: ignore # WTF?
        self._start_time = time.time()
        self._communicated = None
        self._rc_lock = threading.Lock()

    def done(self):
        return self.poll() is not None

    def communicate(self, inbuf=None):
        if self._communicated:
            return self._communicated
        if PY3:
            self._communicated = super(LocalPopen, self).communicate(inbuf, timeout=None)
        else:
            self._communicated = super(LocalPopen, self).communicate(inbuf)
        return self._communicated

    def wait(self, timeout=None):
        # timeout not in used - just to support python3
        if self.return_code is None:
            with self._rc_lock:
                if self.return_code is None:
                    self.return_code = super(LocalPopen, self).wait()
                    if self.return_code < 0:
                        self.return_code = -1       # For compatibility with RemotePopen()
                    self.logger.info('Exit {} : <{}> [{:.3f}s] {}'.format(self.return_code, 'localhost',
                        time.time()-self._start_time, self._logcmd))
        return self.return_code

    def poll(self):
        # Compatibility with RemotePopen is -1 vs. -9/-15.  And wait() handles return_code setting
        return None if super(LocalPopen, self).poll() is None else self.wait()

    def _stop(self, is_kill=False):
        # Subprocess and sudo handling, and compatibility with RemotePopen
        try:
            me = psutil.Process(self.pid)
            procs = me.children(recursive=True)
            self.logger.info('{}({}) + procs: {}'.format('kill' if is_kill else 'terminate', me, procs))
            procs.append(me)
            with POPEN_LOCK:
                subprocess.call(["sudo", "kill", "-9" if is_kill else "-15"] + [str(p.pid) for p in procs],
                        stdout=self.DEVNULL, stderr=subprocess.STDOUT)
            dead, alive = psutil.wait_procs(procs, timeout=0.5)
            if alive:
                # Zombies are already exited but unreaped by their new parent (common in containers
                # without a proper init). They are not alive in any meaningful sense — skip them.
                alive = [p for p in alive if self._proc_status(p) not in (psutil.STATUS_ZOMBIE, None)]
            if alive:
                _, alive = psutil.wait_procs(alive, timeout=2)
            if alive:
                self.logger.warn('{}({}) failed! cmd={}, PROCS: {}'.format('kill' if is_kill else 'terminate', self.pid, self._logcmd, alive))
            if me in dead:
                # Strangely, returncode is 0 after Process() is killed.
                # NOTE: change internal returncode (vs. our return_code) to let wait() do it's side-effects
                self.returncode = -1
        except psutil.NoSuchProcess:
            # If process already dead, that's good too
            # But we still need to close the files (or py3 get's angry...)
            pass

        # safely closing channels
        try:
            self.stdin and self.stdin.close()
        except:
            pass
        try:
            self.stderr and self.stderr.close()
        except:
            pass
        try:
            self.stdout and self.stdout.close()
        except:
            pass
        return

    @staticmethod
    def _proc_status(p):
        try:
            return p.status()
        except psutil.NoSuchProcess:
            return None

    def kill(self):
        self._stop(is_kill=True)

    def terminate(self):
        self._stop(is_kill=False)


class LogEncoder(json.JSONEncoder):
    def default(self, obj): # pylint: disable=method-hidden
        from xlro.core.entities import BaseEntity
        if isinstance(obj, BaseEntity):
            return obj.key()
        if isinstance(obj, Exception):
            return repr(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, (bytes, bytearray)):
            return str(obj, 'utf-8', 'backslashreplace')
        try:
            return json.JSONEncoder.default(self, obj)
        except:
            return str(obj)


class Connection(object):
    cmd_debug_init = False
    cmd_debug_lock = threading.Lock()
    cmd_debug_log: Optional[IO] = None

    @property
    def debug_log(self):
        ''' Return cmd-debug-logfile, if configured '''
        from xlro.core import infra_conf
        from os import environ
        if not Connection.cmd_debug_init:
            with Connection.cmd_debug_lock:
                if not Connection.cmd_debug_init:
                    Connection.cmd_debug_init = True
                    debug_path = environ.get('CMD_DEBUG', infra_conf.root.logging.cmd_debug)
                    conn_logger.debug(f'Logging CMD outputs to {debug_path}')
                    if debug_path:
                        try:
                            Connection.cmd_debug_log = open(debug_path, 'w')
                        except Exception as e:
                            conn_logger.info(f'Failed to open CMD_DEBUG: {debug_path}. {repr(e)}')
        return Connection.cmd_debug_log

    def write_log(self, **kwargs):
        if self.debug_log:
            try:
                with Connection.cmd_debug_lock:
                    json.dump(kwargs, cls=LogEncoder, fp=self.debug_log, indent=2)
                    self.debug_log.write('\n')
                    self.debug_log.flush()
            except Exception as e:
                conn_logger.info(f'write_log() error: {repr(e)}')

    KUBECONFIG = os.environ.get('INFRA_KUBECONFIG', None)
    LOCALHOST_CHECK = False
    # NOTE: if LOCALHOST_CHECK is True, we'll use LocalPopen() vs. RemotePopen() for "remote" calls that happen to be targeted to localhost
    # This wasn't really an optimization, but an attempt to avoid SSH where not needed.  Specifically, this could allow a utility
    # to run locally on a customer machine which didn't even allow SSH, without changing all our code.
    # Unfortunately, despite the efforts in LocalPopen() it is STILL not entirely compatible with RemotePopen(). Monitors give trouble

    MAX_CONNECTIONS = 100
    cache: Dict[str, 'Connection'] = OrderedDict()
    cache_lock = threading.RLock()
    _opts_cache: Dict[str, Dict] = {}
    _ssh_config: paramiko.SSHConfig = None
    _ssh_key_mapping = {
            'user': 'username',
            'password': 'password',
            'identityfile': 'key_filename',
            'hostname': 'hostname',
            'port': 'port',
            'proxyjump': 'proxy',
            'proxycommand': 'proxycommand',
    }

    # See http://docs.paramiko.org/en/stable/api/config.html# for the limited set of directives
    # supported. We don't yet support ProxyCommand
    #
    # NOTE-1: password is a non-standard directive, so you must use "IgnoreUnknown password" at
    # TOP of config if using passwords
    #
    # NOTE-2: Since Connection uses getHostByAddr() on the name first, the Host/Match directives must
    # use match the FQDN/IP and not any alias from /etc/hosts.  For example, if you have in your /etc/hosts:
    #       10.0.1.142               nvme142.acme.com nvme142 n142 jenkins
    # your Match/Host directives should match nvme142.acme.com or 10.0.1.142
    # We should consider adopting more standard/flexible usage, but we ran into a lot of problems
    # early on matching client/target names, since Management uses FQDN.

    # Example content:
    '''

        IgnoreUnkown Password

        Host switch*
            User userXYZ
            Password passwordXYZ

        Host nvclient* nvtarget* 10.0.3.*
            User acme
            IdentityFile ~/.ssh/acme_id_rsa

    '''

    all_transports: List[paramiko.Transport] = []
    host_to_paramiko_clients: Dict[str, List[paramiko.SSHClient]] = defaultdict(list)
    is_local = False

    @classmethod
    def ssh_config(cls, host):
        cfgpaths = infra_conf.root.cluster.sshconfig.split(':')
        if not cfgpaths or cfgpaths[0].lower() == 'nossh':
            conn_logger.info(f'SSH DISABLED: cfgpaths: {cfgpaths}')
            raise Exception('SSH DISABLED')
        if cls._ssh_config is None:
            with cls.cache_lock:
                if cls._ssh_config is None:
                    cls._ssh_config = paramiko.SSHConfig()
                    conn_logger.info(f'CONFIG_PATHS: {cfgpaths}')
                    for cfgpath in cfgpaths:
                        cfgpath = os.path.expanduser(cfgpath)
                        conn_logger.info('SSHConfig - parsing: {}'.format(cfgpath))
                        try:
                            with open(cfgpath) as f:
                                cls._ssh_config.parse(f)
                        except Exception as e:
                            conn_logger.info('Cannot load/parse SSHCONFIG at: {}. {}'.format(cfgpath, repr(e)))
                            # Not aborting, because SSH can work even if no config
                    # For backwards compatibility until all the places using the old mechanism are cleaned up.
                    old_defaults = 'Host *\n    StrictHostKeyChecking no\n    UserKnownHostsFile /dev/null\n'
                    if 'XLRO_SSHUSER' in os.environ:
                        old_defaults += f'    User {os.environ["XLRO_SSHUSER"]}\n'
                    if 'XLRO_SSHKEYFILE' in os.environ:
                        old_defaults += f'    IdentityFile {os.environ["XLRO_SSHKEYFILE"]}\n'
                    cls._ssh_config.parse(StringIO(old_defaults))
        return cls._ssh_config.lookup(host)

    @classmethod
    def ssh_options(cls, name):
        if name not in cls._opts_cache:
            with cls.cache_lock:
                if name not in cls._opts_cache:
                    host_config = cls.ssh_config(name)
                    host_opts = {}
                    for confkey, optkey in cls._ssh_key_mapping.items():
                        if confkey in host_config:
                            value = host_config[confkey]
                            host_opts[optkey] = value if not isinstance(value, list) else value[0]
                        host_opts.setdefault('hostname', name)
                    conn_logger.debug('SSH {} - {}:{}@{}'.format(name,
                        host_opts.get('username', '<self>'),
                        host_opts.get('key_filename', '*passwd*' if 'password' in host_opts else '<none>'),
                        host_opts.get('hostname', name)))
                    cls._opts_cache[name] = host_opts
        return cls._opts_cache[name]

    _localhostname: Optional[str] = None

    @classmethod
    def localhostname(cls):
        # type: () -> str
        if cls._localhostname is None:
            with cls.cache_lock:
                if cls._localhostname is None:
                    cls._localhostname = host_name(socket.gethostname())
        return cls._localhostname

    @classmethod
    def localhosts(cls):
        if cls.LOCALHOST_CHECK:
            conn_logger.info(f'Aliases for localhost: {host_aliases("localhost")} + Aliases for localhostname: {host_aliases(cls.localhostname())}')
            return host_aliases('localhost') + host_aliases(cls.localhostname())
        return ('localhost',)

    def __str__(self):
        return 'Connection to: {}. Transport: {}'.format(self.host,
                'kubectl' if self.KUBECONFIG else ('localhost' if self.is_local else self.client._transport))

    def popen(self, cmd, inbuf=None, get_pty=None, stderr=subprocess.PIPE, **kwargs):
        if inbuf or kwargs:
            conn_logger.debug('POPEN - IN? {}, KWARGS: {}'.format(bool(inbuf), kwargs))

        kwargs.update(infra_conf.serialize_to_dict()['cluster']['popen_kwargs'])
        if not self.is_local:
            return RemotePopen(self, cmd, inbuf=inbuf, get_pty=get_pty, **kwargs)
        # execute_cmd_locally() merges stderr to stdout.  However, that's inconsisent with RemotePopen()
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.pop('reconnect_timeout', None)
        kwargs.pop('socket_timeout', None)
        if self.KUBECONFIG:
            # TODO: could also take --context/--user/etc.  Wait for real usage.
            cmd = f'kubectl --kubeconfig {self.KUBECONFIG} exec {self.host} -- bash -c {shlex.quote(cmd)}'
        p = LocalPopen(cmd, stderr=stderr, shell=True, **kwargs)
        if inbuf and p.stdin:
            writeall(p.stdin, inbuf)
        return p

    def __init__(self, host: str, fail_is_error: bool = True, retries: int = 3, **kwargs: Any) -> None:
        conn_logger.debug('New connection: {}, {}'.format(host, kwargs))
        self.lock = threading.Lock()
        self.ftp_lock = threading.RLock()
        self.active_count = 0
        self.host: str = host_name(host)
        self.is_local = bool(self.KUBECONFIG) or self.host in self.localhosts()
        self._need_to_reconnect = not self.is_local
        self.client: paramiko.SSHClient = None

        if self.is_local:
            return

        conf_opts = self.ssh_options(self.host)
        conf_opts.update(kwargs)
        self.kwargs = conf_opts

        if 'proxy' in self.kwargs:
            self.kwargs['sock'] = paramiko.ProxyCommand('ssh -q -W {0}:22 {1}'.format(self.host, self.kwargs.pop('proxy')))
        elif 'proxycommand' in self.kwargs:
            self.kwargs['sock'] = paramiko.ProxyCommand(self.kwargs.pop('proxycommand'))
        try:
            self.reconnect(retries=retries)
        except Exception as e:  # TODO - replace with specific Exception
            e = Exception('Connect to "{}": ({}) {}'.format(host, type(e), e))
            if fail_is_error:
                conn_logger.error(e)
            else:
                conn_logger.debug(e)
            raise e

        # setting keep alive makes Paramiko client detect ungrateful connection close of the server
        self.client.get_transport().set_keepalive(20)

    @classmethod
    def get_connection(cls, host: str, **kwargs: Any) -> 'Connection':
        try:
            full_host_name = host_name(host)
        except Exception as e:
            conn_logger.info('Error converting name: {} - {}: {}'.format(host, type(e), e))
            raise e
        if full_host_name in cls.cache:
            return cls.cache[full_host_name]
        if len(cls.cache) >= cls.MAX_CONNECTIONS:
            try:
                cls.cache.popitem()[1].close()
            except:
                pass
        with cls.cache_lock:
            if not full_host_name in cls.cache:
                cls.cache[full_host_name] = cls(full_host_name, **kwargs)
        return cls.cache[full_host_name]

    def reconnect(self, retries=1, force=False):
        exception = Exception('Unknown error while trying to reconnect with {} retries'.format(retries))
        self._need_to_reconnect = force or self._need_to_reconnect
        for retry in range(retries):
            if not self._need_to_reconnect:
                return True
            try:
                with self.lock:
                    # Recheck with lock to avoid reconnecting from multiple threads
                    if self._need_to_reconnect:
                        conn_logger.debug('Connect attempt (retry={}) to {}'.format(retry, self.host))
                        new_client = paramiko.SSHClient()
                        new_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                        setattr(new_client, '_hostname', self.host)
                        with warnings.catch_warnings():
                            """
                            parmiko doesn't close socket on failure: https://github.com/paramiko/paramiko/issues/1126
                            """
                            if PY3:
                                warnings.simplefilter('ignore', ResourceWarning)  # type: ignore
                            new_client.connect(**self.kwargs)
                        self.all_transports.append(new_client.get_transport())
                        self.host_to_paramiko_clients[self.host].append(new_client)
                        # make it the newest client
                        self.client = new_client
                        self._need_to_reconnect = False
                        conn_logger.info("new connection number: {} to {} was created".
                                         format(len(self.host_to_paramiko_clients[self.host]) - 1, self.host))
                    return True
            except paramiko.ssh_exception.AuthenticationException as e:
                conn_logger.info(f'Authentication failure - not retrying reconnect: {repr(e)}')
                exception = e
                break
            except Exception as e:
                conn_logger.info('Connect attempt #{} failed. ({}) {}'.format(retry, type(e), e))
                exception = e

        raise exception

    @staticmethod
    def err2exc(execute_result: Tuple[str, str, int], expected: Union[int, Container] = 0) -> str:
        (outbuf, errbuf, code) = execute_result
        if isinstance(expected, int):
            expected = (expected,)
        if code not in expected:
            raise Exception("%d - %s" % (code, errbuf or outbuf))

        return outbuf

    @classmethod
    def execute_on_host(cls, host, cmd, conn_args={}, **kwargs):
        return cls.get_connection(host, **conn_args).execute(cmd, **kwargs)

    @classmethod
    def execute_on_all(cls, hosts, cmd, conn_args={}, **kwargs):
        with ThreadPoolManager() as executor:
            for h in hosts:
                executor.add_task("{}".format(h), Connection.execute_on_host, h, cmd, conn_args, **kwargs)
            executor.start()

        return [(h,) + res.result() for h, res in executor.results.items()]

    @contextmanager
    def _ftp_client(self, existing=None):
        with self.ftp_lock:
            try:
                # TODO: This is for get_folder() but seems wrong.  The ftp-client could fail over time.
                ftp_client = existing
                while ftp_client is None:
                    try:
                        self.reconnect()
                        ftp_client = self.client.open_sftp()
                    except Exception as e:
                        conn_logger.info(f'open_sftp() failed: {repr(e)}')
                        self._need_to_reconnect = True
                yield ftp_client
            except Exception as e:
                conn_logger.info(f'Exception in ftp-context: {repr(e)}')
                raise
            finally:
                if existing is None:
                    ftp_client.close()

    def get_file(self, rpath, lpath):
        if self.host in ('localhost', self.localhostname()):
            execute_cmd_locally(f"cp -r {rpath} {lpath}")
        else:
            with self._ftp_client() as ftp_client:
                conn_logger.debug(f'get_file({rpath} -> {lpath}).')
                ftp_client.get(rpath, lpath)

    def get_folder(self, rpath, lpath, existing_ftp=None):
        with self._ftp_client(existing_ftp) as ftp_client:
            for item_name in ftp_client.listdir(rpath):
                path_to_copy = os.path.join(rpath, item_name)
                new_lpath = os.path.join(lpath, item_name)

                if stat.S_ISDIR(ftp_client.stat(path_to_copy).st_mode):
                    os.makedirs(new_lpath, exist_ok=True)
                    self.get_folder(path_to_copy, new_lpath, ftp_client)
                else:
                    ftp_client.get(path_to_copy, new_lpath)

    def put_file(self, lpath, rpath):
        if self.host in ('localhost', self.localhostname()):
            execute_cmd_locally(f"cp -r {lpath} {rpath}")
        else:
            with self._ftp_client() as ftp_client:
                conn_logger.debug(f'put_file({lpath} -> {rpath}).')
                ftp_client.put(lpath, rpath)

    def get_dir(self: 'Connection', lpath: str, rpath: str) -> Tuple[str, str, int]:
        if self.is_local or self.host == self.localhostname():
            cmd = f"cp -r {rpath} {lpath}"
        else:
            username = f"-o User={self.kwargs['username']}" if "username" in self.kwargs else ""
            key_filename = f"-o IdentityFile={self.kwargs['key_filename']}" if "key_filename" in self.kwargs else ""
            cmd = f"scp -o StrictHostKeyChecking=no {username} {key_filename}  -r {self.host}:{rpath} {lpath}"
        return execute_cmd_locally(cmd)

    def put_dir(self: 'Connection', lpath: str, rpath: str) -> Tuple[str, str, int]:
        # TODO - use SSH-User & SSH-keyFile values from Connection.
        if self.is_local or self.host == self.localhostname():
            cmd = f"cp -r {lpath} {rpath}"
        else:
            username = f"-o User={self.kwargs['username']}" if "username" in self.kwargs else ""
            key_filename = f"-o IdentityFile={self.kwargs['key_filename']}" if "key_filename" in self.kwargs else ""
            cmd = f"scp -o StrictHostKeyChecking=no {username} {key_filename}  -r {lpath} {self.host}:{rpath}"
        return execute_cmd_locally(cmd)

    def scp_between_remotes_via_local(self, src_hostname: str, src_path: str, dst_hostname: str, dst_path: str) -> Tuple[str, str, int]:
        scp_cmd = "scp -o StrictHostKeyChecking=no {0} {1} -3 {2}:{3} {4}:{5}".format(
            "-o User={0}".format(self.kwargs["username"]) if "username" in self.kwargs else "",
            "-o IdentityFile={0}".format(self.kwargs["key_filename"]) if "key_filename" in self.kwargs else "",
            src_hostname, src_path, dst_hostname, dst_path)
        return execute_cmd_locally(scp_cmd)

    def spawn(self, cmd, inbuf=None, get_pty=False, **kwargs):
        process = self.popen(cmd, get_pty=get_pty, **kwargs)
        if inbuf is not None:
            writeall(process.stdin, inbuf)
            process.stdin.close()
            process.stdin.channel.shutdown(1)
        return (process.stdin, process.stdout, process.stderr)

    # NOTE: I'm disabling this for now. I was too nervous to remove the code, but I think it's irrelevant and I'll remove once we prove stability
    def incr_active(self):
        return
        self.lock.acquire()
        self.active_count += 1
        self.lock.release()

    def decr_active(self):
        return
        self.lock.acquire()
        self.active_count -= 1
        self.lock.release()

    def execute(self, cmd, timeout=60, desc=None, success=None, fail=None, err_is_fail=False, decode=True, **kwargs):
        '''
        Execute command and wait for completion (or timeout, if non-zero).
        Reconnect is handled by Popen to retry command. (Use 0 to disable retry)
        '''
        # Refactor to encapsulate functionality in Popen() process. Also, Popen logging clearer.
        # We don't pass timeout to Popen. Rather we do a wait and kill here. Connection timeout should be much shorter.
        start = datetime.now()
        timed_out = False
        process = self.popen(cmd, **kwargs)

        if timeout and not wait_for_it(process.done, poll=0.09, timeout=timeout, quiet=False):
            conn_logger.error('TIMEOUT ({}s) : <{}> {}'.format(timeout, self.host, cmd))
            timed_out = True
            process.kill()
        outbuf, errbuf = process.communicate()
        code = process.wait()

        end = datetime.now()
        self.write_log(HOST=self.host, CMD=cmd, RET_CODE=code, OUT=outbuf, ERR=errbuf, START=start, END=end, ELAPSED=end-start,
                TIMEOUT=timeout, TIMED_OUT=timed_out)

        # Needs to be inside here (vs. err2exc) because lots of conveniences call execute()
        # OTOH, can't default to success=0, because that would be a breaking change in behavior
        success = (success,) if isinstance(success, int) else success
        fail = (fail,) if isinstance(fail, int) else fail
        if (success and code not in success) or (fail and code in fail) \
                or (err_is_fail and errbuf):
            desc = desc or cmd.partition('\n')[0]
            raise CmdException('{}: {} FAILED ({})! {}'.format(self.host, desc, code, errbuf or outbuf))

        if PY3 and decode:
            outbuf = outbuf.decode('utf-8') if outbuf else ""
            errbuf = errbuf.decode('utf-8') if errbuf else ""
        return outbuf, errbuf, code

    def tolerant_exec(self, cmd, timeout=10, retries=1, delay=1, log_level='info', expected=None, expected_out=None, **kwargs):
        if not expected:
            expected = [0]
        exception = Exception('Unkown error while attempting to run a tolerant_exc cmd {} with {} retries'
                              .format(cmd, retries))
        for retry in range(retries):
            try:
                if retry:
                    time.sleep(delay)
                out, err, code = self.execute(cmd, timeout=timeout, **kwargs)
                if code not in expected:
                    raise Exception('Exit code: {}. {}'.format(code, (err or out)[:150]))
                if expected_out and not re.search(expected_out, out):
                    raise Exception(f'Pattern {expected_out} not found in Output: {out}')
                return out, err, code
            except Exception as e:
                getattr(conn_logger, log_level)('CMD <{}> {} failed (#{}): ({}) {}'.format(self.host, cmd, retry + 1, type(e), e))
                exception = e

        raise exception

    def close(self):
        conn_logger.info('Closing connection to: {}'.format(self.host))
        self.__class__.cache.pop(self.host, None)
        # NOTE: I don't think this was working anyway, and I don't even think anyone calls close()
        # But if so, the correct thing is to check here, but otherwise set a flag for the last decr_active()
        if not self.active_count:
            # TODO: count should be on client vs. connection?
            self.client.close()

    @classmethod
    def cleanup(cls):
        list(map(lambda c: c.close(), cls.all_transports))

    @staticmethod
    def read_bytes_from_socket(sock, n_bytes):
        all_data = bytes()
        left_to_read = n_bytes
        while len(all_data) != n_bytes:
            data = sock.read(left_to_read)
            if not data:
                break
            left_to_read -= len(data)
            all_data += data
        assert len(all_data) == n_bytes, f"Number of read data:{len(all_data)} not equal to requested bytes {n_bytes}"
        return all_data


class ExecThread(threading.Thread):
    counter = 0

    @classmethod
    def count(cls):
        cls.counter += 1
        return cls.counter

    def __init__(self, conn, cmd, name=None):
        threading.Thread.__init__(self)
        self.conn = conn
        self.cmd = cmd
        self.id = name if name else self.count()

    def run(self):
        self.out, self.err, self.code = self.conn.execute(self.cmd)


@contextmanager
def temp_dir(hostname, pattern=None, timeout=60):
    if pattern and not 'XX' in pattern:
        pattern += '.XXXXXXXXXX'
    out, err, code = Connection.execute_on_host(hostname, 'mktemp -d {}'.format(pattern if pattern else ''),
                                                timeout=timeout)

    path = out.strip()
    if code != 0 or not path:
        raise Exception('Failed to create tempdir on {} ({}). {}'.format(hostname, code, err))
    try:
        yield path
    finally:
        Connection.execute_on_host(hostname, 'sudo rm -rf {}'.format(path), timeout=timeout)


# Admittedly fugly. Cache src-per-session on host? Move to RPYC?
# Note - the tempfile complexity (vs. python - <<!) is to enable stdin within the python script
_RPY_SOURCES = {}
_RPY_TERMINATOR = '__EndOfScript__'
_RPY_CMD_FORMAT = 'F=$(mktemp -t nv-XXXXXX.py); cat >$F <<\\' + _RPY_TERMINATOR \
                  + '; python -u $F {} ; rm -f $F\n{}\n' + _RPY_TERMINATOR + '\n\n'


def remote_python_command(module, cmd_args=''):
    if module not in _RPY_SOURCES:
        with open(inspect.getfile(module).rpartition('.')[0] + '.py', 'r') as fp:
            _RPY_SOURCES[module] = fp.read()
    return _RPY_CMD_FORMAT.format(cmd_args, _RPY_SOURCES[module])


def main():
    import sys
    import argparse
    from concurrent.futures import ThreadPoolExecutor, wait
    from collections import defaultdict
    from xlro.core.util.cli_util import CLIArgumentParser, cli_print

    parser = CLIArgumentParser()
    parser.add_argument('-p', '--parallel-count', type=int, default=1)
    parser.add_argument('-r', '--repeat', type=int, default=1)
    parser.add_argument('-t', '--timeout', type=int, default=0)
    parser.add_argument('-d', '--delay', type=int, default=0)
    parser.add_argument('-x', '--execute', action='store_true')
    parser.add_argument('-l', '--linebuffered', action='store_true')
    parser.add_argument('-k', '--kubeconfig')
    parser.add_argument('-a', '--auth-only', action='store_true')
    parser.add_argument('-c', '--connect-opt', type=str, action='append', default=[])

    parser.add_argument('host')
    parser.add_argument('cmd', nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.kubeconfig:
        Connection.KUBECONFIG = args.kubeconfig

    logger = logging.getLogger(sys.argv[0])

    kwargs = {}
    for opt in args.connect_opt:
        k, _, v = opt.partition('=')
        kwargs[k] = v if not v.isdigit() else int(v)
    logger.info('Connection args: {}'.format(kwargs))
    try:
        conn = Connection.get_connection(args.host, **kwargs)
        if args.auth_only:
            cli_print('Connect to {} ({}) OK'.format(conn.host, args.host))
            conn.close()
            sys.exit(0)
    except Exception as e:
        cli_print('Connect to {} FAIL: {}'.format(args.host, repr(e)))
        conf = Connection.ssh_options(args.host)
        conf.update(kwargs)
        cli_print('SSH-CONF: {}'.format(conf))
        sys.exit(1)

    cmd = ' '.join(args.cmd)
    results: Dict[Any, int] = defaultdict(int)
    starts = {}
    exec_times = []
    elapsed_times = []
    start_clock = time.time()

    for r in range(args.repeat):
        if r > 0 and args.delay:
            time.sleep(args.delay)

        if args.execute:
            if args.linebuffered:
                process = conn.popen(cmd)
                if args.timeout:
                    p_timer = Timer(args.timeout, process.kill)
                    p_timer.start()
                if process.stdin:
                    process.stdin.close()
                    process.stdin.channel.shutdown(1)
                cli_print('OUT:')
                line = process.stdout.readline()
                while line:
                    cli_print(line.strip())
                    line = process.stdout.readline()
                err = str(process.stderr.read())
                code = process.wait()
                if args.timeout:
                    p_timer.cancel()
            else:
                out, err, code = conn.execute(cmd, timeout=args.timeout)
                cli_print('OUT:' + out.strip())
            if err:
                cli_print('ERR:' + err.strip())
            cli_print('CODE:' + str(code))
            continue

        def spawn(p):
            cli_print('Spawning #{}.{}'.format(r, p))
            starts[p] = time.time()
            return conn.popen(cmd, reconnect_timeout=args.timeout)

        def collect(process, p):
            out, err = process.communicate()
            code = process.wait()
            results[code] += 1
            now = time.time()
            elapsed_times.append(now - start_clock - (r* args.delay))
            exec_times.append(now-starts[p])
            cli_print('EXECUTION #{}.{} -> {}'.format(r, p, code))
            cli_print('OUT:' + (str(out).strip() if out else '<None>'))
            if err:
                cli_print('ERR:' + str(err).strip())

        with ThreadPoolExecutor(args.parallel_count) as executor:
            processes = executor.map(spawn, range(args.parallel_count))
            executor.map(collect, processes, range(args.parallel_count))

    cli_print('RESULTS:' + str(results))
    if not args.execute:
        cli_print('EXEC TIMES: min: {:.2f} max: {:.2f} avg: {:.2f}'.format(min(exec_times), max(exec_times), old_div(sum(exec_times),len(exec_times))))
        cli_print('ELAPSED TIMES: min: {:.2f} max: {:.2f} avg: {:.2f}'.format(min(elapsed_times), max(elapsed_times), old_div(sum(elapsed_times),len(elapsed_times))))

if __name__ == '__main__':
    main()
