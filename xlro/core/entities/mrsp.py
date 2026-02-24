#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from typing import List
from xlro.core.entities.base import *
from xlro.core.entities import Host, Process
import json
import configparser
import re


@entity(sourcetypes=[SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2])
class TPV(BaseEntity):
    id : str = PropertySpec(str, key=True)
    size : int = PropertySpec(int)
    namespace : int = PropertySpec(int)
    nvmesh_bdev : 'NVMESHBdev' = PropertySpec('NVMESHBdev')
    state : str = PropertySpec(str)

    TPV_STATE_REMOTE = 'REMOTE'
    TPV_STATE_LOCAL = 'LOCAL'
    TPV_STATE_MAKING_LOCAL = 'MAKING_LOCAL'
    TPV_STATE_RELEASING = 'RELEASING'

    @prop_loader([SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2], ['state'])
    def _load_state(self, source=None):
        controller = MRSP.instance().controller1 if source == SourceTypes.MRSP_RPC_CONTROLLER1\
            else MRSP.instance().controller2

        output = controller.send_rpc_request('nvmesh_list_bdevs')[0]
        j_output = json.loads(output)

        for dev in j_output:
            if dev['name'] == self.id:
                return {'state': dev['state']}


@entity(sourcetypes=[SourceTypes.MRSP_CONFIG_FILE, SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2])
class Subsystem(BaseEntity):
    id : str = PropertySpec(str, key=True)
    ip : str = PropertySpec(str, key=True)
    port : int = PropertySpec(int)
    sn : str = PropertySpec(str)
    tpvs : List[TPV] = PropertySpec([TPV])
    connected_hosts : List[Host] = PropertySpec([Host])

    @prop_loader([SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2], ['connected_hosts'])
    def _load_state(self, source=None):
        controller = MRSP.instance().controller1 if source == SourceTypes.MRSP_RPC_CONTROLLER1\
            else MRSP.instance().controller2

        output = controller.send_rpc_request('get_nvmf_connections')[0]
        j_output = json.loads(output)

        ret = []
        for s in [s for s in j_output if s['nqn'] == self.id]:
            for c in s['connections']:
                ret.append(Host.instance(name=c['host-traddr']))

        return {'connected_hosts': ret}

@entity(sourcetypes=[SourceTypes.MRSP_CONFIG_FILE, SourceTypes.MRSP_RPC])
class Controller(BaseEntity):
    controller_host : Host = PropertySpec(Host, key=True)
    controller_process : Process = PropertySpec(Process)
    controller_state : str = PropertySpec(str, transient=True)
    subsystems : List[Subsystem] = PropertySpec([Subsystem])
    install_path : str = PropertySpec(str)
    config_file_path : str = PropertySpec(str)
    cores_bitmask : str = PropertySpec(str) #0xffff means 16 cores running
    recovery_state : dict = PropertySpec(dict, transient=True)
    nvmesh_bdev : 'NVMESHBdev' = PropertySpec('NVMESHBdev')

    CONTROLLER_PROCESS_NAME = 'reactor_0'
    CONTROLLER_EXEC_PATH_FORMAT = '{install_path}/app/nvmesh_tgt'
    CONTROLLER_START_CMD_FORMAT = '{exec_path}/nvmesh_tgt -c {config_path} ' \
                                  '-m 0x{cores_bitmask} -i 1 > /dev/null 2>&1 &'
    CONTROLLER_RPC_SCRIPT_FORMAT = '{install_path}/scripts/rpc.py'

    SUBSYSTEM_REGEX = re.compile(r'NQN\s+(?P<nqn>.+)|AllowAnyHost\s+(?P<any_host>.+)|SN\s+(?P<sn>.+)|'
                   r'Listen RDMA (?P<ip>\d+.\d+.\d+.\d):(?P<port>\d+)')
    NAMESPACE_REGEX = re.compile(r'Namespace\s+(?P<id>.+) (?P<namespace>\d+)')
    TPV_REGEX = re.compile('TPV\s+(?P<name>[^\s]+)\s+(?P<size>[^\s]+)')
    NVMESHBDEV_REGEX = re.compile(r'Name\s+(?P<name>.+)|^StripHeight\s+(?P<height>.+)|^Devices\s+(?P<devs>.+)', re.MULTILINE)

    CONTROLLER_STATE_UP = 'Up'
    CONTROLLER_STATE_DOWN = 'Down'
    CONTROLLER_STATE_DEGRADED = 'Up-Degraded'
    CONTROLLER_STATE_REBUILDING = 'Up-Rebuilding'
    CONTROLLER_STATE_UNKNOWN = 'Unknown'

    def __init__(self, *args, **kwargs):
        super(Controller, self).__init__(*args, **kwargs)
        self.controller_process = Process.instance(name=Controller.CONTROLLER_PROCESS_NAME,
                                                   startcmd=self.CONTROLLER_START_CMD_FORMAT.format(
                                                       exec_path=self.CONTROLLER_EXEC_PATH_FORMAT.format(
                                                           install_path=self.install_path),
                                                       config_path=self.config_file_path,
                                                       cores_bitmask=self.cores_bitmask),
                                                   host=self.controller_host)

    def send_rpc_request(self, method_name):
        connection = self.controller_host.connection
        return connection.execute('sudo {0} {1}'.format(self.CONTROLLER_RPC_SCRIPT_FORMAT.
                                                        format(install_path=self.install_path),
                                                        method_name))

    @prop_loader(SourceTypes.MRSP_RPC, ['controller_state'])
    def _load_controller_state(self):
        try:
            rpc_response = self.send_rpc_request('nvmesh_get_controller_state')
            json_response = json.loads(rpc_response[0])
            ret = json_response['Local']
        except:
            ret = self.CONTROLLER_STATE_UNKNOWN
        return {'controller_state': ret}

    def _get_tpv_by_rpc_id(self, tpv_id):
        rpc_response = self.send_rpc_request('nvmesh_list_bdevs')
        json_response = json.loads(rpc_response[0])
        return TPV.instance(id=json_response[tpv_id]['name'])

    @prop_loader(SourceTypes.MRSP_RPC, ['recovery_state'])
    def _load_recovery_state(self):
        try:
            rpc_response = self.send_rpc_request('nvmesh_get_recovery_summary')
            json_response = json.loads(rpc_response[0])
            state = json_response['State']
            if state == 'Recovering':
                tpv_id = json_response['Volume #']
                ret = {'tpv': self._get_tpv_by_rpc_id(tpv_id), 'state': state,
                       'Volume percentage done': json_response['Volume percentage done']}
            else:
                ret = {'state': state}
        except:
            ret = {'state': 'Unknown'}
        return {'recovery_state': ret}

    @staticmethod
    def _create_subsystem_from_text(section_items):
        match_dict = {}

        for m in re.finditer(Controller.SUBSYSTEM_REGEX, section_items):
            match_dict.update({k: v for (k, v) in m.groupdict().items() if v})

        return Subsystem.instance(id=match_dict['nqn'], ip=match_dict['ip'], port=match_dict['port'],
                                  sn=match_dict['sn'], tpvs=[TPV.from_dict(m.groupdict())
                                                             for m in re.finditer(Controller.NAMESPACE_REGEX,
                                                                                  section_items)])

    @staticmethod
    def _create_tpv_from_text(section_items):
        return [TPV.instance(id=m.group('name'), size=m.group('size')) for m in re.finditer(Controller.TPV_REGEX,
                                                                                            section_items)]

    @staticmethod
    def _create_nvmesh_bdev_from_text(section_items):
        match_dict = {}

        for m in re.finditer(Controller.NVMESHBDEV_REGEX, section_items):
            match_dict.update({k: v for (k, v) in m.groupdict().items() if v})

        return NVMESHBdev.instance(id=match_dict['name'],
                                   drive_pairs=[MDrivePair.instance(id=p) for p in match_dict['devs'].split()])

    @prop_loader(SourceTypes.MRSP_CONFIG_FILE, ['subsystems', 'nvmesh_bdev'])
    def _parse_config_file(self):
        config_file_sections_dispatcher = {re.compile(r'Subsystem\d*'): self._create_subsystem_from_text,
                                           re.compile(r'ThinProvisioning'): self._create_tpv_from_text,
                                           re.compile(r'NVMesh_R0_1'): self._create_nvmesh_bdev_from_text}
        res_dict = {}

        config_file = self.config_file_path
        output = self.controller_host.connection.execute('sudo cat {0}'.format(config_file))[0]
        config = configparser.ConfigParser(delimiters='=', allow_no_value=True, inline_comment_prefixes='#')
        # don't lowercase keys and vals
        setattr(config, 'optionxform', lambda x: str(x))
        config.read_string(str(output))

        for section in config.sections():
            for r, callback in list(config_file_sections_dispatcher.items()):
                if r.match(section):
                    res_dict[section] = callback(('\n'.join(s[0] for s in config.items(section))))
        return {'subsystems': [res_dict[s] for s in list(res_dict.keys()) if s.startswith('Subsystem')],
                'nvmesh_bdev': res_dict['NVMesh_R0_1']}


@entity(sourcetypes=[SourceTypes.MRSP_CONFIG_FILE])
class MRSP(BaseEntity):

    controller1 : Controller = PropertySpec(Controller)
    controller2 : Controller = PropertySpec(Controller)


@entity(sourcetypes=[SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2])
class MDrive(BaseEntity):
    id : str = PropertySpec(str, key=True)
    sn : str = PropertySpec(str)
    pci_address : str = PropertySpec(str)

    @prop_loader([SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2], ['sn', 'pci_address'])
    def _load_props(self, source=None):
        controller = MRSP.instance().controller1 if source == SourceTypes.MRSP_RPC_CONTROLLER1\
            else MRSP.instance().controller2

        output = controller.send_rpc_request('get_bdevs')[0]
        j_output = json.loads(output)

        for dev in j_output:
            if dev['name'] == self.id:
                return {'pci_address': dev['driver_specific']['nvme']['pci_address'],
                        'sn': dev['driver_specific']['nvme']['ctrlr_data']['serial_number']}


@entity(sourcetypes=[SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2])
class MDrivePair(BaseEntity):
    id : str = PropertySpec(str, key=True)
    num_of_blocks : int = PropertySpec(int)
    state : int = PropertySpec(int)
    drive1 : MDrive = PropertySpec(MDrive)
    drive2 : MDrive = PropertySpec(MDrive)

    @prop_loader([SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2], ['drive1', 'drive2',
                                                                                        'num_of_blocks'])
    def _load_drives(self, source=None):
        controller = MRSP.instance().controller1 if source == SourceTypes.MRSP_RPC_CONTROLLER1\
            else MRSP.instance().controller2

        output = controller.send_rpc_request('get_bdevs')[0]
        j_output = json.loads(output)

        for dev in j_output:
            if dev['name'] == self.id:
                drives = dev['driver_specific']['nvmp']['base_bdevs_list']
                return {'drive1': MDrive.instance(id=drives[0]) if drives[0] else None,
                        'drive2': MDrive.instance(id=drives[1]) if drives[1] else None,
                        'num_of_blocks': dev['num_blocks']}


@entity(sourcetypes=[SourceTypes.MRSP_RPC, SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2])
class NVMESHBdev(BaseEntity):
    id : str = PropertySpec(str, key=True)
    num_of_blocks : int = PropertySpec(int)
    state : int = PropertySpec(int, transient=True)
    drive_pairs : List[MDrivePair] = PropertySpec([MDrivePair])
    strip_size : int = PropertySpec(int)

    @prop_loader([SourceTypes.MRSP_RPC_CONTROLLER1, SourceTypes.MRSP_RPC_CONTROLLER2], ['state', 'num_of_blocks'])
    def _load_state(self, source=None):
        controller = MRSP.instance().controller1 if source == SourceTypes.MRSP_RPC_CONTROLLER1\
            else MRSP.instance().controller2

        output = controller.send_rpc_request('get_bdevs')[0]
        j_output = json.loads(output)

        for dev in j_output:
            if dev['product_name'] == 'NVMesh RAID-0':
                return {'state': dev['driver_specific']['nvmesh']['state'],
                        'num_of_blocks': dev['num_blocks']}
