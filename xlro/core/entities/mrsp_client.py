#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import chr
from builtins import range
from typing import List
from xlro.core.entities.base import *
from xlro.core.entities.host import Host, Service
from xlro.core.entities.mrsp import MrspTPV, Subsystem
import re
import json
from abc import ABCMeta, abstractmethod


@entity(sourcetypes=[SourceTypes.OS])
class NVMEDevice(BaseEntity):
    id : str = PropertySpec(str, key=True)
    client : 'MRSPClient' = PropertySpec('MRSPClient', key=True)
    subsystems : List[Subsystem] = PropertySpec([Subsystem], transient=True)
    tpv : MrspTPV = PropertySpec(MrspTPV)

    @prop_loader(SourceTypes.OS, ['subsystems'])
    def _load_subsystems(self):
        return {'subsystems': [Subsystem.instance(id=self.client.get_nqn(self.id), ip=s_ip) # type: ignore[attr-defined] # Not fixing MRSP at this stage
                for s_ip in self.client.get_subsystem_ips(self.client.get_nqn(self.id))]} # type: ignore[attr-defined] # Not fixing MRSP at this stage


@entity(sourcetypes=[SourceTypes.OS])
class MRSPClient(BaseEntity): # Should be ABCMeta?
    host : Host = PropertySpec(Host, key=True)
    nvme_devices : List[NVMEDevice] = PropertySpec([NVMEDevice], transient=True)

    @abstractmethod
    def _load_nvme_devices(self):
        """Purpose of this function is to load nvme devices"""
        pass

    @abstractmethod
    def connect(self, subsystem):
        pass

    @abstractmethod
    def disconnect(self, subsystem):
        pass

    @staticmethod
    def create(host, bluefield=None, *args, **kwargs):
        """Factory Method to create MRSPClient of specific OS"""
        if host.connection.execute('pwd')[2] == 0:
            return MRSPLinuxClient.instance(host=host, *args, **kwargs)
        else:
            return MRSPWindowsClient.instance(host=host, bluefield=bluefield, *args, **kwargs)


@entity(sourcetypes=[SourceTypes.OS])
class MRSPLinuxClient(MRSPClient):

    NVME_DEVICE_REGEX = r'^(?P<dev_name>[^\s]+)[ ]*(?P<sn>[^\s]+)\s*SPDK bdev Controller[ ]*(?P<ns>\d)+[^\n]*$'

    @prop_loader(SourceTypes.OS, ['nvme_devices'])
    def _load_nvme_devices(self):
        output = self.host.connection.execute('sudo nvme list | grep /dev')[0]

        r = re.compile(MRSPLinuxClient.NVME_DEVICE_REGEX, re.MULTILINE)
        ret = []
        for dev in re.finditer(r, output):
            nqn = self.get_nqn(dev.group('dev_name'))
            subsystems = self.get_subsystem_ips(nqn)
            for s in subsystems:
                for tpv in [tpv for tpv in getattr(Subsystem.instance(id=nqn, ip=s), 'tpvs', [])
                            if tpv.namespace == int(dev.group('ns'))]:
                    ret.append(NVMEDevice.from_dict(dict(id=dev.group('dev_name'), client=self, tpv=tpv)))

        return {'nvme_devices': ret}

    def get_nqn(self, dev_name):
        output = self.host.connection.execute('sudo nvme id-ctrl {0} | grep subnqn'.format(dev_name))[0]
        m = re.search('subnqn    : (?P<id>.+)', output)
        return m.group('id') if m else None

    @staticmethod
    def _extract_ip_address(data):
        match = re.match(r'traddr=(?P<ip>\d+.\d+.\d+.\d+)', data)
        assert match, 'Cannot parse IP from: {}'.format(data)
        return match.group('ip')

    def get_subsystem_ips(self, subsystem_name):
        """
        nvme list-subsys is a bit buggy so this function looks a bit wired
        """
        output = self.host.connection.execute('sudo nvme list-subsys -o json')[0]
        j_subsystems = json.loads(output)['Subsystems']
        for i in range(len(j_subsystems))[::2]:
            if str(j_subsystems[i]['NQN']) == str(subsystem_name):
                return [self._extract_ip_address(p['Address']) for p in j_subsystems[i+1]['Paths']]
        return []

    def connect(self, subsystem):
        return self.host.connection.execute('sudo nvme connect -t rdma -n "{0}" -a {1} -s {2}'.format(subsystem.id,
                                                                                                      subsystem.ip,
                                                                                                      subsystem.port))[2]

    def disconnect(self, subsystem):
        return self.host.connection.execute('sudo nvme disconnect -n "{0}"'.format(subsystem.id))[2]


@entity(sourcetypes=[SourceTypes.OS])
class MRSPWindowsClient(MRSPClient):

    bluefield : 'BlueField' = PropertySpec('BlueField')
    pci_address : str = PropertySpec(str)

    NVME_DEVICE_REGEX = re.compile(r'\S+\s+_(?P<ns>\d)+.')

    NVME_DRIVER_OP_STOP = 'Disable'
    NVME_DRIVER_OP_START = 'Enable'

    POWERSHELL_ASSIGN_LETTER = 'powershell \"Get-Partition -DiskNumber {0} -PartitionNumber 2 | ' \
                               'Set-Partition -NewDriveLetter {1}\"'
    POWERSHELL_FORMAT_DISK = 'powershell \"New-Partition -DiskNumber {0} -UseMaximumSize | ' \
                             'Format-Volume -FileSystem {1} -NewFileSystemLabel NVMx{0}\"'
    POWERSHELL_INITIALIZE_DISK = 'powershell \"Initialize-Disk -Number {0} -PartitionStyle {1}\"'
    POWERSHELL_CLEAR_DISKS = 'powershell \"Get-Disk {0} | Clear-Disk -RemoveData -Confirm:$false\"'
    POWERSHELL_GET_DISKS = 'powershell \"Get-Disk | ConvertTo-Json -DEPTH 1\"'
    POWERSHELL_SET_DRIVER = 'powershell \"Get-PnpDevice -InstanceId \'{0}\' | {1}-PnpDevice -Confirm:$false"'

    PARTITION_STYLE_MBR = 'MBR'
    PARTITION_STYLE_GPT = 'GPT'

    FILE_SYSTEM_NTFS = 'NTFS'
    FILE_SYSTEM_EXFAT = 'exFAT'
    FILE_SYSTEM_REFS = 'ReFS'

    def connect(self, subsystem):
        self._set_nvme_driver(self.NVME_DRIVER_OP_STOP)

        if self.bluefield.connect(subsystem):
            self._set_nvme_driver(self.NVME_DRIVER_OP_START)
            return 1

        self._set_nvme_driver(self.NVME_DRIVER_OP_START)

    def format_nvme_device(self, nvme_device, fs_type, partition_style):
        number = int(nvme_device.id)
        connection = self.host.connection

        connection.execute(self.POWERSHELL_CLEAR_DISKS.format(number))
        connection.execute(self.POWERSHELL_INITIALIZE_DISK.format(number, partition_style))
        connection.execute(self.POWERSHELL_FORMAT_DISK.format(number, fs_type))
        connection.execute(self.POWERSHELL_ASSIGN_LETTER.format(number,
                                                                chr(ord('C') + number)))

    def _set_nvme_driver(self, operation):
        self.host.connection.execute(self.POWERSHELL_SET_DRIVER.
                                     format(self.pci_address, operation))

    def disconnect(self, subsystem):
        self._set_nvme_driver(self.NVME_DRIVER_OP_STOP)
        self.bluefield.disconnect(subsystem)

    @prop_loader(SourceTypes.OS, ['nvme_devices'])
    def _load_nvme_devices(self):
        ret = []
        output = self.host.connection.execute(self.POWERSHELL_GET_DISKS)[0]
        j_output = json.loads(output)
        for dev in [d for d in j_output if d['BusType'] == 'NVMe']:
            ret.append(NVMEDevice.instance(id=dev['DiskNumber'], client=self,
                                           tpv=self._find_matching_tpv(dev)))
        return {'nvme_devices': ret}

    def _find_matching_tpv(self, dev):
        match = re.match(self.NVME_DEVICE_REGEX, dev['SerialNumber'])
        assert match, 'Cannot parse name-space: {}'.format(dev['SerialNumber'])
        ns = match.group('ns')
        # Our assumption is that both configuration files of controllers are the same and that we have only one subsystem
        for tpv in self._get_subsystems()[0].tpvs:
            if tpv.namespace == ns:
                return tpv

    def _get_subsystems(self):
        return self.bluefield.get_subsystems()


@entity(sourcetypes=[SourceTypes.OS])
class BlueField(BaseEntity):

    host : Host = PropertySpec(Host, key=True)
    ifbdev_name : str = PropertySpec(str)
    snap_service : Service = PropertySpec(Service)

    def __init__(self, *args, **kwargs):
        super(BlueField, self).__init__(*args, **kwargs)
        self.snap_service = Service.instance(name='nvme_snap@{0}'.
                                             format(self.ifbdev_name), host=self.host)

    def get_subsystems(self):
        j_output = self._get_configuration()
        backends = j_output['backends'][0]

        return [Subsystem.instance(id=backends['name'],
                                   ip=backends['paths'][0]['addr'])]

    def disconnect(self, subsystem):
        j_output = self._get_configuration()
        backends = j_output['backends']

        i = len(backends)
        if i == 1 and backends[0]['name'] != subsystem.id:
            # another subsystem is configured
            return 1
        elif i == 1 and backends[0]['name'] == subsystem.id:
            # this subsystem is configured check if path is configured
            deleted = False
            for i, s in enumerate(backends[0]['paths']):
                if s['addr'] == subsystem.ip:
                    del backends[0]['paths'][i]
                    deleted = True
            if not deleted:
                return 1
            if len(backends[0]['paths']) == 0:
                del backends[0]
                j_output['ctrl']['namespaces'] = []
        else:
            return 1

        self._update_configuration_file(j_output)
        self.snap_service.stop()
        return 0

    def _get_configuration(self):
        output = self.host.connection.execute('sudo cat /etc/nvme_snap/{0}.json'
                                              .format(self.ifbdev_name))[0]
        j_output = json.loads(output)
        return j_output

    def connect(self, subsystem):
        j_output = self._get_configuration()

        backends = j_output['backends']
        i = len(backends)

        path = {'addr': subsystem.ip,
                'port': 4420,
                'ka_timeout_ms': 15000,
                'nqn': 'nqn.2016-06.io.spdk:nvme-bluefield-{0}'.format(self.host.name.split('.')[2])}

        if i == 1 and subsystem.id == backends[0]['name']:
            # subsystem is already configured, check if need to add path to a new controller
            paths = backends[0]['paths']
            if len(paths) == 1 and paths[0]['addr'] == subsystem.ip:
                return 1
            paths.append(path)
        elif i == 1 and subsystem.id != backends[0]['name']:
            # There is different subsystem configured - only one subsystem is supported
            return 1
        elif i == 0:
            # There was no subsystem configured, add it and its namespaces to config file
            backends.append({'id': 'nvmftgt1',
                             'type': 'nvmf_rdma',
                             'name': subsystem.id,
                             'paths': [path]
                             })
            for tpv in subsystem.tpvs:
                j_output['ctrl']['namespaces'].append({'nsid': tpv.namespace,
                                                       'size_mb': tpv.size,
                                                       'format': {'block_order': 12,
                                                                  'metadata': 8},
                                                       'backend': {'id': 'nvmftgt1',
                                                                   'nsid': tpv.namespace,
                                                                   'format': {'integrity': 'crc',
                                                                              'block_order': 12,
                                                                              'metadata': 8}
                                                                   },
                                                       'format': {'block_order': 12,
                                                                  'metadata': 8}
                                                       })
        else:
            return 1

        self._update_configuration_file(j_output)
        self.snap_service.stop()
        self.snap_service.start()
        return 0

    def _update_configuration_file(self, j_output):
        dumped_json = json.dumps(j_output, indent=2)
        sftp = self.host.connection.client.open_sftp()
        f = sftp.file('/etc/nvme_snap/{0}.json'.format(self.ifbdev_name), 'w+')
        f.write(dumped_json)
        f.close()
