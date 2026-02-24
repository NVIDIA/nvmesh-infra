#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import next
from builtins import str
from builtins import map
from builtins import range
from builtins import object
import yaml
from typing import Dict, Optional, Union, Any, Iterable, List, Mapping
from xlro.core.entities import Host, Service, Process, BaseEntity, NamedEntity, BaseService
# from xlro.core.entities.host import BaseService
from xlro.core.entities.base import entity, prop_loader, PropertySpec, SourceTypes
from xlro.core.entities.sdk_base import SdkException
from xlro.core.util.general_utils import wait_for_it, IDAdapter, WaitResult, host_name
from xlro.core.util.ssh import Connection
from abc import ABCMeta, abstractmethod
from xlro.core.util.tree import toJson
from xlro.core.sdk.Utils import MongoObj
import os
import re
import json
import time
import datetime
import logging
import threading
from collections import defaultdict

SWITCH_MAPPER: Dict[str, Any] = {}

@entity(sourcetypes=[SourceTypes.PROC])
class BasePort(NamedEntity): # Should be ABCMeta?
    protocol : str = PropertySpec(str)
    net_state : str = PropertySpec(str, transient=True)
    if_name : str = PropertySpec(str, default='')

    STATUS_UP = ['up', 'active', 'linkup', 'ok']
    STATUS_DOWN = ['disabled', 'down', 'admdn', 'missing', 'error', 'link_down']

    # switch-side interface for switchports, physical port number for hostports
    # name = port number

    def _wait_for_status(self, status):
        current = self.net_state.lower()
        if current in status:
            return WaitResult(True)
        return WaitResult(False, "key: {}, value {}".format(self, current))

    def wait_for_connect(self, timeout=60):
        return wait_for_it(lambda: self._wait_for_status(BasePort.STATUS_UP), timeout=timeout, quiet=False)

    def wait_for_disconnect(self, timeout=60):
        return wait_for_it(lambda: self._wait_for_status(BasePort.STATUS_DOWN), timeout=timeout, quiet=False)

    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def disconnect(self):
        pass


@entity(sourcetypes=[SourceTypes.PROC])
class SwitchPort(BasePort):
    switch : 'BaseSwitch' = PropertySpec('BaseSwitch', key=True)
    hostport : 'HostPort' = PropertySpec('HostPort')

    @prop_loader(SourceTypes.PROC, ['net_state'])
    def _load_net_state(self):
        self.logger.debug('Loading SwitchPort {} net_state'.format(self.name))
        state = self.switch.load_switchport_net_state(self.name)
        self.logger.debug(f'SwitchPort: {self.name}, State: {state}')
        return {'net_state': state}

    def snapshot(self):
        return self.switch.snapshot(self.name)

    def connect(self):
        if self.hostport.net_state in BasePort.STATUS_UP and self.net_state in BasePort.STATUS_DOWN:
            raise Exception('Cached mapping to switchport {} {} is incorrect.'.format(self.switch.name, self.name))
        self.logger.debug('Connecting switchport {}.'.format(self.to_dict()))
        return self.switch.connect_switchport(self.name)

    def disconnect(self):
        if self.hostport.net_state in BasePort.STATUS_DOWN and self.net_state in BasePort.STATUS_UP:
            raise Exception('Cached mapping to switchport {} {} is incorrect.'.format(self.switch.name, self.name))
        self.logger.debug('Disconnecting switchport {}.'.format(self.to_dict()))
        return self.switch.disconnect_switchport(self.name)


@entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC])
class HostPort(BasePort): # Should be ABCMeta?
    lid : str = PropertySpec(str)
    phys_state : str = PropertySpec(str)                  # should be transient, TODO: loader if used
    sm_state : str = PropertySpec(str)                    # should be transient, TODO: loader if used
    switchport : SwitchPort = PropertySpec(SwitchPort)
    sm_lid : str = PropertySpec(str)
    num : str = PropertySpec(str, default='1')             # TODO: building entity from file may try to convert num to int
    nic : 'NIC' = PropertySpec('NIC', key=True)
    gid : str = PropertySpec(str)                         # ibstat value - 'Port GUID'
    guid : str = PropertySpec(str)                        # ibstatus value - 'default gid'
    sgid : str = PropertySpec(str)                        # called GUID in the Management
    transport : str = PropertySpec(str, default='unset')  # TODO: default not working in inheritance?
    ip : str = PropertySpec(str, default='')
    mtu : int = PropertySpec(int, default=None)
    health : str = PropertySpec(str)                      # SDK value, refers to topology nic health status reported from TOMA
    status : str = PropertySpec(str)                      # SDK value, one of ['OK', 'MISSING', 'ERROR', 'LINK_DOWN']
    sys_guid : str = PropertySpec(str)                    # ibstat value - 'System image GUID'
    firmware_ver : str = PropertySpec(str)

    @abstractmethod
    def map_switchport(self):
        pass

    @property
    def host(self):
        return self.nic.host

    @prop_loader(SourceTypes.PROC, ['if_name', 'net_state'])
    def _load_ibdev2netdev(self):
        ibdev = self.host.ibdev2netdev()[self.nic.name][self.num]
        return {'if_name': ibdev['if_name'], 'net_state': ibdev['net_state']}

    @prop_loader(SourceTypes.PROC, ['switchport'])
    def build_switchport(self):
        # builds switch and returns appropriate switchport entity
            try:
                if self.get_property('phys_state', no_cache=True) == 'LinkUp':
                    sdict, spname = self.map_switchport()
                    stype = sdict.pop('type')
                    switch = self.ENTITY_REGISTRY[stype].from_dict(sdict, source=SourceTypes.PROC)
                    sdict['type'] = stype
                    return {'switchport': {'name': spname,
                                           'switch': switch,
                                           'protocol': self.protocol,
                                           'hostport': self}}
                else:
                    raise Exception('Link is down for device {}'.format(self.name))
            except Exception as e:
                self.logger.error('Could not build switchport for {}: {}.'.format(self.if_name, repr(e)), exc_info=True)
                return {'switchport': None}

    @prop_loader(SourceTypes.PROC, ['num', 'protocol', 'guid', 'lid', 'sm_lid', 'phys_state', 'sm_state', 'sys_guid', 'firmware_ver'])
    def _load_ibstat_pdict(self):
        return self.host.ibstat(self.name)[self.name]['ports'][0]


@entity(sourcetypes=[SourceTypes.PROC])
class ROCEPort(HostPort):
    hw_gid: str = PropertySpec(str)  # HW-GID of RoCE/TCP port, acts the same as IBPort.gid
    port_control_cmds = ('if{}', 'ip', 'ifconfig')

    @classmethod
    def set_port_ctl_cmds(cls, port_ctl_cmds):
        cls.port_control_cmds = port_ctl_cmds

    def map_switchport(self):
        return self.host.lldpctl(self.if_name)

    def connect(self, *cmds):
        self.logger.debug('Connecting hostport {}.'.format(self.to_dict()))
        return self._set_port_state('up', *cmds)

    def disconnect(self, *cmds):
        self.logger.debug('Disconnecting hostport {}.'.format(self.to_dict()))
        return self._set_port_state('down', *cmds)

    def _set_port_state(self, state: str, *cmds: Iterable[str]) -> str:
        state = state.lower()
        assert state in ("up", "down"), "{} is not a valid input to port state commands".format(state)
        if not cmds:
            cmds = self.port_control_cmds

        path = None
        for cmd in cmds:
            try:
                path = self.host.cache_cmd_path(cmd.format(state))  # type: ignore
                break
            except EnvironmentError as e:
                self.logger.debug(repr(e))

        assert path, 'Unable to find any of commands {} on {} for setting port state'.format(list(cmds), self.name)
        if path.endswith('ip'):
            cmd = 'sudo {path} link set dev {interface} {state}'.format(path=path, interface=self.if_name, state=state)
        elif path.endswith('ifconfig'):
            cmd = 'sudo ifconfig {interface} {state}'.format(interface=self.if_name, state=state)
        else:
            cmd = 'sudo {path} {interface}'.format(path=path, interface=self.if_name)

        out, err, code = self.host.connection.execute(cmd)
        if code:
            raise Exception(' "{}" fail on {}. OUT:{} , err:{} '.format(cmd, self.host.name, out, err))
        return out

    @prop_loader(SourceTypes.PROC, ['ip'])
    def _load_ip(self):
        output = self.host.connection.execute('sudo ip -4 addr show {0}'.format(self.if_name))[0]
        match = re.search(r'inet (?P<ip>\d+.\d+.\d+.\d+)', output)
        assert match, 'Could not parse IP from: {}'.format(output)
        return {'ip': match.group('ip')}

    @prop_loader(SourceTypes.PROC, ['sgid'])
    def _load_sgid(self):
        return {'sgid': '0000:0000:0000:0000:0000:ffff:{:02x}{:02x}:{:02x}{:02x}'.format(*list(map(int, self.ip.split('.'))))}

    @prop_loader(SourceTypes.PROC, ['hw_gid'])
    def _load_hw_gid(self):
        if not self.sgid:
            return {'hw_gid': ''}
        out, _, code = self.nic.host.connection.execute(f'grep -l {self.sgid} /proc/nvmeibs/nic_gids/*')
        hwgid = re.findall(r'[a-f0-9]{32}', out)[0]
        formatted_hwgid = ':'.join([hwgid[:4], hwgid[4:8], hwgid[8:12], hwgid[12:16], hwgid[16:20], hwgid[20:24],
                                    hwgid[24:28], hwgid[28:32]])
        return {'hw_gid': formatted_hwgid}

    @staticmethod
    def sgid2ip(sgid):
        hex_ip = sgid.replace(":", "")[-8:]
        return ".".join(str(int(hex_ip[index : index + 2], 16)) for index in range(0, len(hex_ip), 2))


@entity(sourcetypes=[SourceTypes.PROC])
class TCPPort(ROCEPort):
    port_control_cmds = ('ifconfig', 'ip', 'if{}')
    link_cmd = None

    @prop_loader(SourceTypes.PROC, ['if_name'])
    def _get_if_name(self):
        return {'if_name': self.name.lstrip('siw_')}

    @prop_loader(SourceTypes.PROC, ['net_state', 'phys_state'])
    def _get_state(self):
        # for TCP setup without ibtools use this loader
        if not self.link_cmd:
            ip_cmd = self.nic.host.cache_cmd_path('ip')
            self.link_cmd = '{} link show {} up'.format(ip_cmd, self.if_name)
        out, _, code = self.nic.host.connection.execute(self.link_cmd)
        if out and not code:
            return {'net_state': 'Up', 'phys_state': 'LinkUp'}
        return {'net_state': 'Down', 'phys_state': 'Disabled'}


@entity(sourcetypes=[SourceTypes.PROC])
class IBPort(HostPort):
    def map_switchport(self):
        ibnetdiscover = self.host.ibnetdiscover(self.guid.lstrip('0x'), self.nic.name)
        ibswitches = self.host.ibswitches(self.nic.name)[ibnetdiscover['guid']]
        sminfo = self.host.sminfo(self.nic.name)

        sdict = {'type': ibswitches['type']}
        if sdict['type'] == BaseSwitch.UNMANAGED_MELLANOX:
            sdict['name'] = self.host.smpquery(self.nic.name, sminfo['smlid'])
        else:
            sdict['name'] = ibnetdiscover['name']

        sdict['guid'] = ibnetdiscover['guid']
        sdict['lid'] = ibswitches['lid']
        sdict['sm_guid'] = sminfo['smguid']
        return sdict, ibnetdiscover['port']

    def connect(self):
        return self.switchport.connect()

    def disconnect(self):
        return self.switchport.disconnect()

    @prop_loader(SourceTypes.PROC, ['gid'])
    def _load_gid(self):
        return {'gid': self.host.ibstatus()[self.nic.name][self.num]['gid']}

    @prop_loader(SourceTypes.PROC, ['sgid'])
    def _load_sgid(self):
        return {'sgid': self.gid}

    @prop_loader(SourceTypes.PROC, ['net_state'])
    def _load_net_state(self):
        for port in self.host.ibstat(self.nic.name)[self.nic.name]['ports']:
            if port['num'] == self.num:
                return {'net_state': port['sm_state']}


@entity(sourcetypes=[SourceTypes.PROC])
class BFRepresentorPort(ROCEPort):
    port_control_cmds = ('ifconfig', 'ip', 'if{}')

    def map_switchport(self):
        out, err, code = self.host.execute(f"sudo ovs-vsctl list-ifaces `sudo ovs-vsctl port-to-br {self.if_name}` | grep -E 'p[0-9]+'")
        if not code:
            return self.host.lldpctl(out.strip())
        return self.host.lldpctl(self.if_name)

@entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC])
class NIC(BaseEntity):
    name : str = PropertySpec(str, key=True)
    host : 'Node' = PropertySpec('Node', key=True)
    ports : List[HostPort] = PropertySpec([HostPort])


@entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC])
class Node(Host):
    nics : List[NIC] = PropertySpec([NIC])

    def __init__(self, *args, **kwargs):
        super(Node, self).__init__(*args, **kwargs)
        self.is_lldpd = None

    # Moved this out of constructor to not force SSH on instance create (e.g., simulator problem)
    # TODO: Why are services a Property, and how can it be overridden in child?
    _services: Optional[Mapping[str, BaseService]] = None
    @property
    def services(self):
        os_name = self.os_info['NAME'].lower()
        if self._services is None:
            self._services = {'lldpd': Process.instance(name='lldpd', startcmd='lldpd', host=self),
                              'network': Service.instance(name='networking' if 'ubuntu' in os_name else 'network', host=self),
                              'firewall': Service.instance(name='ufw' if 'ubuntu' in os_name else 'firewalld', host=self),
                              'irqbalance': Service.instance(name='irqbalance', host=self),
                              'tuned': Service.instance(name='tuned', host=self)}
        return self._services

    # TODO: move to utils
    def _err2exc(self, cmd):
        outbuf, errbuf, code = self.connection.execute(cmd)
        if code != 0:
            raise Exception(errbuf)
        return outbuf

    def sdk_nics_to_entities(self, sdk_nics: Dict) -> List[NIC]:
        ''' processes the nics section of SDK Target response '''
        nics: List[NIC] = []
        for sdk_nic in sdk_nics:
            nic = NIC.instance(name=sdk_nic['deviceType'], host=self, nicID=sdk_nic['nicID'])
            protocol = sdk_nic['protocol'].lower()
            ptype = 'IBPort' if protocol == 'infiniband' else 'TCPPort' if protocol == 'tcp' else 'ROCEPort'
            sgid = sdk_nic['guid']
            pdict = {'host': self,
                     'nic': nic,
                     'name': sdk_nic['deviceType'],
                     'protocol': protocol,
                     'transport': 'TCP' if protocol == 'tcp' else 'RDMA',
                     BaseEntity.TYPE_PROP: ptype,
                     'sgid': sgid,
                     'ip': None if protocol == 'infiniband' else ROCEPort.sgid2ip(sgid),
                     'mtu': sdk_nic['mtu'],
                     'health': sdk_nic['health'],
                     'status': sdk_nic['status']}
            ports = [HostPort.from_dict(pdict, SourceTypes.MANAGEMENT)]
            nic.set_property('ports', ports, SourceTypes.MANAGEMENT)
            nics.append(nic)
        return nics

    @prop_loader(SourceTypes.MANAGEMENT, ['nics'])
    def _load_nics_from_mgmt(self):
        from xlro.core.entities.target import Target
        nics_proj = [MongoObj(field='nics', value=1)]
        nics_filter = [MongoObj('node_id', Target.instance(name=self.name)._name)]

        try:
            sdk_nics = next(Target._sdk_get(projection_mongo_objs=nics_proj, filter_mongo_objs=nics_filter))['nics']
        except IndexError:
            raise SdkException(self, 'Unable to get SDK NICs for {}, possibly a client-only node'.format(self.name))

        return {'nics': self.sdk_nics_to_entities(sdk_nics)}

    @prop_loader(SourceTypes.PROC, ['nics'])
    def _load_nics_from_proc(self):
        ibdev = self.ibdev2netdev()
        # TODO: Need to fix config-object sub-typing
        conf = self.nvmeshconf()
        assert conf is not None, 'Config is required.'
        nics_in_conf = conf.get('CONFIGURED_NICS', '')
        tcp_enabled = conf.get('TCP_ENABLED', 'no').lower() == "yes"
        configured_nics = {}
        if nics_in_conf:
            for nic_spec in nics_in_conf.split(';'):
                nic, _, transport = nic_spec.partition('/')
                configured_nics[nic] = transport.upper() or 'RDMA'

        ibstat_res = {}
        for nname in [n for n in self.get_nic_names() if n in ibdev]:
            try:
                ibstat_res.update(self.ibstat(dev=nname))
            except Exception as e:
                # ibstat didn't return data for this nic - make sure we don't need this nic anyway
                assert configured_nics and not (nname in configured_nics or any(p['if_name'] in configured_nics for p in ibdev[nname].values())), \
                    f"Unable to fetch {nname} nic on {self.name} via ibstat: {repr(e)}"

        nics = []
        for nname, ndict in ibstat_res.items():
            nic = NIC.instance(name=nname, host=self)
            ports = []
            for pdict in ndict['ports']:
                if tcp_enabled:
                    pdict['protocol'] = 'tcp'
                pdict.update(ibdev[nic.name][pdict['num']])
                transport = 'RDMA'
                if configured_nics:
                    transport = configured_nics.get(ibdev[nic.name][pdict['num']]['if_name'],
                                                    configured_nics.get(nname + ':{}'.format(pdict['num'])))
                    if not transport:  # This is not one of the configured nics, so skip
                        continue

                if pdict['protocol'] == 'infiniband':
                    ptype = 'IBPort'
                else:
                    if 'bluefield' in self.info['kernel-release']:
                        ptype = 'BFRepresentorPort'
                    else:
                        ptype = 'ROCEPort'

                pdict.update({'host': self,
                              'transport': transport,
                              BaseEntity.TYPE_PROP: ptype,
                              'gid': self.ibstatus()[nic.name][pdict['num']]['gid'],
                              'nic': nic})
                port = HostPort.from_dict(pdict, source=SourceTypes.PROC)
                ports.append(port)
            if not ports:
                del nic
                continue
            nic.set_property('ports', ports, SourceTypes.PROC)
            nics.append(nic)
        return {'nics': nics}

    def ibstat(self, dev=''):
        nic_regex = re.compile('CA \'(?P<nic>[^\']+)\'')
        port_regex = re.compile('Port (?P<num>[0-9]+)')

        result = self._err2exc('{} {}'.format(self.cache_cmd_path('ibstat'), dev))
        nics = {}

        for nname, ndict in list(toJson(result, '\t', '[\r\n]+', ':').items()):
            match1 = nic_regex.match(nname)
            if not match1:
                raise Exception('Unable to parse nic physical name from {} in ibstat output {}.'.format(nname, result))

            ports = []
            for pname in ndict:
                match = re.match(port_regex, pname)
                if not match:
                    continue
                pdict = {
                    'num': match.group('num'),
                    'protocol': ndict[pname]['Link layer'].lower(),
                    'firmware_ver': ndict['Firmware version'],
                    'guid': ndict[pname]['Port GUID'],
                    'sys_guid': ndict['System image GUID'],
                    'lid': ndict[pname]['Base lid'],
                    'sm_lid': ndict[pname]['SM lid'],
                    'phys_state': ndict[pname]['Physical state'],
                    'sm_state': ndict[pname]['State']
                }
                ports.append(pdict)
            nics[match1.group('nic')] = {'guid': ndict['Node GUID'], 'ports': ports}
        return nics

    def get_nic_names(self) -> List[str]:
        return self._err2exc(f'{self.cache_cmd_path("ibstat")} -l').rstrip('\n').split('\n')

    def ibdev2netdev(self):
        """
        Maps nic physical device and port to network device.
        Returns None if parsing fails.
        """
        result = self._err2exc(self.cache_cmd_path('ibdev2netdev'))
        rdict = toJson(result, '\t', '\n', '==>')
        denormalize_data = defaultdict(list)

        for dname, ddict in list(rdict.items()):
            match1 = re.search("(?P<dev>.+) port (?P<num>[0-9]+)", dname)
            match2 = re.search("(?P<net>.+) \((?P<state>.+)\)", ddict)
            if match1 and match2:
                dev, num = match1.group('dev'), match1.group('num')
                net, state = match2.group('net'), match2.group('state')
                denormalize_data[dev].append((int(num), net, state))

        """
        ibdev2netdev using the proc of /sys/class/net/<iface>/dev_port so for specific nic the lowest port can be a 
        number that is bigger than one.
        ibstat and ibstatus using a different method to choose port numbers and the lowest port will always be 1 so we
        need to normalize ibdev2netdev results.
        """
        nics: Dict[str, Dict[str, Dict]] = defaultdict(dict)
        for dev, info in denormalize_data.items():
            min_port = min([port_info[0] for port_info in info])
            distance_from_1 = min_port - 1
            for num, net, state in info:
                normalize_port_num = num - distance_from_1
                nics[dev][str(normalize_port_num)] = {'name': dev, 'if_name': net, 'net_state': state}

        return nics

    def ibstatus(self):
        """
        Maps nics physical device and port to gid.
        Returns: None if parsing fails.
        """
        result = self._err2exc(self.cache_cmd_path('ibstatus'))
        rdict = toJson(result, '\t', '[\r\n]+', ':')

        nics: Dict[str, Dict] = defaultdict(dict)
        for dname, ddict in list(rdict.items()):
            match1 = re.search("'(?P<dev>.+)' port (?P<num>[0-9]+) status", dname)
            if match1:
                dev, num = match1.group('dev'), match1.group('num')
                if num not in nics[dev]:
                    nics[dev][num] = {}
                nics[dev][num]['gid'] = ddict['default gid']
        return nics

    def ibnetdiscover(self, guid, if_name):
        switches = {}
        result = self._err2exc('sudo {} -C {} | grep {} | cat'.format(self.cache_cmd_path('ibnetdiscover'),
                                                                      if_name, guid.lstrip('0x')))
        rdict = toJson(result, '\t', '[\r\n]+', '', patternsToIgnore=['#.+', '\[.+\]\t"H-.+'])

        # Assumed only single link found per port.
        for link in rdict:
            match1 = re.search(
                '\[.+\]\(.+\) \t"S-(?P<sguid>.+)"\[(?P<sport>.+)\]\t\t# lid .+ lmc .+ ".+;(?P<sname>.+):', link)
            if not match1:
                # IB Unmanaged
                match1 = re.search(
                    '\[.+\]\(.+\) \t"S-(?P<sguid>.+)"\[(?P<sport>.+)\]\t\t# lid .+ lmc .+ "(?P<sname>.+)" lid', link)
            if match1:
                sguid, sport, sname = match1.group('sguid'), match1.group('sport'), match1.group('sname')
                switches = {'guid': '0x' + sguid, 'port': sport, 'name': sname}
        return switches

    def ibswitches(self, if_name):
        """ Type is the name of the switch class. """
        result = self._err2exc('sudo {} -C {}'.format(self.cache_cmd_path('ibswitches'), if_name))
        rdict = toJson(result, '\t', '[\r\n]+', 'ports', patternsToIgnore=['src.+'])

        switches = {}
        for sname, sdict in list(rdict.items()):
            if sdict:
                match1 = re.search('Switch\t: (?P<sguid>.+)', sname)
                match1 = re.search('Switch\t: (?P<sguid>.+)', sname)
                match2 = re.search(';(?P<name>.+):.* lid (?P<lid>.+) lmc', sdict)
                if match2 is None:
                    # IB Unmanaged
                    match2 = re.search('"(?P<name>.+)" base port .+ lid (?P<lid>.+) lmc', sdict)

                if match1 and match2:
                    switches[match1.group('sguid')] = {'name': match2.group('name'),
                                                       'lid': match2.group('lid'),
                                                       'type': self.parse_switch_type({'name': match2.group('name')})}
        return switches

    def parse_switch_type(self, switch_info):
        if not switch_info:
            return BaseSwitch.UNKNOWN

        self.logger.debug(f'Switch-type: Info={switch_info}')
        global SWITCH_MAPPER
        if not SWITCH_MAPPER:
            with open("{}/config/switch_mapper.yaml".format(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'rb') as fp:
                SWITCH_MAPPER = yaml.safe_load(fp)

        for lookup_field in SWITCH_MAPPER['default_lookup']:
            path = lookup_field.split('.')
            value = switch_info
            for field in path:
                try:
                    value = value[field]
                except KeyError:
                    self.logger.debug(f'Switch-type: No such field "{field}" in switch info')
                    value = None
                    break
            if not value:
                continue

            self.logger.debug(f'Switch-type: {lookup_field}: ({type(value)}) {value}')
            for stype, lookup_info in SWITCH_MAPPER['switch_types'].items():
                if re.search(lookup_info['patterns'], value, re.IGNORECASE):
                    self.logger.debug(f'Switch-type: {stype}')
                    return stype

        return BaseSwitch.UNKNOWN

    def smpquery(self, name, smlid):
        result = self._err2exc('sudo {} ND -C {} -L {}'.format(self.cache_cmd_path('smpquery'), name, smlid))
        match1 = re.search('Node Description:\.+(?P<smhost>.+) .+', result)
        if match1:
            return match1.group('smhost')

    def sminfo(self, if_name):
        result = self._err2exc('sudo {} -C {}'.format(self.cache_cmd_path('sminfo'), if_name))
        match1 = re.search('sminfo: sm lid (?P<smlid>.+) sm guid (?P<smguid>.+), .+', result)
        if match1:
            return {'smlid': match1.group('smlid'), 'smguid': match1.group('smguid')}

    def lldpctl(self, name, wait=3, retries=20):
        # if this is a vlan interface we remove the vlan part
        name = name.partition('.')[0]

        if self.is_lldpd is None:
            _, _, code = self.connection.execute("sudo which lldpd")
            self.is_lldpd = False if code else True
        if not self.is_lldpd:
            raise Exception("lldpd not installed on node {0}".format(self.name))

        self.services['lldpd'].start()

        count = 0
        while count < retries:
            result = self._err2exc('sudo {} {}'.format(self.cache_cmd_path('lldpctl'), name))
            rdict = toJson(result, ' ', '[\r\n]+', ':  ', patternsToIgnore=['--+'])
            sdict = {}

            try:
                chassis = rdict['Interface']['Chassis']
                sdict['name'] = host_name(chassis.get('MgmtIP', chassis.get('SysName')))
                sdict['type'] = self.parse_switch_type(rdict)
                unparsed_spname = rdict['Interface']['Port']['PortID']
                match1 = re.search('ifname (?P<spname>.+)', unparsed_spname)
                if not match1:
                    # virbr has mac instead of ifname in PortID. PortDescr is ifname
                    unparsed_spname = rdict['Interface']['Port']['PortDescr']
                    match1 = re.search('(?P<spname>.+)', unparsed_spname)
                    if not match1:
                        raise Exception('Unable to parse switchport name from {}.'.format(unparsed_spname))
                spname = match1.group('spname') if match1 else unparsed_spname
                return sdict, spname
            except KeyError:
                count += 1
                time.sleep(wait)

        raise Exception('Unable to parse lldpctl, last output: {}'.format(result))


class Channel(object):
    RECV_MAX = 1024
    logger: Union[logging.Logger, IDAdapter] = logging.getLogger(__name__)

    def __init__(self, client, prompt):
        self.client = client
        self.channel = client.invoke_shell()
        self.c_id = str(self.channel)
        self.prompt = prompt
        self.logger = IDAdapter(logging.getLogger('.'.join([self.__module__, self.__class__.__name__])))
        self.flush()    # flush login

    def send(self, cmd, timeout=300):
        s = cmd
        start_t = datetime.datetime.now()

        self.logger.debug('Sending: {}'.format(repr(cmd)))
        while s:
            wait_for_it(self.channel.send_ready, poll=0.01, timeout=timeout).assert_result(
                'Channel {} is not send-ready after {} seconds'.format(self.c_id, timeout))
            sent = self.channel.send(s)
            s = s[sent:]

            time_waited = (datetime.datetime.now() - start_t).total_seconds()
            if time_waited > timeout:
                raise EOFError('Unable to complete send {} via {}. Last sent: {}'.format(repr(cmd), self.c_id, sent))

    def expect(self, timeout=300):
        """flush=True reads until no new data is read; need to match the end prompt.
           Limiting buffer reading with timeout"""
        buff = ''
        match = None
        start_t = datetime.datetime.now()

        while not match:
            time_waited = (datetime.datetime.now() - start_t).total_seconds()
            if time_waited > timeout:
                raise EOFError('Unable to complete read from buffer after {} seconds on channel {}, '
                               'buffer: {}'.format(timeout, self.c_id, buff))
            elif self.channel.recv_ready():
                ndata = self.channel.recv(Channel.RECV_MAX).decode()

                if ndata == '':
                    raise EOFError('Connection closed unexpectedly on channel {}, buffer: {}.'.format(self.c_id, buff))

                buff += ndata
                buffer_lines = self.normalize(buff).split('\n')
                match = re.search(self.prompt, buffer_lines[-1])
            if not match:
                if self.channel.closed:
                    raise EOFError('Connection closed on channel {}, buffer: {}'.format(self.c_id, buff))
                time.sleep(0.05)

        return buff

    def flush(self):
        # receive everything on the input buffer.
        self.expect()  # wait for first prompt
        self.send('\n')  # KS: not sure why we do this actually
        self.expect()

    def close(self):
        self.channel.close()

    @staticmethod
    def normalize(text):
        return re.sub('(\r|\n)+', '\n', text)


def _translate_spname(func):
    def wrapper(self, spname, channel=None):
        match1 = re.search('Eth(?P<slot>.+)/(?P<num>.+)', spname)
        spname = 'ethernet {}/{}'.format(match1.group('slot'),
                                         match1.group('num')) if match1 else 'ib 1/{}'.format(spname)
        return func(self, spname, channel)
    return wrapper


def switch_command(func):
    ''' Wrap a sequence of switch commands to initialize connection/channel '''
    def _manage_conn(self, *args, retries=1, **kwargs):
        with self.conn_lock:
            try:
                self.undo.clear()
                self.logger.debug(f'switch-command {func.__name__}() current channel: {self.channel}')
                while retries >= 0:
                    retries -= 1
                    try:
                        assert self.channel, 'Channel not initialized'
                        self._disable_paging(channel=self.channel)
                        return func(self, *args, channel=self.channel)
                    except Exception as e:
                        # Above could fail if no initial channel or connection/client dropped, so recreate channel and retry
                        self.logger.debug(f'Initial try failed {repr(e)}. Reset channel.')
                        self.connection.reconnect(force=True)
                        self.channel = Channel(self.connection.client, self.PROMPT)
            except Exception as e:
                self.connection._need_to_reconnect = True
                self.logger.info(f'switch-command {func.__name__} failed: {repr(e)}')
                raise
            finally:
                while self.undo:
                    self._switch_cmd(self.undo.pop(), channel=self.channel)

    return _manage_conn


@entity(sourcetypes=[SourceTypes.PROC])
class BaseSwitch(Host): # Should be ABCMeta?
    _is_switch = True
    # name: switch ip
    UNMANAGED_MELLANOX = 'UnmanagedMellanoxSwitch'
    MELLANOX = 'MellanoxSwitch'
    CUMULUS = 'CumulusSwitch'
    SUPERMICRO = 'SupermicroSwitch'
    DELL = 'DellSwitch'
    CISCO = 'CiscoSwitch'
    VIRTUAL = 'VirtualSwitch'
    UNKNOWN = 'UnknownSwitch'

    # Must be overridden in concrete class
    PROMPT: Optional[str] = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conn_lock = threading.RLock()
        self.channel = None
        self.undo: List[str] = []

    def _switch_cmd(self, subcmd, channel):
        subcmd = subcmd + '\n' if len(subcmd) == 0 or subcmd[-1] != '\n' else subcmd
        self.logger.debug('switch sub-command: {}, channel: {}'.format(subcmd, channel))
        channel.send(subcmd)
        return channel, channel.expect()

    def _mode_config(self, channel=None):
        self.undo.append('exit')
        return self._switch_cmd('configure terminal', channel=channel)

    def _mode_interface(self, spname, channel=None):
        self.undo.append('exit')
        return self._switch_cmd('interface {}'.format(spname), channel=channel)

    def _disable_paging(self, channel=None):
        return self._switch_cmd('terminal length 0', channel=channel)

    def snapshot(self, spname, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

    def load_switchport_net_state(self, spname, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

    @switch_command
    def connect_switchport(self, spname, channel=None):
        channel, _ = self._mode_config(channel=channel)
        self._mode_interface(spname, channel=channel)
        return self._switch_cmd('no shutdown', channel=channel)

    @switch_command
    def disconnect_switchport(self, spname, channel=None):
        channel, _ = self._mode_config(channel=channel)
        self._mode_interface(spname, channel=channel)
        return self._switch_cmd('shutdown', channel=channel)


@entity(sourcetypes=[SourceTypes.PROC])
class MellanoxSwitch(BaseSwitch):
    PROMPT = '.*.+ \[.+\] .+'

    guid : str = PropertySpec(str)
    lid : str = PropertySpec(str)
    sm_guid : str = PropertySpec(str)

    def _mode_config(self, channel=None):
        channel, _ = self._switch_cmd('enable', channel=channel)
        self.undo.append('disable')
        return super(MellanoxSwitch, self)._mode_config(channel=channel)

    def _disable_paging(self, channel=None):
        return self._switch_cmd('no cli session paging enable', channel=channel)

    @switch_command
    @_translate_spname
    def snapshot(self, spname, channel=None):
        channel, _ = self._mode_config(channel)
        return self._switch_cmd('show interfaces {}'.format(spname), channel=channel)

    @_translate_spname
    def connect_switchport(self, spname, channel=None):
        return super(MellanoxSwitch, self).connect_switchport(spname, channel=channel)

    @_translate_spname
    def disconnect_switchport(self, spname, channel=None):
        return super(MellanoxSwitch, self).disconnect_switchport(spname, channel=channel)

    def load_switchport_net_state(self, spname, channel=None):
        channel, result = self.snapshot(spname, channel=channel)
        self.logger.debug('load_switchport_net_state returned: ' + result)
        rdict = toJson(result, ' ', '\r\n', ':', startPattern='(Eth|IB)[0-9]+/[0-9]', stopPattern='Rx')
        try:
            return rdict[spname]['Operational state']
        except KeyError:
            return rdict['IB1/{} state'.format(spname)]['\tLogical port state']


@entity(sourcetypes=[SourceTypes.PROC])
class UnmanagedMellanoxSwitch(Node):
    PROMPT = '\[.+@.+ .+\]#'

    sm_guid : str = PropertySpec(str, key=True)
    lid : str = PropertySpec(str)
    guid : str = PropertySpec(str)
    sm_net : str = PropertySpec(str)

    def __init__(self, *args, **kwargs):
        super(UnmanagedMellanoxSwitch, self).__init__(*args, **kwargs)
        self.sm_net, self.sm_lid = self._load_sm_net()

    # @prop_loader(SourceTypes.PROC, ['sm_guid'])
    # def _load_sm_guid(self):
    #     return { 'sm_guid': self.sminfo()['smguid'] }

    # @prop_loader(SourceTypes.PROC, ['sm_net'])
    # TODO: should be loader? Tried but self was outtermost host, xlro_props does not have context (flat dict)
    def _load_sm_net(self):
        for nic in self.nics:
            for port in nic.ports:
                if port.guid == self.sm_guid:
                    return nic.name, nic.ports[0].sm_lid
                    # return {'sm_net': nic.name }

    def disable_paging(self, channel=None):
        pass

    def snapshot(self, spname):
        return self.ibportstate(spname, 'query')

    def connect_switchport(self, spname):
        return self.ibportstate(spname, 'enable')

    def disconnect_switchport(self, spname):
        return self.ibportstate(spname, 'disable')

    def ibportstate(self, spname, op):
        # TODO: do we care about output of enable/disable
        result = self._err2exc('sudo {} -s {} {} {} -C {} {}'.format(self.cache_cmd_path('ibportstate'),
                                                                     self.sm_lid, self.lid, spname, self.sm_net, op))
        self.logger.debug('ibportstate returned: ' + result)
        if op == 'query':
            rdict = toJson(result, '', '[\r\n]+', ':\.*',
                                              startPattern='Switch PortInfo:',
                                              stopPattern='Peer PortInfo:',
                                              patternsToIgnore=['#.+'])
            del rdict['Switch PortInfo']
        else:
            rdict = toJson(result, '', '[\r\n]+', ':\.*',
                                              startPattern='After PortInfo set:',
                                              patternsToIgnore=['#.+'])
            del rdict['After PortInfo set']
        return rdict

    def load_switchport_net_state(self, spname):
        result = self.snapshot(spname)
        return result['LinkState']

@entity(sourcetypes=[SourceTypes.PROC])
class SupermicroSwitch(BaseSwitch):
    PROMPT = '.+@.+:.+$'

    def _disable_paging(self, channel=None):
        pass

    @switch_command
    def snapshot(self, spname, channel=None):
        return self._switch_cmd(f'ip link show {spname}', channel=channel)

    @switch_command
    def connect_switchport(self, spname, channel=None):
        return self._switch_cmd('sudo ifup {}'.format(spname), channel=channel)

    @switch_command
    def disconnect_switchport(self, spname, channel=None):
        return self._switch_cmd('sudo ifdown {} --admin-state'.format(spname), channel=channel)

    def load_switchport_net_state(self, spname, channel=None):
        _, result = self.snapshot(spname, channel=channel)
        self.logger.debug('load_switchport_net_state returned: ' + result)
        return 'UP' if 'state UP' in result else 'DOWN'

@entity(sourcetypes=[SourceTypes.PROC])
class VirtualSwitch(BaseSwitch):
    PROMPT = '.+@.+:.+$'

    def _disable_paging(self, channel=None):
        pass

    @switch_command
    def snapshot(self, spname, channel=None):
        # Channel is SSH with terminal allocated. If output has ANSI
        # color codes, they will be captured. Substring match will be
        # failing on such output. Work that around by suppressing
        # color output in ip with -c=never
        return self._switch_cmd(f'ip -c=never link show {spname}', channel=channel)

    @switch_command
    def connect_switchport(self, spname, channel=None):
        return self._switch_cmd('sudo ip link set dev {} up'.format(spname), channel=channel)

    @switch_command
    def disconnect_switchport(self, spname, channel=None):
        return self._switch_cmd('sudo ip link set dev {} down'.format(spname), channel=channel)

    def load_switchport_net_state(self, spname, channel=None):
        _, result = self.snapshot(spname, channel=channel)
        self.logger.debug('load_switchport_net_state returned: ' + result)
        return 'DOWN' if 'state DOWN' in result else 'UP'

@entity(sourcetypes=[SourceTypes.PROC])
class CumulusSwitch(SupermicroSwitch):
    @switch_command
    def connect_switchport(self, spname, channel=None):
        return self._switch_cmd(f'nv set interface {spname} link state up ; nv config apply -y', channel=channel)

    @switch_command
    def disconnect_switchport(self, spname, channel=None):
        return self._switch_cmd(f'nv set interface {spname} link state down ; nv config apply -y', channel=channel)

@entity(sourcetypes=[SourceTypes.PROC])
class DellSwitch(BaseSwitch):
    PROMPT = 'dell.+#'

    @switch_command
    def snapshot(self, spname, channel=None):
        return self._switch_cmd('show interface {}'.format(spname), channel=channel)

    def load_switchport_net_state(self, spname, channel=None):
        channel, result = self.snapshot(spname, channel=channel)
        self.logger.debug('load_switchport_net_state returned: ' + result)
        # TODO: for now supporting only hundredGigE and Ethernet as this is what we have in the lab
        rdict = toJson(result, ' ', '\r\n', ' is ', startPattern=f"(hundredGigE|Ethernet) [0-9]+/[0-9]", stopPattern='Hardware')
        match = re.match(r'ethernet(?P<eth_port>[\d/:]+)', spname)
        if match:
            spname = 'Ethernet ' + match.group('eth_port')
        match1 = re.match('(?P<admin_state>.+), line protocol is (?P<line_protocol_state>.+)', rdict[spname])
        assert match1, 'Cannot parse admin_state from: {}'.format(rdict[spname])
        return match1.group('line_protocol_state')


@entity(sourcetypes=[SourceTypes.PROC])
class CiscoSwitch(BaseSwitch):
    PROMPT = 'Cisco.+#'

    @switch_command
    def snapshot(self, spname, channel=None):
        return self._switch_cmd('show interface {} | json'.format(spname), channel=channel)

    def load_switchport_net_state(self, spname, channel=None):
        channel, result = self.snapshot(spname, channel=channel)
        self.logger.debug('load_switchport_net_state returned: ' + result)
        rdict = result.split('\r\n')[1:-1]  # remove command and prompt
        return json.loads('\r\n'.join(rdict))['TABLE_interface']['ROW_interface']['state']


@entity(sourcetypes=[SourceTypes.PROC])
class UnknownSwitch(BaseSwitch):
    PROMPT = '.+'

    def __init__(self, *args, **kwargs):
        super(UnknownSwitch, self).__init__(*args, **kwargs)
        self.logger.warning('Initializing a protection object for an unsupported switch type')

    def connect_switchport(self, spname, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

    def disconnect_switchport(self, spname, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

    def _mode_config(self, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

    def _mode_interface(self, spname, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

    def _disable_paging(self, channel=None):
        raise NotImplementedError('Unsupported command for an Unknown switch')

def main():
    from xlro.core.util.cli_util import CLIArgumentParser
    from xlro.core.util.general_utils import get_hostnames
    parser = CLIArgumentParser(require_manager=False)
    parser.add_argument('names', nargs='+')
    args = parser.parse_args()
    for name in args.names:
        node = Node.instance(name=name)
        for nic in node.nics:
            print(f'NIC: {nic}')
            for port in nic.ports:
                print(f'    PORT: {port}  ->  SWITCHPORT: {port.switchport} NETSTATE: {port.switchport.net_state} SWITCH: {port.switchport.switch}')
    return 0


if __name__ == '__main__':
    main()
