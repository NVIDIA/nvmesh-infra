# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from collections import defaultdict
from typing import Set, Dict

from xlro.core.util.general_utils import host_name
from xlro.tools.nvmesh_diag.util import MsgLvl, DiagArgs, ClusterDiagnostic
from xlro.core.entities import Client, Target, Cluster
from xlro.core.entities.drive import DriveStatus
from xlro.core.sdk.ConnectionManager import ManagementLoginError, ManagementTimeout

class ClusterCheck(ClusterDiagnostic):
    description = "Cluster Check"
    mgmt_version: str

    def discover(self):
        self.diag_info.manager = None
        self.diag_info.nodes = None
        mgr = DiagArgs.manager
        nodes = DiagArgs.nodes
        try:
            if mgr:
                self.add_message(f'Connected to Manager at {mgr.live_host}. Version: {mgr.version}, API: {mgr.api_version}')
                self.diag_info.manager = mgr
            self.diag_info.nodes = nodes
        except (ManagementLoginError, ManagementTimeout) as e:
            self.add_message(f'{e.args[0]}.', msg_level=MsgLvl.ERROR)
        except Exception as e:
            self.add_message(f'Cannot connect to management server on {mgr.host}. {repr(e)}', msg_level=MsgLvl.ERROR)

    def validate(self):
        expected = self.expectations
        mgr = self.diag_info.manager
        nodes = self.diag_info.nodes
        mver = ''
        exp_mver = ''
        exp_num_mgmt_nodes = 0
        exp_num_mongo_nodes = 0
        cluster = None

        # Check if this is a UM node
        is_um_node = self.is_um()
        
        if mgr:
            mver = mgr.version.partition('-')[0]
            exp_mver = expected.get('mgr-version')
            exp_num_mgmt_nodes = expected.get('num-mgmt-nodes')
            exp_num_mongo_nodes = expected.get('num-mongo-nodes')
            cluster = Cluster.sdk_get()[0]
            exp_av_space = expected.get('available-space')
            exp_us_space = expected.get('used-space')
            targets = [target for target in mgr.targets if any(node.name == target.name for node in nodes)]
            if exp_mver and exp_mver != mver:
                self.add_message(f'Management version mismatch. Expected: {exp_mver}, Got: {mver}',
                                 msg_level=MsgLvl.ERROR)
            if exp_num_mgmt_nodes is not None and int(exp_num_mgmt_nodes) != len(mgr.mgmt_cluster):
                self.add_message(f'Management nodes mismatch. Expected: {exp_num_mgmt_nodes}, Got: {mgr.mgmt_cluster}',
                                 msg_level=MsgLvl.ERROR)
            if exp_num_mongo_nodes is not None and int(exp_num_mongo_nodes) != len(mgr.mongo_cluster):
                self.add_message(
                    f'Management nodes mismatch. Expected: {exp_num_mongo_nodes}, Got: {mgr.mongo_cluster}',
                    msg_level=MsgLvl.ERROR)
            if exp_av_space and exp_av_space != cluster.freeSpace:
                self.add_message(f'Free space mismatch. Expected {exp_av_space}, Got: {cluster.freeSpace}')
            if exp_us_space and exp_us_space != cluster.allocatedSpace:
                self.add_message(f'Used space mismatch. Expected {exp_us_space}, Got: {cluster.allocatedSpace}')
        else:
            # If no mgr we assume that all nodes are targets (and clients)
            targets = [Target.instance(mgmt="", name=node.name) for node in nodes]

        drive_count = 0
        ports_by_protocol: Dict[str, Set] = defaultdict(set)
        exp_tver = expected.get('target-version')
        exp_cver = expected.get('client-version')

        # Skip target checks for UM nodes
        if not is_um_node:
            self.add_message(f'Checking {len(targets)} targets (health, version and drives)')
            for t in targets:
                self.logger.debug(f'Checking target {t._name} - {t.version}')
                if t.health != Target.HEALTH.HEALTHY:
                    self.add_message(f'Target: {t._name} - {t.health}', msg_level=MsgLvl.ERROR)
                tver = t.version.partition('-')[0]
                if exp_tver and exp_tver != tver:
                    self.add_message(f'Target: {t._name} - version mismatch. Expected: {exp_tver}, Got: {tver}', msg_level=MsgLvl.ERROR)
                elif tver != mver and mgr:
                    self.add_message(f'Target: {t._name} - version mismatch. Target: {tver}, Mgmt: {mver}', msg_level=MsgLvl.WARNING)
                drive_count += len(t.drives)
                for d in t.drives:
                    if d.status != DriveStatus.OK or d.health != Target.HEALTH.HEALTHY or d.evicted:
                        self.add_message(
                            f'Drive: {t._name}/{d.name} status: {d.status}, health: {d.health}, evicted: {d.evicted}',
                            MsgLvl.WARNING)

                if not t.node.nics:
                    self.add_message(f'No NICs found on Target: {t._name}', msg_level=MsgLvl.ERROR)
                else:
                    for n in t.node.nics:
                        for p in n.ports:
                            ports_by_protocol[p.protocol].add(f'{t._name}/{p.name}')

        if mgr:
            clients = [client for client in mgr.clients if any(node.name == client.name for node in nodes)]
        else:
            clients = [Client.instance(name=node.name) for node in nodes]
        self.add_message(f'Checking {len(clients)} clients (health and version)')
        for c in clients:
            self.logger.debug(f'Checking client {c._name} - {c.version}')
            if c.health != Client.HEALTH.HEALTHY:
                self.add_message(f'Client: {c._name} - {c.health}', msg_level=MsgLvl.ERROR)
            cver = c.version.partition('-')[0]
            if exp_cver and exp_cver != cver:
                self.add_message(f'Client: {c._name} - version mismatch. Expected: {exp_tver}, Got: {cver}', msg_level=MsgLvl.ERROR)
            elif cver != mver and mgr:
                self.add_message(f'Client: {c._name} - version mismatch. Client: {cver}, Mgmt: {mver}', msg_level=MsgLvl.WARNING)
            if not c.node.nics:
                self.add_message(f'No NICs found on Client: {c._name}', msg_level=MsgLvl.ERROR)
            else:
                for n in c.node.nics:
                    for p in n.ports:
                        ports_by_protocol[p.protocol].add(f'{c._name}/{p.name}')

        self.add_message(f'Verifying port protocols all same')
        if len(ports_by_protocol) > 1:
            nic_msg = ', '.join([f'{len(ports_by_protocol[proto])} are {proto}' for proto in ports_by_protocol])
            self.add_message(f'Port protocol mismatch. {nic_msg}', msg_level=MsgLvl.ERROR)
            for proto, ports in ports_by_protocol.items():
                self.add_message(f'{proto} Ports {", ".join(ports)}')

        if self.diag_max_level < logging.WARNING:
            self.add_message(f'{len(targets)} targets, {drive_count} drives, {len(clients)} clients - healthy, Protocol: {next(iter(ports_by_protocol.keys()))}', MsgLvl.SUCCESS)

        try:
            if mgr:
                mongos = mgr.connection.request('get', '/mongoDB/all')[1][0]['members']
                state2mongo: Dict[int, list] = defaultdict(list)
                for m in mongos:
                    state2mongo[m['health']].append(m['name'])
                if len(state2mongo[1]) == len(mongos):
                    self.add_message(f'{len(mongos)} MongoDB server(s) healthy.', MsgLvl.SUCCESS)
                else:
                    state_msg = ', '.join([f'Healthy: {bool(health)} on server(s): {names}' for health, names in state2mongo.items()])
                    self.add_message(f'MongoDB not healthy! {state_msg}', MsgLvl.WARNING)
        except:
            self.add_message(f'Failed to check Mongo status.', msg_level=MsgLvl.ERROR)
