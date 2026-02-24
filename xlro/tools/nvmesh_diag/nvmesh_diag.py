#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import re
import logging
import json
from argparse import FileType

import yaml

from collections import defaultdict
from xlro.core import infra_conf
from xlro.core.entities import Node, Host, Manager
from xlro.core.sdk.ConnectionManager import ConnectionManagerError
from xlro.core.util.common import get_path
from xlro.core.util.general_utils import run_local
from xlro.core.util.ssh import Connection
from xlro.core.util.cli_util import CLIArgumentParser, EntitiesArg
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl, FixEndException, DiagManager, DiagArgs, \
    ClusterDiagnostic, StopDiag, StopAll, StopNode, ExcludedDiag, DiagGroup


def main():
    # Although this can be problematic for monitors, it should work fine in our case
    # And it's necessary to execute localhost stuff when no ssh, without code changes
    logger = logging.getLogger('nvmesh_diag')
    Connection.LOCALHOST_CHECK = True
    DEFAULT_MODULES = ['post_install']

    parser = CLIArgumentParser(user_style=True)
    parser.add_argument('-p', '--password', help='REST password')
    parser.add_argument('-n', '--nodes', type=EntitiesArg(Node), default=[])
    parser.add_argument('-o', '--output', type=FileType('w'), default='-', help='Output logfile (default:\'-\', aka stdout)')
    parser.add_argument('-j', '--json', action='store_true', help='Output in JSON format (without this option, output in plain text format)')
    parser.add_argument('-v', '--verbose', action='count', default=2, help="Increase verbosity (multiple allowed).")
    parser.add_argument('-q', '--quiet', action='count', default=0, help="Reduce verbosity (multiple allowed).")
    parser.add_argument('--nocolor', action='store_true', default=os.environ.get('NVMESH_DIAG_NOCOLOR', '').lower() == 'true', help="Print without colors")
    parser.add_argument('--details', action='store_true', help="output details")
    parser.add_argument('-V', '--version', action='store_true')
    parser.add_argument('-e', '--extensions', default='~/.nvmesh/diag',
            help="Path(s) to directories of extentions. (default: ~/.nvmesh/diag)")
    parser.add_argument('-x', '--exclude', action='append', default=[], help="Exclude a specific module")
    parser.add_argument('-l', '--list-modules', action='store_true', help="List modules")
    parser.add_argument('--local', action='store_true', help="Check only this node (default: false, which checks all nodes in the cluster)")
    parser.add_argument('--expectations', help="Path to expectations yaml file",
                        default=get_path(f'{os.path.dirname(__file__)}/expectations.yaml', 'expectations.yaml'))
    # parser.add_argument('-s', '--set-parameters', action='store_true', help="Set the recommended parameters where possible")
    # parser.add_argument('-l', '--run-log-collection', action='store_true',
                        # help="Run the NVMesh log collector together with the NVMesh diag tool.")
    # parser.add_argument('-i', '--software-inventory', action='store_true',
                        # help="include a list of all installed software packages on this server. RPMs for RH/CentOS and DEBs for Ubuntu.")
    parser.add_argument('modules', type=str, nargs='*')

    try:
        parser.parse_args(namespace=DiagArgs)
    except Exception as e:
        print(repr(e))
        sys.exit(2)

    if DiagArgs.version:
        print(f'Infra-Version: {infra_conf.root.src_version}')
        sys.exit(0)

    console_levels = [1000, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG, 0]
    DiagArgs.console_level = console_levels[min(max(DiagArgs.verbose - DiagArgs.quiet, 0), len(console_levels)-1)]

    nodes = DiagArgs.nodes
    mgr = DiagArgs.manager

    if DiagArgs.list_modules:
        if DiagArgs.modules:
            for mod_name in DiagArgs.modules:
                try:
                    dm = DiagManager.diag_instance(mod_name, Node.instance(name='localhost'))
                except Exception as e:
                    print(str(e))
                    sys.exit(1)
                if isinstance(dm, DiagGroup) and dm.is_set:
                    for sdm in dm.dm_instances:
                        print(f'{sdm._cname():<25}{sdm.description:>25}')
                else:
                    print(f'{dm._cname():<25}{dm.description:>25}')
        else:
            modules, group_modules = DiagManager.list_modules()
            if modules:
                print('Modules:')
                for module in modules:
                    print(f' {module}')
            if group_modules:
                print()
                print('Set Modules:')
                for group2mods in group_modules:
                    for group, mods in group2mods.items():
                        print(f' {group}')
                        for mod in mods:
                            print(f'  {mod}')
        return

    if not DiagArgs.modules:
        DiagArgs.modules = DEFAULT_MODULES

    if DiagArgs.local: # Check only current node
        if not nodes:
            lname = run_local('hostname -f')[0].split()[0] # Get the full name of the node
            nodes = [Node.instance(name=lname)]
    else: # Default: check all nodes in the cluster
        if not mgr:
            # Try to get manager from nvmesh.conf
            try:
                if not nodes:
                    # Try to get manager from nvmesh.conf located localhost
                    mgr = Manager.instance(endpoints=Manager.default_endpoints())
                else:
                    # Try to get manager from nvmesh.conf located at one of the given node
                    node = nodes[0]
                    def_mgr = Host.instance(name=node.name).nvmeshconf()['_REST_SERVERS']
                    mgr = Manager.instance(endpoints=[def_mgr.split(':')[0]])
                DiagArgs.manager = mgr
            except Exception as e:
                logger.info(f'Exception trying to determine Manager from config: {repr(e)}')
                # It's legit to have no manager - can still run non-cluster checks on nodes

        if mgr and (DiagArgs.user or DiagArgs.password):
            # Set the credentials, but don't force connection. If required, module/cluster will handle.
            # This is messy, but I REALLY don't want to break CLI or anything else
            # TODO: move the options to cli_utils and add setting creds to Manager
            def_user, def_passwd = mgr.creds
            mgr._creds = (DiagArgs.user or def_user, DiagArgs.password or def_passwd)

        if not mgr:
            if not nodes:
                print('You must specify a Manager or Nodes or be running on a configured Node.', file=sys.stderr)
                sys.exit(1)
            else:
                print('No manager specified, continue with nodes checks.')
        errmsg = ''
        try:
            # Connecting to management
            if mgr:
                mgr.connect()
            if not nodes:
                nodes = [c.node for c in mgr.clients]
                logger.info(f'Got nodes from {mgr.host}: {[n.name for n in nodes]}')
        except ConnectionManagerError as ce:
            # Verifying the manager user param exists in nvmesh.conf file
            errmsg = ce.args[0]
            mgr_host = mgr.host
            try:
                if mgr_host in Host.instance(name=mgr_host).nvmeshconf()['_REST_SERVERS']:
                    print(f'{errmsg}, Given manager param {mgr_host} exists in nvmesh.conf file but no connection to manager. continue with nodes checks.',
                        file=sys.stderr)
                    if not nodes:
                        print(f'Adding manager for nodes checks.')
                        nodes.append(Node.instance(name=mgr_host))
                else:
                    print(f'Can not find info about the given manager: {mgr_host} - nvmesh.conf file does not exist or rest_servers value is empty or incorrect.')
                    if not nodes:
                        # in case no nodes and no mgr for nodes checks- exit
                        exit(1)
                mgr = None
            except Exception as e:
                if not nodes:
                    # in case no nodes and no mgr for nodes checks- exit
                    exit(1)
                mgr_err = e.args[0]
                print(f'{mgr_err}, Could not find info about the given manager: {mgr_host}. continue with nodes checks.', file=sys.stderr)
                mgr = None

        except Exception as e:
            errmsg = repr(e)
        if not nodes:
            def_node = DiagArgs.manager.host if DiagArgs.manager else 'localhost'
            print(f'{errmsg} Using {def_node} for node checks.')
            nodes = [Node.instance(name=def_node)]

    with open(DiagArgs.expectations, 'r') as fp:
        DiagModule._class2expectations = yaml.safe_load(fp.read())['expect']

    DiagArgs.nodes = nodes
    DiagArgs.manager = mgr

    stop = False
    json_data: Dict[str, Dict[str, Any]] = defaultdict(dict) # nodename->mod_name->{status,messages}
    for node in nodes:
        if stop:
            break
        for mod_name in DiagArgs.modules:
            try:
                dm = DiagManager.diag_instance(mod_name, node)
            except:
                print(f'Failed loading {mod_name} for node {node.name}. Skipping...', file=sys.stderr)
                continue

            if isinstance(dm, ExcludedDiag) or (isinstance(dm, ClusterDiagnostic) and node != nodes[0]):
                continue
            dm.add_message(f'Running {dm.description} on {node.name}...')
            try:
                dm.run_phase('discover')
                dm.run_phase('validate')
                if DiagArgs.details:
                    dm.run_phase('details')
                if DiagArgs.json:
                    json_data[node.name][mod_name] = dm.json_data
            except StopNode:
                break
            except StopAll:
                stop = True
                break
            except (StopDiag, FixEndException):
                continue
            except Exception as e:
                logger.exception(f'Exception while running "{mod_name}"')
                dm.add_message(f'Runtime error: {repr(e)}', MsgLvl.ERROR)

    if DiagArgs.json:
        json.dump(json_data, DiagArgs.output, indent=2)

    if DiagArgs.output is not sys.stdout:
        DiagArgs.output.close()

    return 0 if DiagModule.diag_max_level < logging.ERROR else 1


if __name__ == "__main__":
    sys.exit(main())
