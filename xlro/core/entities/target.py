# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from collections import defaultdict

from future import standard_library
standard_library.install_aliases()
from builtins import str
from builtins import zip
from builtins import object
from time import sleep
import re
import json
import csv
import random
import re
import threading
import sys
import os
from io import StringIO
from deprecated import deprecated
from typing import Dict, Optional, Union, Any, List, Type, TYPE_CHECKING
if TYPE_CHECKING:
    from xlro.core.entities import Manager

from xlro.core.entities.base import BaseEntity
from xlro.core.entities.sdk_base import sdk_entity, SDKEntity, RE
from xlro.core.entities.host import *
from xlro.core.entities.drive import Drive
from xlro.core.entities.nvnode import NvNode
from xlro.core.entities.etypes import HostName
from xlro.core.entities.network import Node
from xlro.core.util.consts import Deprecate
from xlro.core.util.general_utils import host_name, tolerant_func, host_aliases
from xlro.core.util.ssh import Connection, remote_python_command, temp_dir
from xlro.core.util.common import get_path
from os import path

MODPROBE = "modprobe"

@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC, SourceTypes.OS])
class Target(SDKEntity):
    PROC = '/proc/nvmeibs'
    TOMA_RPC_CMD = "/opt/nvmesh/common-repo/tools/toma_rpc"
    drives : List[Drive] = PropertySpec([Drive])
    # gpt : GPT = PropertySpec(GPT)
    #nics : List['NIC'] = PropertySpec(['NIC'])

    class HEALTH(object):
        HEALTHY = 'healthy'
        ALARM = 'alarm'
        CRITICAL = 'critical'

    name : str = PropertySpec(HostName, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    _name : str = PropertySpec(str)
    uuid : str = PropertySpec(str)
    version : str = PropertySpec(str)
    in_recovery : bool = PropertySpec(bool)
    health : str = PropertySpec(str)
    all_drives : List[Drive] = PropertySpec([Drive])

    COMMON_MODULE = "nvmeib_common"
    COMMON_PUBLIC_MODULE = "nvmeib_common_public"
    NVMEIBS_MODULE = "nvmeibs"
    PERSISTENCIES = "/var/opt/nvmesh/toma/*"
    NETLINK_UTIL = "/opt/nvmesh/common-repo/tools/nvmesh_netlink.py"
    USE_LOCAL_NETLINK = ['read', 'write']

    def __init__(self, *args, **kwargs):
        super(Target, self).__init__(*args, **kwargs)
        self.property_lock = threading.Lock()

    @classmethod
    def _sdk_get(cls: Type[RE], *args, **kwargs):
        dicts =  super()._sdk_get(*args, **kwargs)
        for d in dicts:
            if 'nics' in d:
                node = Node.instance(name=d.get('node_id', d.get('_id')))
                node.set_property('nics', node.sdk_nics_to_entities(sdk_nics=d['nics']), SourceTypes.MANAGEMENT)
            yield d

    @prop_loader(SourceTypes.MANAGEMENT, ['_name'])
    def get_rest_name(self):
        # Infra standardized name may not me management name.  So look for a match on any alias.
        try:
            aliases = host_aliases(self.name)
            return {'_name': list(self.get_headlines(count=1, query={'node_id': aliases}).values())[0]._name}
        except Exception as e:
            self.logger.info(f'Cannot get aliases for {self.name}.  {repr(e)}')
            return {'_name': self.name}

    @property
    def rest_id(self):
        # If we've never been loaded from Mgmt, we have to search for the correct name
        return self.get_property('_name', SourceTypes.MANAGEMENT)

    @classmethod
    def map_props(cls, propmap, source_type=None):
        if source_type == SourceTypes.MANAGEMENT:
            target_sdk_name = propmap.get('node_id', propmap.get('_id'))
            if target_sdk_name:
                propmap.setdefault('_name', target_sdk_name)
                propmap.setdefault('name', target_sdk_name)
        propmap = super(Target, cls).map_props(propmap, source_type)
        if 'name' in propmap:
            # TODO - find better way to do so
            propmap['name'] = propmap['name'].replace('_000', '')
            try:
                propmap['name'] = host_name(propmap['name'])
            except Exception:
                cls.logger.info("couldn't detect host name for {}".format(propmap['name']))

            if 'drives' in propmap and source_type == SourceTypes.MANAGEMENT:
                # Strip excluded drives returned by MGMT
                included = []
                propmap['all_drives'] = propmap['drives']
                for ddict in propmap['drives']:
                    if ddict.get('isExcluded', False):
                        continue
                    ddict['nodeID'] = propmap.get('node_id', propmap.get('_name', propmap.get('name')))
                    included.append(ddict)
                propmap['drives'] = included
        return propmap

    # TODO - replace with xlro.core.util.entities_utils.get_toma_leader_service
    def get_toma_leader(self) -> Optional[str]:
        out, err, code = self.host.connection.execute('sudo cat /proc/nvmeibs/toma_status/leader')
        leader = out.strip()
        if code == 0 and leader and ' ' not in leader:
            return leader
        return None

    @property
    def toma_config(self):
        config_str = self.toma_rpc_cmd('config print')
        toma_config = {}
        for conf in config_str.split('\n'):
            match = re.match('\+ param (?P<conf>\w+) (?P<value>\d+)', conf)
            if match:
                toma_config[match.group('conf')] = match.group('value')
        return toma_config

    def get_toma_peer_node_to_last_vote_time(self):
        assert self.get_toma_leader() in host_aliases(self.name), f'Querying toma peer nodes from a non-leader TOMA {self.name}'
        raft_proc = self.proc_content('toma_status/raft', no_cache=True)
        regex_pattern = "\s*- ((?!effective)[^: ]*):[^=]*=([0-9.]*).*"  # returns node name and time, ignoring line starting with "effective"
        return [list(tup) for tup in re.findall(regex_pattern, raft_proc)]

    def kill_toma_leader(self):
        leader = self.get_toma_leader()
        if leader:
            Target.instance(name=leader).services['toma'].stop()
        return leader

    def remove_pci(self, drive):
        drive = drive if isinstance(drive, Drive) else Drive.instance(name=drive)
        drive.get_property('pci_address', SourceTypes.PROC)
        pci_address = drive.pci_address
        cmd = 'sudo sh -c "echo 1 > /sys/bus/pci/drivers/nvmeibs/{}/remove"'.format(pci_address)
        _, err, code = Connection.execute_on_host(self.name, cmd)
        if code != 0:
            raise Exception('remote execution failed. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))

    def rescan_pci(self):
        cmd = 'sudo sh -c "echo 1 > /sys/bus/pci/rescan"'
        _, err, code = Connection.execute_on_host(self.name, cmd)
        if code != 0:
            raise Exception('remote execution failed. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))

    def pci_power(self, pci_slot):
        cmd = 'cat /sys/bus/pci/slots/{}/power'.format(pci_slot)
        return True if Connection.execute_on_host(self.name, cmd)[0].strip() == "1" else False

    @tolerant_func(delay=10, log_level='warning')
    def remove_drive(self, drive):
        # PCI slot power off is a one way ticket in QEMU. Do it via devices/remove instead
        model = drive.get_property('model', source=SourceTypes.MANAGEMENT)
        if 'QEMU' in model.upper():
            self.remove_pci(drive)
        else:
            cmd = 'sudo sh -c "echo 0 > /sys/bus/pci/slots/{}/power"'.format(drive.pci_slot)
            _, err, code = Connection.execute_on_host(self.name, cmd)
            # Sometimes cmd gets timeout, but drive state still changes. So verify value of 'power' before raising Exception
            if code != 0 and self.pci_power(drive.pci_slot):
                raise Exception('Failed to remove_drive, cmd={}, code={}, err={}'.format(cmd, code, err))

    @tolerant_func(delay=10, log_level='warning')
    def return_drive(self, drive):
        model = drive.get_property('model', source=SourceTypes.MANAGEMENT)
        if 'QEMU' in model.upper():
            self.rescan_pci()
        else:
            cmd = 'sudo sh -c "echo 1 > /sys/bus/pci/slots/{}/power"'.format(drive.pci_slot)
            _, err, code = Connection.execute_on_host(self.name, cmd)
            # Sometimes cmd gets timeout, but drive state still changes. So verify value of 'power' before raising Exception
            if code != 0 and not self.pci_power(drive.pci_slot):
                raise Exception('Failed to return drive, cmd={}, code={}, err={}'.format(cmd, code, err))

    @tolerant_func(delay=10, log_level='warning')
    def reset_nvme(self, drive_seq):
        cmd = 'sudo sh -c "echo d > /proc/nvmeibs/freeze{}"'.format(str(drive_seq))
        _, err, code = Connection.execute_on_host(self.name, cmd)
        if code != 0:
            raise Exception('Remote execution failed. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))

    @property
    def ports(self):
        return [p.sgid for n in self.node.nics for p in n.ports]
        from xlro.core.util.cli_util import jsonify
        self.logger.warning(f'Getting ports.')
        nics = self.node.get_property('nics', SourceTypes.MANAGEMENT)
        # self.logger.warning(f'NICS: {nics}')
        ports = [p for n in nics for p in n.ports]
        # self.logger.warning(f'PORTS: {ports}')
        for p in ports:
            self.logger.warning(f'PORT: {jsonify(p.to_dict(), indent=2)}')
        return [p.sgid for p in ports]

    @property
    def nic_ids(self):
        return [n.nicID for n in self.node.nics]

    @prop_loader(SourceTypes.PROC, ['drives'])
    def load_drives_from_proc(self):
        drives: List['Drive'] = []
        for ddict in csv.DictReader(StringIO(self.proc_for_drives(True))):  # type: Dict[str, Any]
            ddict['name'] = ddict.pop('id')
            ddict['target'] = self
            ddict['blockSize'] = ddict.pop('block_size')
            self.logger.info('DISK: {}'.format(ddict))
            ddict['mgmt'] = self.mgmt
            drive = Drive.from_dict(ddict, source=SourceTypes.PROC)
            # Let this be lazy...
            # drive.load_properties(SourceTypes.PROC)
            drives.append(drive)
        return {'drives': drives}

    @prop_loader(SourceTypes.NETLINK, ['drives'])
    def load_drives_from_netlink(self):
        drives = []
        # TODO: the pattern should be to have netlink loader in Drive which optionally takes a dict
        # That keeps field knowledge localized
        for info in json.load(self.netlink('drives')):
            drive = Drive.instance(name=info['disk_id'].rstrip('\x00'),
                                   mgmt=self.mgmt)
            drive.set_properties(dict(
                blockSize=info['hw_block_size'],
                metadata=info['hw_md_size'],
                blocks=info['n_hw_blocks'],
                is_inline=bool(info['md_inline']),
                target=self,
            ), SourceTypes.NETLINK)
            drives.append(drive)
        return {'drives': drives}

    @staticmethod
    def evict_drives(drives_names):
        return Drive.evict_drives(drives_names)

    def proc_for_partitions(self, no_cache=False, proc_partitions_path="partitions.csv"):
        return self.proc_content(path.join(self.PROC, proc_partitions_path), no_cache)

    def proc_for_drives(self, no_cache=False):
        proc_drives_path = "disks.csv"
        return self.proc_content(proc_drives_path, no_cache)

    def proc_for_nics(self, no_cache=False):
        proc_nics_path = "nics.csv"
        return self.proc_content(proc_nics_path, no_cache)

    def proc_for_volumes(self, no_cache=False):
        bdev_path = path.join('toma_status', 'bdev')
        return self.proc_content(bdev_path, no_cache)

    def proc_for_toma_cfg(self, no_cache=False):
        cfg_path = path.join('toma_status', 'cfg')
        return self.proc_content(cfg_path, no_cache)

    def proc_for_toma_rdma(self, no_cache=False):
        rdma_path = path.join('toma_status', 'rdma')
        return self.proc_content(rdma_path, no_cache)

    @property
    def netlink_util_version(self):
        if not hasattr(self, "_netlink_util_version"):
            # Parallel netlink I/O was running this command a LOT of times
            with self.property_lock:
                if not hasattr(self, "_netlink_util_version"):
                    out, _, code = self.connection.execute("{} --version".format(self.NETLINK_UTIL))
                    if code != 0:
                        self.logger.info(f'Netlink not found! Defaulting to version 0.1. (code={code}, out={out}')
                        self._netlink_util_version = "0.1"
                    else:
                        self._netlink_util_version = out.strip()
        return self._netlink_util_version

    def netlink(self, subcmd, inbuf=None, **kwargs):
        if subcmd in self.USE_LOCAL_NETLINK and self.name == Connection.localhostname():
            self.logger.debug(f'Running in-process')
            return self.local_netlink(subcmd, inbuf, **kwargs)
        self.logger.debug(f'Running exec')
        if self.netlink_util_version:
            return self.direct_netlink(subcmd, inbuf, **kwargs)
        raise Exception("couldn't find {} on {}".format(self.NETLINK_UTIL, str(self)))

    def local_netlink(self, subcmd, inbuf=None, **kwargs) -> Tuple[bytes, bytes]:
        ''' This is temporary.  Once nvmesh py supports p3, we should not have local copies and load those '''
        from xlro.core.entities.netlink.nvmesh_netlink import OPS
        op_class, arg_map = OPS[subcmd]
        # reverse arg mapping
        arg_map = {v: k for k, v in arg_map.items()}
        op_args = {arg_map.get(k, k): v for k, v in kwargs.items()}
        op_args['pid'] = os.getpid()
        if subcmd == 'write':
            op_args['inbuf'] = inbuf
        for retry in range(5):
            try:
                return op_class(return_data=True, **op_args).perform()
            except Exception as e:
                errmsg = f'local_netlink {subcmd} failed. {repr(e)} (retry={retry})'
                self.logger.info(errmsg)
                sleep(2**retry)

        raise Exception(errmsg)

    def direct_netlink(self, subcmd, inbuf=None, **kwargs):
        netlink_args = ("--{}='{}'".format(k, v) for k, v in kwargs.items())
        netlink_cmd = " ".join((self.NETLINK_UTIL, subcmd, " ".join(netlink_args)))
        stdin, stdout, stderr = self.connection.spawn(netlink_cmd)
        if inbuf:
            self.logger.info('NETLINK: writing {} bytes to {}'.format(len(inbuf), stdin))
            while inbuf:
                written = stdin.channel.send(inbuf)
                inbuf = inbuf[written:]
            stdin.channel.shutdown_write()
            stdin.close()
        return stdout

    @staticmethod
    def build_services(host):
        return {'toma': Service.instance(name='nvmeshtoma', host=host, pid_path='nvmeshtarget/toma'),
                'target': Service.instance(name='nvmeshtarget', host=host)}

    _ENT2SEC = None  # cached Dict[BaseEntity subcls, corresponding cfg SECTION_NAME str]

    @classmethod
    def get_entity_cls2cfg_section(cls) -> Dict[type, str]:
        """cls cached map of BaseEntity classes to relevant cfg 'SECTION NAME' str"""
        if not cls._ENT2SEC:
            from xlro.core.entities import Volume, Chunk, PRaid, Segment, Target, NIC
            cls._ENT2SEC = {Volume: 'BLOCK_DEVICES_V1_4',
                            Chunk: 'CHUNKS',
                            PRaid: 'PRAIDS_1',
                            Segment: 'DISK_SEGMENTS',
                            Target: 'NODES',
                            Drive: 'DISKS',
                            NIC: 'NICS'}
        return cls._ENT2SEC

    @classmethod
    def get_proc_target(cls) -> 'Target':
        from xlro.core.entities import Manager, Target
        rnd_tar = random.choice(Manager.get_manager().targets)
        leader = rnd_tar.get_toma_leader()
        return Target.instance(name=leader) if leader else rnd_tar

    _last_sec2csv = None  # cached Dict['cfg section name', 'cfg csv str']

    @classmethod
    def get_toma_cfg_sec2csv(cls, target: Optional['Target'] = None, no_cache: Optional[bool] = False) -> Dict[str, str]:
        if not (target or no_cache) and cls._last_sec2csv:
            return cls._last_sec2csv  # prevents unnecessary call in case 'proc_target' changed, and never called cfg
        target = target or cls.get_proc_target()
        cfg_str = target.proc_for_toma_cfg(no_cache=no_cache)
        zip_list = re.split('SECTION NAME: (.*)\n', cfg_str)[1:]
        cls._last_sec2csv = dict(list(zip(zip_list[::2], zip_list[1::2])))
        return cls._last_sec2csv

    @classmethod
    def get_toma_entity_csv(cls, entity_cls: type, target: Optional['Target'] = None, no_cache: Optional[bool] = False) -> str:
        return cls.get_toma_cfg_sec2csv(target=target, no_cache=no_cache)[cls.get_entity_cls2cfg_section()[entity_cls]]

    @property
    def configured_nics(self):
        cmd = 'sudo cat /etc/nvmesh/nvmesh.conf | grep "^CONFIGURED_NICS" | sed \'s/[CONFIGURED_NICS=,"]//g\''
        out, err, code = Connection.execute_on_host(self.name, cmd)
        configured_nics_names = out.split(';')
        return [nic for nic in self.nvnode.node.nics if nic.name in configured_nics_names]

    @property
    def services(self):
        return self.build_services(self.nvnode.host)

    # support OLD APIs - should be removed
    _nvnode: Optional['NvNode'] = None
    @property
    def nvnode(self):
        if not self._nvnode:
            self._nvnode = NvNode.instance(name=self.name)
        return self._nvnode

    # TODO - same method as in MgmtHost, consider merging
    @prop_loader(SourceTypes.OS, ['version'])
    def load_version_from_proc(self):
        return {'version': self.nvnode.get_property('version', no_cache=True)}

    @property # type: ignore # JW: mypy says annotated property not supported :-(
    def host(self) -> 'Host':
        return self.nvnode.host

    # This seems a legit convenience. No reason to deprecate, IMO.
    # @deprecated(Deprecate.ToBeReplaced(NvNode.connection))
    @property # type: ignore # JW: mypy says annotated property not supported :-(
    def connection(self) -> Connection:
        return self.nvnode.host.connection

    @deprecated(Deprecate.ToBeReplaced(NvNode.reboot))
    def reboot(self, force_level=0, wait=True):
        return self.nvnode.host.reboot(force_level, wait)

    @deprecated(Deprecate.ToBeReplaced(NvNode.ipmi))
    def ipmi(self, cmd, wait=True):
        return self.nvnode.host.ipmi(cmd, wait)

    def proc_content(self, proc_path, no_cache=False, *args, **kwargs):
        return self.nvnode.proc_content(self.PROC, proc_path, no_cache, *args, **kwargs)

    def modprobe_target_kmods(self):
        out, err, code = self.nvnode.host.connection.execute(
            "sudo {0} {1} && sudo {0} {2} && sudo {0} {3}".format(MODPROBE,
                                                            self.COMMON_MODULE,
                                                            self.COMMON_PUBLIC_MODULE,
                                                            self.NVMEIBS_MODULE))
        if err:
            raise Exception("{} - nvmeibs module is down,ERROR:{}".format(self.name, err))
        self.logger.info("Upload nvmeibs service in {} module manually".format(self.name))

    @property
    def node(self):
        return self.nvnode.node

    @prop_loader(SourceTypes.PROC, ['in_recovery'])
    def load_recovery_status(self):
        recovery_info_rgx = r"RECOVERY STATUS\n?(?P<recovery_info>[\S\s]*?)\*+"

        out = self.nvnode.proc_content(self.PROC, 'toma_status/recover', True)
        # TODO temporary debug print, will be removed when no longer necessary
        self.logger.info("Recovery status output:\n{}".format(out))
        match = re.search(recovery_info_rgx, out)
        assert match, 'Cannot parse recovery info from: {}'.format(out)

        return {'in_recovery': match.group("recovery_info") != ""}

    def toma_rpc_cmd(self, cmd):
        return Connection.err2exc(self.host.connection.execute("sudo {} {}".format(self.TOMA_RPC_CMD, cmd)))

    def nics_csv_to_dict(self):
        nics_in_csv = [nic for nic in csv.DictReader(StringIO(self.proc_for_nics()))]
        return {nic['device']: nic for nic in nics_in_csv}

    @prop_loader(SourceTypes.OS, ['drives'])
    def _load_drives_os(self):
        included_drives_sh = '''
            (
                if ''' + self.TOMA_RPC_CMD + ''' config print | grep -q 'generic_block_device_support *[^0]$'
                then
                    find /dev -maxdepth 1 -type b -regex '/dev/sd[a-zA-Z]+' | xargs -rl sudo udevadm info | grep -Po '(DEVNAME|ID_SERIAL_SHORT)=\K.*' | paste -sd ' \n'
                fi
                sudo env PATH=/opt/nvmesh/target-repo/scripts/nvme-cli/:/usr/local/sbin:$PATH nvme list | grep /dev
            ) | while read drive sn model model2 ns rest; do
                    echo "CHECKING $drive" >&2
                    ! grep -q ", *$sn *," /etc/nvmesh/target_devices.conf \
                        && sudo python -c "import os; os.close(os.open('${drive}', os.O_RDONLY|os.O_EXCL))" \
                        && echo $sn.$ns
                done
                exit 0
        '''
        included, err, code = self.connection.execute(included_drives_sh)
        assert not code, "{}: included drives command failed: {}".format(self.name, err)
        self.logger.debug(f'OS get drives trace:{os.linesep}{err}')

        return {'drives': [{'name': d} for d in included.splitlines()]}

    @classmethod
    def set_zones(cls, target2zone: Dict['Target', int]):
        targets_per_zone = defaultdict(list)
        for target, zone in target2zone.items():
            targets_per_zone[zone].append(target.name)

        for zone, targets in targets_per_zone.items():
            cls.do_operation(target.mgmt, 'set-zone', targets, zone=zone)

    def toma_rdma_status(self):
        """
        Checks TOMA RDMA proc to verify all paths are 'state: Connected', except for local paths.
        :return: False if any other state exists for non-local path
        """
        proc_rdma = self.proc_for_toma_rdma(no_cache=True)
        network_type = re.search(r'type=(\w+)', proc_rdma).group(1)
        # regex returns path details for all paths: from IP/GID, to IP/GID, state, last_error
        if network_type == 'INFINIBAND':
            long_gids = [port.gid for nic in self.node.nics for port in nic.ports]
            own_ip_addresses = []
            for long_gid in long_gids:
                # convert long GID to short form in TOMA proc. Can probably be done in single line somehow...
                short_gid = re.sub(r'0{2,}', '', long_gid)
                short_gid = re.sub(r':{3,}', '::', short_gid)
                own_ip_addresses.append(short_gid)
            pattern = (r'- ~~path (?P<from>([\w\d]{1,4}[:]{1,})+[\w\d]{1,4})->.*?:'
                       r'(?P<to>([\w\d]{1,4}[:]{1,})+[\w\d]{1,4}).*'
                       r'state: (?P<state>[\w\s]+), '
                       r'last_error: (?P<last_error>[\w\s]+)(?:,|\n)')
        else:
            own_ip_addresses = [port.ip for nic in self.node.nics for port in nic.ports]
            pattern = (r'- ~~path (?P<from>\d+\.\d+\.\d+\.\d+)->.*:'
                       r'(?P<to>\d+\.\d+\.\d+\.\d+).*'
                       r'state: (?P<state>[\w\s]+), '
                       r'last_error: (?P<last_error>[\w\s]+)(?:,|\n)')

        paths_list = [
            [match.group('from'), match.group('to'), match.group('state'), match.group('last_error')]
            for match in re.finditer(pattern, proc_rdma)
        ]

        not_connected_paths = []
        for p in paths_list:
            if p[0] in own_ip_addresses and p[1] in own_ip_addresses:
               pass
            elif p[2] != 'connected':
                self.logger.info(f'Unconnected TOMA path on {self.name}! {p[0]} -> {p[1]}, state: {p[2]}, last_error: {p[3]}')
                not_connected_paths.append(p)
            else:
                pass

        return False if not_connected_paths else True



