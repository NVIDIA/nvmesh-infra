#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from typing import *
import yaml
import signal
import time
import sys
import os
import logging
import inspect
from importlib import import_module
import typing
import json

from xlro.core import infra_conf
import xlro.core.util.monitor as monitor
from xlro.core.util.actions import EventKeyFilter, Action, RemoteCmdAction
from xlro.core.entities import Manager

logger = logging.getLogger(sys.argv[0].upper())
run_mons: typing.List[monitor.Monitor] = []

def cname_to_class(cname, base=None, pkgs=[]):
    for fullname in [cname] + [f'{p}.{cname}' for p in pkgs]:
        modname, _, classname = fullname.rpartition('.')
        try:
            c = getattr(import_module(modname), classname)
            assert inspect.isclass(c)
            assert base is None or issubclass(c, base)
            return c
        except Exception as e:
            pass
    raise Exception(f'{cname} not found. (base={base}, pkgs={pkgs})')


def on_signal(_signo, _stack_frame):
    ret_code = 0
    for m in run_mons:
        try:
            m.stop()
        except Exception:
            logger.exception("couldn't stop {}".format(m))
            ret_code = 1
    sys.exit(ret_code)


class PrintMonitor(monitor.MultiMonitor):
    def __init__(self, *args, **kwargs):
        super(PrintMonitor, self).__init__(*args, **kwargs)

    def on_event(self, event):
        super(PrintMonitor, self).on_event(event)
        print(self.format_event(event))

HOSTSET2FUNC = {
    'managements': lambda: Manager.get_manager().mgmt_cluster,
    'targets': lambda: [t.name for t in Manager.get_manager().targets],
    'clientsonly': lambda: [c.name for c in Manager.get_manager().clients if c.name not in [t.name for t in Manager.get_manager().targets]]
}

def mspec_to_multi_monitor(mspecs: dict, hosts: Optional[str] = None, manager: Optional[Manager] = None, monitor_names: Iterable[str] = None, logdir: str = '.', variables: Dict = None, **multi_kwargs) -> monitor.MultiMonitor:
    return monitor.MultiMonitor(monitors=mspec_to_monitors(mspecs, hosts, manager, monitor_names, logdir, variables), **multi_kwargs)

def mspec_to_monitors(mspecs: dict, hosts: Optional[str] = None, manager: Optional[Manager] = None, monitor_names: Iterable[str] = None, logdir: str = '.', variables: Dict = None) -> List[monitor.Monitor]:
    mons: List[monitor.Monitor] = []
    if hosts:
        hostlist = {h.strip() for h in hosts.split(',')}
    elif manager:
        # We can't monitor scale setups (no ssh) and we don't really have a flag on host
        hostlist = {n.name for n in manager.get_all_subsystems() if not n.name.startswith('scale-')}
    else:
        hostlist = {'localhost'}
    if logdir:
        os.makedirs(logdir, exist_ok=True)

    # xlro.infra refs OK because failure to import will be silently ignored in cname_to_class()
    mon_packages = ['xlro.core.util.monitor', 'xlro.infra.util.io_monitors', 'xlro.infra.util.watchers', 'xlro.infra.util.pod_monitor']
    for mname in monitor_names or mspecs.keys():
        spec = mspecs[mname]
        if not isinstance(spec, dict):
            logger.warning(f'SPEC: for {mname} is not dict!')

        m_class = cname_to_class(spec.pop('class', 'PidMonitor'), base=monitor.Monitor, pkgs=mon_packages)
        hostsets = set()
        for hostset in spec.pop('hosts', []):
            hs = hostset.lower()
            if hs in HOSTSET2FUNC:
                hostsets.update(HOSTSET2FUNC[hs]())
            else:
                hostsets.add(hostset)

        monhosts = hostlist.intersection(hostsets) if hostsets else hostlist
        hspecs = spec.pop('handlers', [])
        aspecs = spec.pop('actions', [])
        for host in monhosts:
            cp_mspec = spec.copy()
            cp_mspec['host'] = host
            cp_mspec['name'] = cp_mspec['name'].format(**cp_mspec) if 'name' in cp_mspec else mname
            if 'logpath' in cp_mspec:
                cp_mspec['logpath'] = os.path.join(logdir, cp_mspec['logpath'].format(**cp_mspec))

            nolog = cp_mspec.pop('nolog', None)
            if nolog:
                if nolog == 'events':
                    nologopt = monitor.Monitor.LoggingOptions.LOG_EVENT_TO_FILE
                else:
                    nologopt = monitor.Monitor.LoggingOptions.LOG_MESSAGE_TO_FILE

                cp_mspec['log_option'] = monitor.Monitor.DEFAULT_LOGGING_OPTION & ~nologopt

            cmd = cp_mspec.get('cmd', '')
            if cmd and cmd[0] == '@':
                cp_mspec['cmd'] = cmd[1:].format(**variables)

            mon = m_class(**cp_mspec)
            mons.append(mon)

            # Build Handlers
            for hspec in hspecs:
                cp_hspec = hspec.copy()
                h_class = cname_to_class(cp_hspec.pop('class', 'PatternHandler'),
                                         base=monitor.MsgHandler, pkgs=mon_packages)
                mon.add_msg_handler(h_class(**cp_hspec))

            # Build Actions (triggers)
            for aspec in aspecs:
                cp_aspec = aspec.copy()
                a_class = cname_to_class(cp_aspec.pop('class', 'RemoteCmdAction'),
                                         base=Action, pkgs=['xlro.core.util.actions'])
                if 'match' in cp_aspec:
                    matchd = cp_aspec.pop('match', None)
                    cp_aspec['condition'] = EventKeyFilter([], **matchd)

                mon.add_action(a_class(**cp_aspec))

    return mons


def yaml_file_to_mspec(yaml_file):
    with open(yaml_file, 'r') as stream:
        try:
            yaml_spec = yaml.safe_load(stream)
            return yaml_spec['monitors']
        except yaml.YAMLError:
            logger.exception("couldn't parse yaml {}".format(yaml_file))
            raise


def yaml_file_to_monitor(yaml_file, **kwargs):
    return mspec_to_multi_monitor(yaml_file_to_mspec(yaml_file), **kwargs)


if __name__ == '__main__':
    from xlro.core.util.cli_util import CLIArgumentParser

    parser = CLIArgumentParser()
    parser.add_argument("--hosts", help='Comma-separated default host list for monitors.  Default is use nodes of Manager (see -M)')
    parser.add_argument("--monitors", help='Comma-separated list of monitor names to run.  Default is all in YAML')
    parser.add_argument("--variables", help='Comma-separated key=value list for resolving variables in cmd string. example: --variables key1=value1,key2=value2,...,keyN=valueN')
    parser.add_argument("--logdir", help='Directory for relative logpaths', default='.')
    parser.add_argument('monitor_yaml', help='YAML descriptor for one or more monitors')
    args = parser.parse_args()

    # Should probably be in cli_util, or drop and have user use -c logging.logdir?
    infra_conf.assign_path('logging.logdir', args.logdir)

    # TODO: IFF monitor via yaml gains traction, we should consider a more flexible hosts spec.
    # For example, calling args.hosts.format(manager=M) would allow '{manager.targets[0]}' to mean first target.
    # Not adding until we have actual use-cases
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    vargs = {k: v for k, _, v in (kv.partition('=') for kv in args.variables.split(','))} if args.variables else None
    mspec = yaml_file_to_mspec(args.monitor_yaml)
    monitor_names = {name.strip() for name in args.monitors.split(',')} if args.monitors else mspec.keys()
    run_mons.extend(mspec_to_monitors(mspec, hosts=args.hosts, manager=args.manager, monitor_names=monitor_names, logdir=args.logdir, variables=vargs))
    mmon = PrintMonitor(run_mons)
    mmon.start()
    while None in mmon.poll_all():
        time.sleep(1)
    logger.info('RESULTS: {}'.format(mmon.wait_all()))
    mmon.stop()
