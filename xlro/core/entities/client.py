# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import socket
from builtins import map
from builtins import str
from builtins import range
from builtins import object
import os
import re
import json
import logging
from functools import partial

import uuid
from xlro.core import infra_conf
from xlro.core.util.thread_manager import ThreadPoolManager

import enum

from deprecated import deprecated
from typing import MutableMapping, Mapping, List, Tuple, Any, Iterable, Optional, Union, Generator, Dict, Callable, \
    TYPE_CHECKING, Type, Set
from enum import Enum
from datetime import datetime, timedelta
from time import sleep
from threading import Lock

from requests import HTTPError

from xlro.core.sdk.Utils import MongoObj
from xlro.core.util.block_objects import PageData
from xlro.core.util.cli_util import EntityArg
from xlro.core.util.consts import Deprecate
from xlro.core.util.lba import LBARange
from xlro.core.util.ssh import Connection
from xlro.core.util.dict_util import reverse_updated_dict
from xlro.core.entities.base import prop_loader, PropertySpec, SourceTypes, entity, BaseEntity, NamedEntity, \
    entity_method
if TYPE_CHECKING:
    from xlro.core.entities import Manager #Host, Service, Volume, Drive, BaseEntity, NamedEntity
from xlro.core.entities.host import Host, Service
from xlro.core.entities.volume import Volume
from xlro.core.entities.drive import Drive
from xlro.core.entities.sdk_base import SDKEntity, sdk_entity, UnknownEntity, RE
from xlro.core.util.general_utils import wait_for_it, wait_for_property_values, list_index_insert, host_name, host_aliases, WaitResult
from xlro.core.entities.nvnode import NvNode
from xlro.core.entities.etypes import HostName, EmulationMode
from xlro.core.util.trace_utils import PagerUtils, TRACE_METHOD2TYPE, TraceEntry
from xlro.core.entities.topology import PraidTopology, SegmentTopology, TRMessage, RTMessage, ClientTomaMessage
from xlro.core.util.general_utils import WaitResult
from os import path

class AttachException(Exception):
    def __init__(self, msg, json_string):
        super(AttachException, self).__init__(msg, json_string)
        try:
            self.info = json.loads(json_string)
        except:
            self.info = None


def counter_dispatcher(counter_dict: Dict[Any, Any], path_to_handler: Dict[str, Callable]) -> Dict[Any, Any]:
    ret = {}
    for k, handler in path_to_handler.items():
        to_update = True
        path_lst = k.split('.')
        counter_dict_p = counter_dict
        for p in path_lst:
            try:
                counter_dict_p = counter_dict_p[p]
            except KeyError:
                to_update = False
                break

        if to_update:
            ret.update(handler(counter_dict_p))
    return ret


def counter_shallow_handler(orignal_dict: Dict[Any, Any], filter_out: Iterable[str] = ()) -> Dict[Any, Any]:
    def _safe_assignment(ddict, key, val):
        if key in filter_out:
            return
        if key in ddict:
            logging.getLogger(__name__).warning("{} already defined in original dict".format(key))
        else:
            ddict[key] = int(val)

    ret: Dict[str, int] = {}
    for k, v in orignal_dict.items():
        if k in filter_out:
            continue
        if isinstance(v, dict):
            for add_key, add_val in counter_shallow_handler(v).items():
                _safe_assignment(ret, add_key, add_val)

        if isinstance(v, list):
            raise Exception("no list support")
        else:
            try:
                _safe_assignment(ret, k, v)
            except:
                continue

    return ret


@sdk_entity(sourcetypes=[SourceTypes.PROC, SourceTypes.MANAGEMENT, SourceTypes.BINARY_TRACE])
class Attachment(SDKEntity):
    PATTERN = r'^Name=(?P<vname>[-\w]+),.*type=(?P<is_hidden>\w+)(\s|\S)*Device status: (?P<status>[-\w]+), (?P<status_str>[-\w, ]+) \(.*\nIO is currently (?P<is_io_enabled>\w+)'
    _PATTERN = re.compile(PATTERN)
    # We ignore any non-attached statuses.  Probably should read/filter consts.js:consts.volumeAttachmentStatusToName
    # See comments of NVMESH-4595 that these are the ONLY Detached Statuses that can be returned by Management
    DETACHED_STATUSES = (2,)
    STATUS_MAP = reverse_updated_dict({
        1: 'Busy',
        2: 'Detached',
        3: 'Detach_Failed',
        4: 'Attached',
        5: 'Attach_Failed',
        6: 'Attaching',
        7: 'Detaching',
        9: 'Detached_After_Shutdown',
        17: 'Volume_Reservation_Denied'
    })

    volume : 'Volume' = PropertySpec('Volume', key=True)
    vname : str = PropertySpec(str)
    client : 'Client' = PropertySpec('Client', key=True)
    status : str = PropertySpec(str, default='Detached') # attached / detached - see management repo /.consts.js
    status_str : str = PropertySpec(str) # 'Live, with IO' - not sure what does it mean
    # io_status : str = PropertySpec(str) # enabled , etc.
    io_enabled : bool = PropertySpec(bool)
    is_hidden : bool = PropertySpec(bool, default=False)
    io_permission_status : int = PropertySpec(int) #io_perm enum : see nvmesh/clnt/block/nvmeibc_topology.c
    debug_di : bool = PropertySpec(bool)
    destager : bool = PropertySpec(bool, transient=True)
    topology : 'ClientVolumeTopology' = PropertySpec('ClientVolumeTopology')
    topo : dict = PropertySpec(dict) # topology info from proc
    reservation_mode : str = PropertySpec(str)
    reservation_version : int = PropertySpec(int)
    preempt : str = PropertySpec(str)
    crc_enabled : bool = PropertySpec(bool)
    local_read : bool = PropertySpec(bool)
    attach_blksz : int = PropertySpec(int)
    is_snapshot_ready : bool = PropertySpec(bool)
    combined_io_enabled : bool = PropertySpec(bool)
    is_snapshot_attached : bool = PropertySpec(bool)
    emulation_mode : EmulationMode = PropertySpec(EmulationMode)
    referenceIDs : List[str] = PropertySpec([str])

    # This 2 are probably Volume property - but for now only block layer is exposed
    MULTI_TIER_RAID = 'Multier Volume'
    MAX_RETRY_SECS_DEFAULT = 2 ** 20
    raid_type : str = PropertySpec(str)
    flow_counters : dict = PropertySpec(dict)
    iostats_counters : dict = PropertySpec(dict)
    status_counters : dict = PropertySpec(dict)
    capacity : int = PropertySpec(int)


    # Reservation Modes
    NONE = 'NONE'
    EXCLUSIVE_READ_WRITE = 'EXCLUSIVE_READ_WRITE'
    SHARED_READ_ONLY = 'SHARED_READ_ONLY'
    SHARED_READ_WRITE = 'SHARED_READ_WRITE'

    @property
    def vol_dir(self):
        return "{}/{}".format(self.client.vol_dir_prefix, self.volume.name)

    @prop_loader(SourceTypes.MANAGEMENT, ['is_snapshot_ready', 'is_snapshot_attached', 'combined_io_enabled'])
    def _combined_status_loader(self):
        combined_status = f"/clients/combinedStatus/{self.client._name}/{self.volume.name}"
        err, res = self.mgmt.connection.get(combined_status)
        if err or not res: self.logger.info(f'Request for combined status failed. {err or res}')
        res = res or {}
        return {'is_snapshot_attached': res.get('isSnapshotAttached', False),
                'combined_io_enabled': res.get('combinedIOEnabled', False),
                'is_snapshot_ready': res.get('isSnapshotReady', False)}

    @classmethod
    def map_props(cls, propmap, source_type=None):
        from xlro.core.entities import Manager

        if 'vol_status' in propmap:
            vol_status = propmap.pop('vol_status')
            propmap['status'] = Attachment.STATUS_MAP.get(vol_status, str(vol_status))

        if 'is_hidden' in propmap:
            propmap['is_hidden'] = propmap.pop('is_hidden') in (1, 'hidden')

        propmap = super(Attachment, cls).map_props(propmap, source_type)

        if 'preempt' in propmap:
            propmap['preempt'] = propmap['preempt'] in [True, 1, 'Yes']

        if 'reservation' in propmap:
            reservation = propmap.pop('reservation')
            for mode, id in Manager.get_manager().reservation_modes.items():
                if id == reservation['mode']:
                    propmap['reservation_mode'] = mode
            propmap['reservation_version'] = reservation['version']
            propmap['preempt'] = reservation['preempt']

        if 'cname' in propmap and 'client' not in propmap:
            propmap['client'] = Client.instance(name=propmap.pop('cname'), mgmt=propmap.get('mgmt', Manager.get_manager()))

        if 'vname' in propmap and 'volume' not in propmap:
            propmap['volume'] = Volume.instance(name=propmap['vname'], mgmt=propmap.get('mgmt', Manager.get_manager()))

        return propmap

    @prop_loader(SourceTypes.PROC, None)
    def load_from_proc(self):
        prop_to_proc_fields = {"is_hidden": "type", "status_str": "status", "reservation_mode": "reservation",
                "status": "attach_status", "crc_enabled": "edic", "attach_blksz": "block[b]"}

        try:
            res = json.loads(self.client.proc_for_volume(self.volume.name, no_cache=True))
        except ValueError:
            self.logger.debug("load from json proc failed - will use legacy proc instead")
            return self.load_from_proc_legacy()

        # normalize nested params
        dp_flags = res.pop("dp_flags", {})
        res.update(dp_flags)

        # convert proc fields name to infra props
        for prop, pf in prop_to_proc_fields.items():
            if pf in res:
                res[prop] = res.pop(pf)

        # Complete info should only be in the nvmesh version which has attachment_status field
        if "status" not in res:
            self.logger.debug("using legacy loader due to a version without status property")
            legacy_res = self.load_from_proc_legacy()
            for k, v in legacy_res.items():
                if k not in res:
                    res[k] = v

        # See NVMESH-4595 - we can't keep up with client "attach_status", so we simplify - if proc there it's attached
        res['status'] = 'Attached'
        return res

    def load_from_proc_legacy(self):
        """
        supports also the legacy proc - should also work so order is not important
        """
        proc = self.client.proc_for_volume_legacy(self.volume.name, no_cache=True)
        match = self._PATTERN.match(proc)
        if not match:
            raise Exception("Can't parse proc fields. OUT: {}".format(proc))
        return match.groupdict()

    @prop_loader(SourceTypes.MANAGEMENT, None)
    def load_from_mgmt(self):
        for k, v in self.client.get_property('attachments', SourceTypes.MANAGEMENT, no_cache=True).items():
            if v is self: # if k == self.volume.name
                return v._property_maps[SourceTypes.MANAGEMENT]
        return {}

    @prop_loader(SourceTypes.PROC, ['debug_di'])
    def load_debug_di(self):
        return {'debug_di': bool(self.volume.use_debug_di)}

    @prop_loader(SourceTypes.PROC, ['destager'])
    def load_destager(self):
        if self.raid_type != self.MULTI_TIER_RAID:
            return {'destager': None}

        # TODO: should probably create a destager object in the future if we need to populate more info
        out, err, code = self.client.connection.execute(
            "jq -c '.\"Multi-tiered\".destager.is_suspended' {}/volumes/{}/status"
            .format(self.client.proc, self.volume.name))
        assert out in (0, 1) and not err, "Load destager failed. Got is_suspended: {}, err: {}".format(out, err)
        return {'destager': bool(int(out))}

    @prop_loader(SourceTypes.PROC, ['capacity'])
    def load_capacity(self):
        if self.client.isUmClient:
            cmd = f'sudo nvmeshum_bdev_nvmesh_rpc bdev_nvmesh_status {self.volume.name}'
        else:
            cmd = f'sudo lsblk -b | grep -w {self.vname}'
        out, err, code = Connection.execute_on_host(self.client.name, cmd)
        if code != 0:
            raise Exception('remote execution failed. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))

        if self.client.isUmClient:
            outjson = json.loads(out)
            return {'capacity': outjson['block size'] * outjson['size (blocks)']}
        else:
            return {'capacity': int(out.split()[3])}

    def find_nvmesh_version(self):
        cmd = 'cat /opt/nvmesh/' + ('nvmeshum/version' if self.client.isUmClient else 'client-repo/version')
        out, err, code = Connection.execute_on_host(self.client.name, cmd)
        if code != 0:
            raise Exception('remote execution failed. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))
        return out

    def foreign_sdk_type_conversion(self, value: Any, attr: Optional[str] = None) -> Any:
        if attr == 'status':
            return self.STATUS_MAP.get(value) or int(value)

        return super(Attachment, self).foreign_sdk_type_conversion(value, attr)

    @prop_loader(SourceTypes.PROC, ['topo'])
    def load_topo_from_proc(self):
        info = self.load_from_proc()
        if 'topo' in info:
            return {'topo': info['topo']}
        return {'topo': None}

    @prop_loader(SourceTypes.BINARY_TRACE, ['topology'])
    def load_topology_from_trace(self):
        start_t = datetime.now() - timedelta(hours=12)
        topos = ClientVolumeTopology.get_volume_topologies(self.client, self.volume, start_t, datetime.now())
        return {'topology': topos[-1] if len(topos) else None}

    def detach(self, **kwargs):
        return self.client.detach([self.volume], **kwargs)

    def set_crc(self, value: Union[int, bool]) -> None:
        # could be really cool if will have setters for props
        self.client.cli(f'#{self.volume.name}|set_read_edic={int(value)}')
        self.reset_property("crc_enabled", [SourceTypes.PROC])

    # consider using builtin 'property.setter' decorator
    def set_destager(self, value):
        # type : (int) -> None
        assert value in (0, 1), 'value must be 0 -(deactivate) or 1 -(activate)'
        self.logger.debug(f"{'activating' if value else 'deactivating'} mtv_destager_on mode")
        self.client.cli(f'#{self.volume.name}|mtv_destager_on={value}', retries=3)

    def set_max_retry_secs(self, value):
        assert value >= 0, f'max_retry_secs value must be >=0, value received: {value}'
        self.logger.debug(f"setting max_retry_secs={value} on {repr(self)}")
        self.client.cli(f'#{self.volume.name}|max_retry_secs={value}', retries=3)

    def read(self, vlba_obj: Union[LBARange, Volume.LBA], blocks: int = 1) -> Generator[PageData, None, None]:
        vlba_range = LBARange.get_by_union(vlba_obj, blocks)
        dd_cmd = "sudo dd if={dpath} iflag=direct bs={bs} skip={lba} count={n_blocks}" \
            .format(dpath=self.client.get_volume_dev_path(self.volume), bs=vlba_range.lbs.blockSize, lba=vlba_range.lbs.addr,
                    n_blocks=vlba_range.n_blocks)
        process = self.client.host.connection.popen(cmd=dd_cmd)

        while True:
            read = process.stdout.read(self.volume.BLOCK_SIZE)
            if not read:
                break
            assert len(read) == self.volume.BLOCK_SIZE, "fail reading 1 page from {}: {}".format(vlba_range, process.stderr.read())
            yield PageData(read)

        assert not process.poll(), "fail reading {} blocks from {}: {}".format(blocks, vlba_range, process.stderr.read())

    def write(self, vlba_obj: Union[LBARange, Volume.LBA], inbuf: str) -> None:
        vlba_range = LBARange.get_by_union(vlba_obj, 1)
        dd_cmd = "sudo dd of={dpath} bs={bs} seek={lba} count={n_blocks} iflag=fullblock oflag=direct" \
            .format(dpath=self.client.get_volume_dev_path(self.volume), bs=vlba_range.lbs.blockSize, lba=vlba_range.lbs.addr,
                    n_blocks=vlba_range.n_blocks)
        out, err, code = self.client.host.execute(cmd=dd_cmd, inbuf=inbuf)
        assert not code, "couldn't write to {}: {}".format(vlba_obj, err)

    def zero(self) -> None:
        dd_cmd = "sudo dd if={ifile} of={dpath} bs={bs} count={n_blocks}" \
            .format(ifile="/dev/zero", dpath=self.vol_dir, bs=self.volume.BLOCK_SIZE, n_blocks=self.volume.blocks + 1)
        out, err, code = self.client.host.execute(cmd=dd_cmd, timeout=300)
        assert not code, "couldn't zero: {}".format(err)

    def generic_counter(self, proc_path: str, dispatcher: Dict[str, Callable], no_cache: bool = True) -> dict:
        """
        We set no_cache=True by default, because this function is called by prop_loaders of counter properties and we want every call to prop_loader to really load from proc.

        To get these counter properties from cache, simply call get_property with no_cache=False there (default behavior when you get them as attributes like entity.property). That will bypass prop_loader.
        """
        out = self.client.proc_content(proc_path, no_cache)
        if not out:
            raise OSError(f'Failed to read proc {proc_path}')

        return counter_dispatcher(json.loads(out), dispatcher)

    @prop_loader(SourceTypes.PROC, ['flow_counters'])
    def _load_flow_counters(self):
        def _extract_needed_counters(item):
            ret = {'n_resets': {t: item[t]['n_resets'] for t in ['cold', 'maintenance', 'nowhole']}}
            ret.update(counter_shallow_handler(item, ['n_resets']))

            return ret

        return {'flow_counters': self.generic_counter("{}/volumes/{}/flow_cntr.json".format(self.client.proc,
                                                                                            self.volume.name),
                                                      {'syncs': _extract_needed_counters})}

    @prop_loader(SourceTypes.PROC, ['iostats_counters'])
    def _load_iostats_counters(self):
        return {'iostats_counters': self.generic_counter("{}/volumes/{}/iostats.json".format(self.client.proc,
                                                                                             self.volume.name),
                                                         {'stats': lambda x: x})}

    @prop_loader(SourceTypes.PROC, ['status_counters'])
    def _load_status_counters(self):
        dispatcher = {'io-toggles': lambda v: {'io-toggles': v},
                      'sync_stas': counter_shallow_handler,
                      'failed_io': counter_shallow_handler,
                      'resubmittion': counter_shallow_handler,
                      'internal_err': counter_shallow_handler,
                      'mgmt_alerts': counter_shallow_handler,
                      'WCV.cpr': counter_shallow_handler
                      }

        return {'status_counters': self.generic_counter("{}/volumes/{}/status.json".format(self.client.proc,
                                                                                           self.volume.name),
                                                        dispatcher)}  # type: ignore[arg-type]

    @prop_loader(SourceTypes.PROC, ['emulation_mode'])
    def _load_emulation_mode(self):
        if not self.client.isUmClient:
            return 'NONE'
        bdev_stack_cfg = self.client.host.execute(
            f'sudo nvmeshum_spdk_rpc --plugin nvmeshum.bdevs_stack nvmesh_bdevs_stack_show {self.volume.name}',
            desc='fetch bdev stack for emulation mode', success=0)[0]['config']
        return 'NONE' if not bdev_stack_cfg['is_emulated'] else 'HOTPLUG' if bdev_stack_cfg['is_hot_plug'] else 'STATIC'

    def set_emulation_mode(self, emulation_mode: EmulationMode, wait_till_completed: bool = False, **kwargs):
        return self.client.set_emulation_mode([self.volume], emulation_mode=emulation_mode,
                                              wait_till_completed=wait_till_completed, **kwargs)

class TracingType(Enum):
    TRACE_BACKEND_UNKNOWN = 0
    TRACE_BACKEND_DMESG = 1
    TRACE_BACKEND_USER = 2
    TRACE_BACKEND_KERNEL = 3

class ProcNotInitialized(Exception):
    pass

@sdk_entity(sourcetypes=[SourceTypes.PROC, SourceTypes.MANAGEMENT, SourceTypes.OS])
class Client(SDKEntity):
    CFLAGS_REGEX = re.compile(r'-D(?P<key>\w+)=?(?P<val>\w*)')
    NVMESH_ATTACH = "nvmesh_attach_volumes"
    NVMESH_DETACH = "nvmesh_detach_volumes"
    NVMEIBC_PARAMS = Host.MODULE_PATH + "/nvmeibc/parameters/"

    class HEALTH(object):
        HEALTHY = 'healthy'
        ALARM = 'alarm'
        CRITICAL = 'critical'

    name : str = PropertySpec(HostName, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    _name : str = PropertySpec(str)
    attachments : MutableMapping[str, Attachment] = PropertySpec({'vname': Attachment})
    all_attachments : MutableMapping[str, Attachment] = PropertySpec({'vname': Attachment})
    lbs : int = PropertySpec(int)
    tracing_type : TracingType = PropertySpec(TracingType)
    version : str = PropertySpec(str)
    health : str = PropertySpec(str)
    nvmf_attached_volumes : List[str] = PropertySpec([str])
    nvmeiba_counters : dict = PropertySpec(dict)
    uuid : str = PropertySpec(str)
    inst_idx : int = PropertySpec(int)
    binje : int = PropertySpec(int)
    isUmClient : bool = PropertySpec(bool)


    # UM-Client additions
    rpc_lock = Lock()
    nbd_bdevs: MutableMapping[Attachment, str] = PropertySpec({Attachment: str})
    max_nbd: int = PropertySpec(int)
    max_ublk: int = PropertySpec(int)
    expose_method = 'auto_expose'
    snap_recreate_controller = expose_method == 'snap' and infra_conf.root.nvmeshum.snap_recreate_controller
    io_queues = infra_conf.root.nvmeshum.io_queues
    io_queue_size = infra_conf.root.nvmeshum.io_queue_size
    _supports_auto_expose = None

    def __init__(self, *args, **kwargs):
        if 'isUmClient' not in kwargs:
            kwargs['isUmClient'] = self._check_if_um(**kwargs)
        super(Client, self).__init__(*args, **kwargs)
        self.logger.debug(f'New Client: {self.name}: UM? {self.isUmClient}')
        mc_info = self._client_name_to_mc_info()
        self.client_node_name: str = mc_info[0]
        self.sub_name: str = mc_info[1]
        self._subdir_ag: str = ""
        # proc and vol_dir_prefix moved to properties, to delay execution until after install.

        # UM-Client
        self.lvol_uuid_to_vname = {}
        self.nvme_prefix = ""
        self.ec_support = infra_conf.root.nvmeshum.um_support_ec

        # External-Client
        self.is_setup: bool = False
        self.bindings: Dict[Volume, Any] = {}
        self.nvmesh_client = self #UMC: Get rid of this
        self.xc_lock = Lock()  #UMC: What's this for?  Do we need here?

    @classmethod
    def _check_if_um(cls, **kwargs) -> bool:
        ''' If needed, determine if we are UM Client '''
        from xlro.core.entities import Manager
        from xlro.core.entities.sdk_base import MongoComparison
        from xlro.core.util.general_utils import print_current_traceback

        cname = kwargs['name']
        cls.logger.debug(f'Checking if {cname}/{kwargs.get("_name")} is a UM Client ({kwargs})')
        try:
            # See if explicit
            is_um = bool(int(kwargs['isUmClient']))
            cls.logger.debug(f'Explicit: um={is_um}')
            return is_um
        except Exception as e:
            cls.logger.debug(f'Not explicit isUmClient flag for {cname}: {repr(e)}')

        try:
            # See if configured (usually only in slash env)
            umnodes = set(infra_conf.root.cluster.umnodes)
            aliases = set(host_aliases(cname))
            is_um = bool(umnodes & aliases)
            cls.logger.debug(f'Cluster config found for {cname}: um={is_um}')
            return is_um
        except Exception as e:
            cls.logger.debug(f'No cluster config for {cname}: {repr(e)}')

        try:
            # Try via manager - assumes known and connected
            mgr = kwargs.get('mgmt') or Manager.get_manager()
            dbkey = Client.cls_rest_info(mgr=mgr).dbkey
            projection = [MongoObj('isUmClient', 1)]
            # _name is the management's official name for the client, if not available, use any/all aliases
            _name = kwargs.get("_name")
            if _name:
                selection = [MongoObj(dbkey, _name)]
            else:
                selection = [MongoObj(dbkey, MongoComparison.get_in_query_val(host_aliases(cname)))]
            props = next(Client._sdk_get(mgmt=mgr, count=1, filter_mongo_objs=selection, projection_mongo_objs=projection))
            is_um = bool(props.get('isUmClient', False))
            cls.logger.debug(f'Management response for {cname}: um={is_um}')
            return is_um
        except Exception as e:
            cls.logger.debug(f'Failed to get {cname} via Manager. {repr(e)}')

        print_current_traceback(f'WARNING: Cannot determine Kernel vs. UserMode client. {kwargs}', cls.logger)
        # Removed the test via nvmeshum service on host, which requires SSH

        cls.logger.info(f'Cannot determine Kernel vs. UserMode client. {kwargs}')
        return False

    @property
    def supports_auto_expose(self):
        if self._supports_auto_expose is None:
            # Check if ublk_drv is loaded and set to auto_expose if it is
            _, _, code = self.client_node.host.execute('lsmod | grep -q ublk_drv')
            self._supports_auto_expose = code == 0
        return self._supports_auto_expose

    def _find_subdir_ag(self):
        if self._subdir_ag:
            return self._subdir_ag

        output = self.client_node.get_multi_instance_json()
        self.logger.debug('Mgmt UUID: {}, MultiInstance JSON: {}'.format(self.mgmt.uuid, str(output)))
        for inst in output['instances']:
            if self.mgmt.uuid and inst['management']['db_uuid'] == self.mgmt.uuid.replace('-', '') and inst['auto_generated'] == "1":
                # TODO: Multi-instance: re-examine caching
                # self._subdir_ag = inst['dev_dir']
                # return self._subdir_ag
                return inst['dev_dir']

        # Note: anyone using self.proc or self.vol_dir_prefix must deal with this
        # Seems like a problem, if someone wants to now the directory before an attachment happened...
        raise ProcNotInitialized("Couldn't find subdir for {}".format(self))

    @property
    def cmp_blocks_path(self):
        return '/opt/nvmesh/nvmeshum/bin/cmp_blocks' if self.isUmClient else '/opt/nvmesh/perfTest/io_stress/cmp_blocks/cmp_blocks'

    @property
    def parse_blocks_path(self):
        return "/opt/nvmesh/nvmeshum/bin/parse_block" if self.isUmClient else "/opt/nvmesh/perfTest/io_stress/di_parser/parse_block"

    def exec_vlba_trace(self, volume, vlba):
        if not self.isUmClient:
            return ('', 'Not a UM Client', -1)
        try:
            cmd_elements = ["sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_vlba_trace.py", "--volume", str(volume.name), "--vlba", str(vlba.addr),
                   "--slice_size", str(volume.dataBlocks)]
            cmd = " ".join(cmd_elements)
            return self.host.connection.execute(cmd, success=0, timeout=120)
        except Exception as e:
            return ('', 'Cmd failed. repr(e)', -1)

    def di_stop(self, volume: Volume, vlba_s: str):
        self.cli(f'#{volume.name}|di_bug_on_addr{vlba_s}')

    @property
    def proc(self):
        return '/proc/' + (self.sub_name or self._find_subdir_ag() if self.client_node.is_multi_instance else 'nvmeibc')

    @property
    def vol_dir_prefix(self):
        return '/dev/' + (self.sub_name or self._find_subdir_ag() if self.client_node.is_multi_instance else 'nvmesh')

    def vol_path(self, volume: Volume) -> str:
        return os.path.join(self.vol_dir_prefix, volume.name)

    @prop_loader(SourceTypes.PROC, ['health'])
    def load_unregistered_from_proc(self):
        ''' Need "proc" loader for mgmt-provided properties of unregistered clients '''
        # TODO: I assume we'll find other needed properties, which should go here
        return { 'health':
            self.HEALTH.HEALTHY if self.client_node.services['client'].status() == 0 else self.HEALTH.ALARM}

    @prop_loader(SourceTypes.PROC, ['attachments'])
    def load_attachments_proc(self):
        if self.isUmClient:
            out, _, _ = self.connection.execute(
                "sudo nvmeshum_spdk_rpc nvmesh_list_class_objects --class volume | grep -Po '\"name\": *\"\K[^\"]*'",
                success=0, desc='RPC list of attached volumes')
        else:
            try:
                procdir = self.proc
            except ProcNotInitialized:
                self.logger.debug('No proc initialized, so returning empty attachments.')
                return {'attachments': {}}
            out, _, _ = self.connection.execute(f'cd {procdir}/volumes && ls', success=0)

        # not sure what the '['s are for
        # e_ and d_ are encryption shadow volumes and to be ignored.
        vnames = [vn for vn in out.split() if '[' not in vn and not vn.startswith('e_') and not vn.startswith('d_')]
        return {'attachments': {vname: dict(client=self, vname=vname) for vname in vnames}}

    @prop_loader(SourceTypes.MANAGEMENT, ['attachments'])
    def load_unregistered_from_mgmt(self):
        ''' Property specific loader runs before self.load_from_mgmt_sdk().  Check for no entity yet. '''
        sdk_dict = super(Client, self).load_from_mgmt_sdk()
        self.logger.debug('fallback for loading from sdk_mgmt. Found? {}'.format(bool(sdk_dict)))
        return sdk_dict or {'attachments': {}}

    def proc_for_volume(self, volume_name: str, no_cache: bool = False) -> str:
        return self.proc_content('{}/{}/status.json'.format('volumes', volume_name), no_cache)

    # Multi-instance: This should be hidden inside proc_for_volume, IMO
    def proc_for_volume_legacy(self, volume_name: str, no_cache: bool = False) -> str:
        """
        we still support normal proc for older versions
        """
        return self.proc_content('{}/{}/status'.format('volumes', volume_name), no_cache)

    @prop_loader(SourceTypes.MANAGEMENT, ['_name'])
    def get_rest_name(self):
        # Infra standardized name may not me management name.  So look for a match on any alias.
        try:
            aliases = host_aliases(self.name)
            return {'_name': list(self.get_headlines(count=1, query={'clientID': aliases}).values())[0]._name}
        except Exception as e:
            self.logger.info(f'Cannot get aliases for {self.name}.  {repr(e)}')
            return {'_name': self.name}

    @property
    def rest_id(self):
        # If we've never been loaded from Mgmt, we have to search for the correct name
        return self.get_property('_name', SourceTypes.MANAGEMENT)

    @classmethod
    def map_props(cls, propmap, source_type=None):
        from xlro.core.entities import Manager

        if source_type == SourceTypes.MANAGEMENT:
            client_sdk_name = propmap.get('clientId', propmap.get('_id'))
            if client_sdk_name:
                propmap.setdefault('_name', client_sdk_name)
                propmap.setdefault('name', client_sdk_name)
        propmap = super(Client, cls).map_props(propmap, source_type)

        try:
            name = propmap['name']
            orig_host = name.partition('_')[0]
            full_host = host_name(orig_host)
            if full_host != orig_host:
                propmap['name'] = name.replace(orig_host, full_host, 1)
        except:
            pass
        # adjust the attachments to contain client instance
        if source_type == SourceTypes.MANAGEMENT:
            # Note: super() will convert ['block_devices'] to ['allattachments'] via rest.yaml:Attachment.rest2infra
            if 'all_attachments' in propmap and not isinstance(propmap['all_attachments'], Mapping):
                # Filter non-attached out of list
                att_list = [a for a in propmap['all_attachments'] if a['vol_status'] not in Attachment.DETACHED_STATUSES]
                # Map fields
                att_map = {a['name']: Attachment.map_props(a.copy(), source_type) for a in att_list}
                # Populate 'client' as needed
                for att_dict in list(att_map.values()):
                    if not ('client' in att_dict or 'cname' in att_dict):
                        # needed to not cause dependency loop of between Client.instance and Attachments.instance
                        att_dict['client'] = Client.instance(name=propmap['name'], mgmt=propmap.get('mgmt',
                                                                                                    Manager.get_manager()))
                propmap['all_attachments'] = att_map
                propmap['attachments'] = {vname: at for vname, at in att_map.items() if '[' not in vname}

        return propmap

    def cli(self, cmd, timeout=10, retries=1, delay=3):
        self.logger.info('CLI <{}> {}'.format(self.name, cmd))
        if not self.isUmClient:
            fullcmd = '''sudo bash -c "echo -ne '{}' > {}/cli/cli"'''.format(cmd, self.proc)
            return self.connection.tolerant_exec(fullcmd, timeout, retries, delay)
        else:
            match = re.match(r'#(?P<vname>[^|]*)\|(?P<ioctl>[^=]*)=*(?P<value>.*)', cmd)
            if match:
                self.logger.warning(f'CMD: {cmd}, MATCH: {match.groupdict()}')
                subcmd = None
                vname = match.groupdict().get('vname') or ''
                ioctl = match.groupdict().get('ioctl') or ''
                value = match.groupdict().get('value') or ''
                if ioctl == 'set_read_edic':
                    subcmd = 'dp_skip_edic_verification'
                    value = int(not bool(int(value)))
                elif ioctl == 'max_retry_secs':
                    subcmd = 'nbio_timeout_ms'
                    value = int(value) * 1000
                elif ioctl.startswith('di_bug_on_addr'):
                    subcmd = 'report_di'
                    value = ioctl[len('di_debug_addr'):]
                if subcmd:
                    rpc_cmd = infra_conf.root.tools.nvmeshum_rpc
                    return self.connection.execute(f'{rpc_cmd} nvmesh_volume_ioctl --name {vname} --cmd {subcmd} --param {value}')
            # Known unsupported CLI:
            # mtv_destager_on
            # clear_profilers
            # recov_launch ...
            raise Exception(f'CLI {cmd} not supported for UM Client {self.name}')

    def set_emulation_mode(self, volumes: Iterable[Volume], emulation_mode: EmulationMode, wait_till_completed: bool = False, timeout=None):
        res = self.do_operation(self.mgmt, 'setEmulationMode', [self.name], timeout=timeout,
                                volumes=volumes, emulation_mode=emulation_mode)

        if wait_till_completed:
            Attachment.bulk_wait_for_attachment_values(
                {self: volumes}, 'emulation_mode', [emulation_mode], timeout=timeout).assert_result(
                f'Failed to set emulation mode after {timeout}s')

        return res

    @entity_method
    def attach(self,
               volumes: Iterable[Volume],
               wait_till_completed: bool = False,
               reservation_mode: str = None,
               reservation_version: Optional[int] = None,
               preempt: bool = False,
               is_detach_others: bool = False,
               emulation_mode: Optional[EmulationMode] = None,
               reference_id: Optional[str] = None,
               **kwargs: Any) -> Optional[WaitResult]:
        volumes = list(volumes)
        for v in volumes:
            assert v.mgmt == self.mgmt, "can't IO from {} to {} - different mgmts".format(v, self.mgmt)

        if self.mgmt.use_rest_attach:
            result = self.do_operation(self.mgmt,
                                       'attach',
                                       [self.name],
                                       volume=volumes,
                                       mode=reservation_mode,
                                       reservation_version=reservation_version,
                                       preempt=preempt,
                                       is_detach_others=is_detach_others,
                                       emulation_mode=emulation_mode,
                                       reference_id = reference_id)
            assert result is not None, 'Attaching via REST API did not return success for all volumes'
            if not all([r['success'] for r in result]):
                raise Exception('Attaching via REST API did not return success for all volumes', result)
        else:
            vnames = [vol.name for vol in volumes]
            self.logger.info("attaching volumes - {} from {}".format(vnames, self))

            _vnames = [vol.name for vol in volumes]
            mgmt = volumes[0].mgmt

            cluster = ",".join(["{}:{}".format(h.name, mgmt.WEB_SOCKET_PORT) for h in mgmt.mgmt_hosts])
            attach_str = "sudo {cmd} {json} {mcargs} {exlusive} {preempt} {vnames} {inst} {cluster}".format(
                cmd=self.NVMESH_ATTACH,
                mcargs=self.get_mc_attach_str(),
                exlusive=self.get_exclusive_attach_mode_str(reservation_mode),
                preempt="--preempt" if preempt else "",
                json='--json' if self.client_node.attach_supports_json else '',
                vnames=" ".join(vnames),
                inst='-i ' + self.sub_name if self.client_node.is_multi_instance and self.sub_name else '',
                cluster="--management_cluster {}".format(cluster) if self.client_node.is_multi_instance else ''
            )
            out, err, code = self.connection.tolerant_exec(attach_str, timeout=180, retries=3, log_level='warning')
            self.logger.info("'{}' result - {}".format(self.NVMESH_ATTACH, out))

        for vol in volumes:
            # when run within parallel, the race between clients might effect the 'last_attached_client's result -
            # his should be problematic
            if not vol.last_attached_client:
                vol.reset_loaders_called_cache([SourceTypes.PROC])
            vol.last_attached_client = self # type: ignore[assignment] ### MUST figure this out one day

        if wait_till_completed:
            return self.wait_for_attach(volumes, reference_id=reference_id, **kwargs)
        return None

    def volumes_in_use(self, volnames: Optional[List[str]] = None) -> List[str]:
        ''' Return list of volumes that are attached, but "in use", for which detach should fail '''
        if self.isUmClient:
            out, _, _ = self.connection.execute("sudo lsof /dev/nvmesh/*; mount) | grep -Po '/dev/nvmesh/\K[^ ]*'")
        else:
            out, _, _ = self.connection.execute(
                'cd /proc/nvmeibc/volumes && grep -sl "^Num Proc.*opens=[^0]" */client_processes | cut -f1 -d/')
        in_use = set(out.split())
        if volnames:
            in_use &= set(volnames)
        if not in_use:
            return []

        # Log who is using in-use vols
        in_use_procs = f'{{{",".join(in_use)}}}' if len(in_use) > 1 else list(in_use)[0]
        if self.isUmClient:
            grep_pattern = "|".join(in_use)
            out, _, _ = self.connection.execute(
                f"sudo lsof /dev/nvmesh/{in_use_procs}; mount | grep -E '/dev/nvmesh/({grep_pattern})'")
        else:
            out, _, _ = self.connection.execute(
                f'cd /proc/nvmeibc/volumes && grep -Hs "^ *[0-9]*)" {in_use_procs}/client_processes')
        self.logger.info(f'VOLS IN USE: {list(in_use)}\n{out}')
        return list(in_use)

    @entity_method
    def detach(self, volumes: Iterable[Volume], wait_till_completed: bool = False, force: bool = False,
           reference_id: Optional[str] = None, **wait_kwargs: Any) -> Optional[WaitResult]:
        volumes = list(volumes)
        for v in volumes:
            assert v.mgmt == self.mgmt, f"Can't detach {v.name} on {v.mgmt} from {self.name} on {self.mgmt}"

        vnames = [vol.name for vol in volumes]
        if not force:
            if not wait_for_it(lambda: not self.volumes_in_use(vnames)):
                raise Exception(f"Volumes in use on {self._name}: {self.volumes_in_use(vnames)}. Cannot detach.")

        if self.mgmt.use_rest_attach:
            result = self.do_operation(self.mgmt, 'detach', [self.name], volume=volumes,
                    reference_id=reference_id, force=force)
            assert result is not None, 'Detaching via REST API did not return a result'
            if not all([r['success'] for r in result]):
                raise Exception('Detaching via REST API did not return success for all volumes', result)
        else:
            mgmt = volumes[0].mgmt
            cluster = ",".join(["{}:{}".format(h.name, mgmt.WEB_SOCKET_PORT) for h in mgmt.mgmt_hosts])
            self.logger.info("detaching volumes - {} from {}".format(vnames, self))
            # Since we're getting sporadic timeouts, and we can't control how many retries
            # nvmesh_detach_volumes does (currently 5 * 1 sec) we'll do our own retry.
            # Also, nvmesh_detach_volumes doesn't have a proper exit code so we use 'ls' to check
            cmd = 'sudo {cmd} {force} {vnames} {mc_extra} {cluster}; cd {devdir}; test -z "$(ls {vnames} 2>/dev/null)"'.format(
                cmd=self.NVMESH_DETACH, force='-f' if force else '',
                mc_extra=self.get_mc_attach_str(), devdir=self.vol_dir_prefix, vnames=' '.join(vnames),
                cluster="--management_cluster {}".format(cluster) if self.client_node.is_multi_instance else '')

            try:
                out, err, code = self.connection.tolerant_exec(cmd, timeout=180, retries=3, log_level='warning')
            except Exception as e:
                self.logger.info(f'Attempt to detach failed - {repr(e)}')

        if wait_till_completed:
            return self.wait_for_detach(volumes, reference_id=reference_id, **wait_kwargs)
        return None

    def wait_for_attach(self, volumes: Iterable[Volume], **kwargs: Any) -> WaitResult:
        return Client.bulk_wait_for_attachments_status({self: volumes}, True, ['Attached'], **kwargs)

    def wait_for_detach(self, volumes: Iterable[Volume], **kwargs: Any) -> WaitResult:
        return Client.bulk_wait_for_attachments_status({self: volumes}, True, ['Detached'], **kwargs)

    @classmethod
    def bulk_wait_for_attachments_status(cls, clients_volumes, is_in_statuses, statuses, **kwargs):
        return cls.bulk_wait_for_attachment_values(clients_volumes, 'status', statuses, non_values=['Detached'], **kwargs)

    @classmethod
    def bulk_wait_for_attachments_io_enabled(cls, clients_volumes, **kwargs):
        return cls.bulk_wait_for_attachment_values(clients_volumes, 'io_enabled', [True], is_matching=True, non_values=[False], **kwargs)

    @classmethod
    def bulk_wait_for_attachments_io_not_enabled(cls, clients_volumes, **kwargs):
        return cls.bulk_wait_for_attachment_values(clients_volumes, 'io_enabled', [False], is_matching=True, non_values=[False], **kwargs)

    @classmethod
    def _refresh_func(cls, source: str, clients_volumes: Dict,
                        strip_hidden: bool = False, reference_id: Optional[str] = None, ignore_refs=False):
        ''' This is used to fetch the relevant attachments for checking property values.
            Need to possibly strip hidden.
            To support multi-attach, need to strip attachments without matching reference-id
        '''
        is_multi_attach = False if ignore_refs else None
        relevant_attachments: list[Attachment] = []
        # Breaking up the huge one-liner which was already unreadable before ref-id support...
        for c in clients_volumes:
            if is_multi_attach is None:
                is_multi_attach = c.rest_feature('multi-attach') or False
            for a in c.get_property('attachments', source=source, no_cache=True).values():
                if (not strip_hidden or not a.is_hidden) \
                        and (not is_multi_attach or str(reference_id or a.volume.uuid) in a.referenceIDs):
                    relevant_attachments.append(a)
        return {a.key(): a for a in relevant_attachments}

    @classmethod
    def bulk_wait_for_attachment_values(cls, clients_volumes, prop_name, values, source=SourceTypes.MANAGEMENT,
                    strip_hidden=True, reference_id=None, **kwargs):
        # Build attachment list from clients_volumes map
        attachments = [Attachment.instance(client=client if type(client) == Client else Client.instance(name=client.name), volume=volume) for client, volumes in clients_volumes.items() for volume in volumes]
        # _refresh_func filters the attachments to consider to only relevant ones (hidden, ref_id)
        refresh_func = lambda: cls._refresh_func(source, clients_volumes, strip_hidden, reference_id,
                            # If not checking status and no explicit ref-id, ignore ref-id.
                            # It was causing troubles for externally attached (k8) check for io-enabled
                            ignore_refs=not reference_id and prop_name != 'status')
        result = wait_for_property_values(attachments, prop_name, values, source=source, refresh_func=refresh_func,
                                          **kwargs)
        if not result.result:
            try:
                v = re.search(r'Volume:.+?(?=:)', result.metadata).group().split(':')[1]
                c = re.search(r'Client:.+?(?=:)', result.metadata).group().split(':')[1]
                try:
                    clnt = [a.client for a in attachments if a.vname == v and a.client.name == c][0]
                    if clnt.isUmClient:
                        cls.logger.debug(f'Volume {v} on UM client {c} not in {prop_name}=={values}, '
                                         f'printing status from nvmeshum_bdev_nvmesh_rpc')
                        # TODO should probably be called by um_client.execute_rpc once nvmeshum_bdev_nvmesh_rpc is supported
                        cls.logger.debug(clnt.host.execute(cmd=f'sudo nvmeshum_bdev_nvmesh_rpc bdev_nvmesh_status {v}')[0])
                    else:
                        cls.logger.debug(f'Volume {v} on kernel client {c} not in {prop_name}=={values}, '
                                         f'printing status from proc')
                        cls.logger.debug(clnt.host.execute(cmd=f'cat /proc/nvmeibc/volumes/{v}/status')[0])
                except Exception as e:
                    cls.logger.debug(f'Failed to read proc for volume {v} on client {c} - {repr(e)}')
            except Exception as parse_e:
                cls.logger.debug(f'Failed to parse result metadata {result.metadata}')
        return result

    def foreign_sdk_type_conversion(self, value: Any, attr: Optional[str] = None) -> Any:
        if attr == 'attachments':
            return list(map(Attachment.to_foreign_sdk_entity, list(value.values())))

        return super(Client, self).foreign_sdk_type_conversion(value, attr)

    @prop_loader(SourceTypes.PROC, ['lbs', 'tracing_type'])
    def _load_cflags(self):
        cflags_dict = self.get_cflags()
        return {'lbs': 1 << int(cflags_dict['NVMEIBC_SECTOR_SHIFT']),
                'tracing_type': self._tracing_type_match(cflags_dict)}

    def get_cflags(self):
        # cflags is in module proc, vs. client.proc
        cflags = self.client_node.proc_content('/proc/nvmeibc', 'cflags')
        return self._create_cflags_dict(cflags)

    def _create_cflags_dict(self, cflags):
        return {m.group('key'): m.group('val') for m in re.finditer(self.CFLAGS_REGEX, cflags)}

    @staticmethod
    def _tracing_type_match(cflags_dict):
        matcher = [('_NVMEIB_TRACE_BACKEND_DMESG', TracingType.TRACE_BACKEND_DMESG),
                   ('_NVMEIB_TRACE_BACKEND_USER', TracingType.TRACE_BACKEND_USER),
                   ('_NVMEIB_TRACE_BACKEND_KERNEL', TracingType.TRACE_BACKEND_KERNEL)]

        for s, e in matcher:
            if s in cflags_dict:
                return e

        return TracingType.TRACE_BACKEND_UNKNOWN

    @classmethod
    def get_toma_client_conversations(cls, client: "Client", volume: "Volume", start_time: datetime = None, end_time: datetime = None) -> List[TraceEntry]:

        filter_query = "({vname}) && ({funcs}) && ({multi_instance})".format(
            vname=PagerUtils.get_field_eq('DEV_NAME', volume.name),
            funcs=" || ".join([PagerUtils.get_func_filer('__block_toma_msg_handler'),
                                 PagerUtils.get_func_filer('____send_combined_msg')]),
            multi_instance=PagerUtils.get_multi_instance_idx_filter(client.inst_idx))

        time_query = PagerUtils.get_time_query(start_time, end_time, client.host)
        all_trace_entries = PagerUtils.get_pager_results(client.host, filter_query=filter_query, time_query=time_query)
        return all_trace_entries

    def get_rionics_paths_by_drive(self, drive):
        ###  I send my apologies from the past to anyone who will try to change this in the future  (-) Dori
        # Matches the whole RIONIC section, from "RIONIC" to "> Total per-path". The 2nd regex is just the GID part.
        rionic_header_rgx = r'(?P<rionic>RIONIC[\s\S]+?(?=\s*> Total per-path))'
        rionic_gid_rgx = r'[0-9a-f:]{39}'
        # Matches each LIONIC line and gets HW-GID and Device
        lionic_header_rgx = r'.+?(?<=HW-GID: )(?P<hwgid>[0-9a-f:]{39}).+?(?<=Device: )(?P<device>\S+)'
        # Matches each IO channel line and gets index, In-Use, local GID, remote GID and State
        channel_header_rgx = r'- NO-RDDA IO CHANNEL (?P<idx>\d+).+?In-Use: (?P<in_use>\S+(?=\,))[\s\S]+?(?<=\([0-9a-f]{16}\)\s)(?P<lgid>[0-9a-f:]{39}).+?(?<=-> )(?P<rgid>[0-9a-f:]{39}).+?(?<=State: )(?P<state>\S+)'
        rionics = []

        drive_status = self.proc_content(f'disks/{drive.name}/status', True)
        rns = re.findall(rionic_header_rgx, drive_status)
        for r in rns:
            r_hw_gid = re.search(rionic_gid_rgx, r).group()
            lionics = []
            lns = r.split('LIONIC')[1:]
            for ln in lns:
                l_hw_gid, device = re.findall(lionic_header_rgx, ln)[0]
                chs = re.findall(channel_header_rgx, ln)
                channels = []
                for c in chs:
                    channels.append(IOChannel(idx=c[0], in_use=c[1], state=c[4], local_gid=c[2], remote_gid=c[3]))
                lionics.append(LIONic(hw_gid=l_hw_gid, device=device, channels=channels))
            rionics.append(RIONic(hw_gid=r_hw_gid, lionics=lionics))

        return rionics

    def get_lock_channels_by_drive(self, drive, no_cache=False):
        output = self.proc_content('disks/{0}/status.json'.format(drive.name), no_cache)
        # add missing comma to turn proc output into a valid json
        fixed_output = re.sub(r'}\n{1}\t{3}{', '},\n\t\t\t{', output)
        j_output = json.loads(fixed_output)[drive.name]

        return [LockChannel(lock_chan['net']['state'],
                            lock_chan['net']['local']['gid'],
                            lock_chan['net']['remote']['gid']) for lock_chan in j_output['lock_ch']]

    def get_admin_channels_by_drive(self, drive, no_cache=False):
        output = self.proc_content('disks/{0}/status.json'.format(drive.name), no_cache)
        # add missing comma to turn proc output into a valid json
        fixed_output = re.sub(r'}\n{1}\t{3}{', '},\n\t\t\t{', output)
        j_output = json.loads(fixed_output)[drive.name]

        return [AdminChannel(admin_chan['net']['state'],
                             admin_chan['net']['local']['gid'],
                             admin_chan['net']['remote']['gid']) for admin_chan in j_output['admin_ch']]

    def get_client_parameter(self, parameter):
        return Connection.err2exc(self.connection.execute("cat {}{}".format(self.NVMEIBC_PARAMS, parameter)))

    def set_client_parameter(self, parameter, value):
        return Connection.err2exc(self.connection.execute(
            "sudo sh -c \"echo {0} > {1}{2}\"".format(value, self.NVMEIBC_PARAMS, parameter)))

    def _client_name_to_mc_info(self) -> Tuple[str, str]:
        m = re.match(r"(?P<client_node>\S+)?_(?P<mc_id>\S+)", self.name)
        if m:
            return m.groupdict()['client_node'], m.groupdict()['mc_id']
        return self.name, ""


    def get_mc_attach_str(self) -> str:
        if self.sub_name:
            number_only = re.match(r"mc(\d+)", self.sub_name)
            if number_only:
                return "--mc {}".format(number_only.group(1))
        return ""

    @classmethod
    def get_exclusive_attach_mode_str(cls, reservation_mode: Optional[str]) -> str:
        if reservation_mode and reservation_mode is not Attachment.NONE:
            return "--access {}".format(reservation_mode)
        return ""

    _client_node: Optional['ClientNode'] = None

    def client_service(self) -> 'Service':
        c_n = ClientNode.instance(name=self.client_node_name)
        return c_n.services.get('nvmeshum', None) if self.isUmClient else c_n.services.get('client', None)

    @property
    def client_node(self) -> 'ClientNode':
        if not self._client_node:
            self._client_node = ClientNode.instance(name=self.client_node_name)
        return self._client_node

    # TODO - same method as in MgmtHost, consider merging
    @prop_loader(SourceTypes.OS, ['version'])
    def load_version_from_proc(self):
        return {'version': self.client_node.get_property("version", no_cache=True)}

    _local_client: Optional['Client'] = None
    c_lock: Lock = Lock()

    @classmethod
    def local_client(cls):
        """ For the use of local utilities, use local client vs some random remote client if possible """
        from xlro.core.entities import Manager

        if not cls._local_client:
            with cls.c_lock:
                if not cls._local_client:
                    clients = Manager.get_manager().clients
                    local_hostname = Connection.localhostname()
                    cls._local_client = Client.instance(name=local_hostname) if local_hostname in [c.name for c in clients] else clients[0]
        return cls._local_client

    @property
    def host(self):
        if self.isUmClient:
            return Host.instance(name=self.get_hostname())
        return self.client_node.host

    # This seems a legit convenience. No reason to deprecate, IMO.
    # @deprecated(Deprecate.ToBeReplaced(NvNode.connection))
    @property
    def connection(self) -> Connection:
        return self.client_node.host.connection

    @deprecated(Deprecate.ToBeReplaced(NvNode.reboot))
    def reboot(self, force_level=0, wait=True):
        return self.host.reboot(force_level, wait)

    @deprecated(Deprecate.ToBeReplaced(NvNode.ipmi))
    def ipmi(self, cmd, wait=True):
        return self.host.ipmi(cmd, wait)

    def proc_content(self, proc_path, no_cache=False, *args, **kwargs):
        return self.client_node.proc_content(self.proc, proc_path, no_cache, *args, **kwargs)

    @property
    def node(self):
        return self.client_node.node

    @prop_loader(SourceTypes.PROC, ['nvmeiba_counters'])
    def _load_nvmeiba_counters(self):
        j = json.loads(self.connection.execute("cat /proc/nvmeiba/status.json")[0])

        ret = counter_dispatcher(j, {'counters': counter_shallow_handler,
                                     'atoms': lambda v: {'num_opens': {item['name']: item['num_opens'] for item in v},
                                                         'status': {item['name']: item['status'] for item in v}}})
        return {'nvmeiba_counters': ret}

    @prop_loader(SourceTypes.PROC, ['uuid'])
    def _load_client_status(self):
        try:
            out = self.proc_content('status.json', True)
            return json.loads(out)['main']
        except OSError:
            pattern = r'UUID: (?P<uuid>\S+)\,'
            match = re.search(pattern, self.proc_content('status', True))
            assert match, "could not match status regex"
            return match.groupdict()

    @prop_loader(SourceTypes.PROC, ['inst_idx'])
    def _load_inst_idx(self):
        if not self.client_node.is_multi_instance:
            return {'inst_idx': 0}

        output = self.client_node.get_multi_instance_json()
        for inst in output["instances"]:
            if self.sub_name:
                # not autogenerated so just find the same dir
                if inst["dev_dir"] == self.sub_name:
                    return {'inst_idx': inst['index']}
            else:
                # autogen
                if self.mgmt.uuid and inst['management']['db_uuid'] == self.mgmt.uuid.replace('-', '') and inst['auto_generated'] == "1":
                    return {'inst_idx': inst['index']}

        raise Exception("Could not match ")

    @prop_loader(SourceTypes.PROC, ['binje'])
    def _load_binje(self):
        try:
            binje = int(self.get_client_parameter('nvmeibc_jentry_num_blocks'))
        except:
            binje = 1

        return {'binje': binje}

    @staticmethod
    def get_mod_param_conf_file_name(param):
        return '/etc/modprobe.d/nvmeshZZZInfra_{}.conf'.format(param)

    def unset_nvmeibc_param(self, param, restart_client=True):
        cmd = "sudo rm {}".format(self.get_mod_param_conf_file_name(param))
        out, err, code = self.connection.execute(cmd)
        assert not code, 'Failed removing configuration for {} param: {}'.format(param, err)
        if restart_client:
            self.host.services['client'].restart()

    def set_nvmeibc_param(self, param, value, restart_client=True):
        if int(self.get_client_parameter(param)) == value:
            return

        cmd = "sudo sh -c 'echo options nvmeibc {}={} >> {}'".format(param, value, self.get_mod_param_conf_file_name(param))
        out, err, code = self.connection.execute(cmd)
        assert not code, 'Failed setting nvmeibc param: {}'.format(err)
        if restart_client:
            self.host.services['client'].restart()
            wait_for_it(lambda: self.health == self.HEALTH.HEALTHY).assert_result()

        assert int(self.get_client_parameter(param)) == value, 'nvmeibc param was not changed correctly after service restart'

    def load_attached_drives(self):
        try:
            return self.connection.err2exc(self.connection.execute("ls {}/disks/".format(self.proc))).split()
        except ProcNotInitialized:
            return []

    @property
    def attached_io_enabled(self):
        return [n for n, a in self.attachments.items() if a.io_enabled]

    @property
    def attached_io_disabled(self):
        return [n for n, a in self.attachments.items() if not a.io_enabled and not a.is_hidden]

    @property
    def attached_recovery(self):
        return [n for n, a in self.all_attachments.items() if a.is_hidden]

    def get_mon_cls(self, io_tools='fio'):
        from xlro.core.util.io_monitors import IOCmdMonitor, FioPidMonitor
        if io_tools == 'fio':
            return FioPidMonitor
        return IOCmdMonitor

    def get_volume_dev_path(self, volume):
        return path.join(self.vol_dir_prefix, volume.name)

    def get_hostname(self):
        return self.client_node.name

    # UM-Client additions
    def _execute_rpc(self, cmd, allow_failure=False, snap_rpc=False):
        ''' Gets raw response without JSON unmarshalling '''
        rpc_cmd = infra_conf.root.tools.nvmeshum_rpc if not snap_rpc else 'nvmeshum_snap_rpc'
        return self.client_node.host.execute(f'sudo {rpc_cmd} {cmd}', success=None if allow_failure else 0)[0]

    def execute_rpc(self, cmd, allow_failure=False, snap_rpc=False):
        out = self._execute_rpc(cmd, allow_failure, snap_rpc)
        try:
            jout = json.loads(out)
            if isinstance(jout, dict) and 'result' in jout:
                return jout['result']['response']
            else:
                return jout
        except Exception:
            return out

    def run_nvmesh_operation_rpc(self, cls, id, oper_type, by_name=False, rest=""):
        identify_param = "name" if by_name else "id"
        output = self.execute_rpc(f"nvmesh_rpc_operation --class {cls} --{identify_param} {id} --op_type {oper_type} {rest}")
        return output

    @prop_loader(SourceTypes.PROC, ['max_nbd'])
    def _max_nbd_loader(self):
        out, err, code = self.client_node.host.execute('cat /sys/module/nbd/parameters/nbds_max')
        return {'max_nbd': int(out)}

    @prop_loader(SourceTypes.PROC, ['max_ublk'])
    def _max_ublk_loader(self):
        out, err, code = self.client_node.host.execute('cat /sys/module/ublk_drv/parameters/ublks_max')
        return {'max_ublk': int(out)}

    # JW: This seems only used for testing.
    # For now support 1 lvol per volume
    def attach_and_create_snapshot(self, volume):
        volume.snapshot_create_and_attach(self, timeout=120)
        virtual_volume = Volume.instance(name=f"{volume.name}_lvs/lvol")
        virtual_volume.RAIDlevel = "lvol" # Just some way to mark it as virtual volume
        lvols = self.execute_rpc(f'bdev_lvol_get_lvols')
        for lvol in lvols:
            self.lvol_uuid_to_vname[lvol['uuid']] = lvol['alias']
        self.logger.debug(f'lvol_uuid_to_vname: {str(self.lvol_uuid_to_vname)}')
        return virtual_volume

    def detach_snapshot(self, volume):
        volume.snapshot_detach_and_delete()


@entity(sourcetypes=[SourceTypes.PROC, SourceTypes.MANAGEMENT, SourceTypes.OS])
class ClientNode(NvNode):
    clients : List[Client] = PropertySpec([Client])
    _is_dpu : Optional[bool] = None
    _traces_conf : Optional[str] = None
    MULTI_CLIENTS_JSON_PROC = 'inst_list.json'
    CLIENT_INSTANCE_CMD = 'nvmesh_client_instance_do'
    MAX_MULTI_INSTANCES = 64
    PERSISTENCIES = "/var/opt/nvmesh/mcs/CLIENT/CONFIGURATION"

    def get_multi_instance_json(self):
        return json.loads(self.proc_content("/proc/nvmeibc", self.MULTI_CLIENTS_JSON_PROC, no_cache=True))

    # TODO: Need to expand this for real multi-instance support
    @prop_loader(SourceTypes.PROC, ['clients'])
    def load_client_node_clients(self):
        from xlro.core.entities import Manager
        output = self.get_multi_instance_json()

        clients = []
        for inst in output["instances"]:
            name = inst["name"]
            if inst["dev_dir"] == "nvmesh" and self.is_multi_instance:
                # not relevant anymore
                continue
            mgmt = inst["management"]["cluster"].split(',')
            no_port_cluster = [m.split(':')[0] for m in mgmt]
            clients.append(Client.instance(name=self._build_client_instance_name(name),
                                           mgmt=Manager.instance(endpoints=no_port_cluster)))

        return {'clients': clients}

    @property
    def traces_conf(self):
        if self._traces_conf is None:
            out, _, _ = self.connection.execute(f"grep -oP '^NFS_TARGET=\"*\K[^\"]*' {PagerUtils.NFS_CLIENT_CONF}")
            self._traces_conf = os.path.join(out.strip(), self.name) if out else ''
        return self._traces_conf

    @property
    def is_dpu(self):
        if self._is_dpu is None:
            self._is_dpu = Connection.err2exc(self.connection.execute('uname -m')).strip() == 'aarch64'
        return self._is_dpu

    def _build_client_instance_name(self, c_name: str) -> str:
        if c_name.startswith('ag'):
            # autogenerated(ag) by mgmt - name will be hostname
            return self.name
        return "_".join((self.name, c_name)) if c_name != 'nvmeibc' else self.name

    def _validate_client_instance_id(self, id: Any) -> None:
        assert 0 < id < self.MAX_MULTI_INSTANCES, "id should be between 0 to {}".format(self.MAX_MULTI_INSTANCES)

    def add_client_instance(self, id: Any) -> None:
        self._validate_client_instance_id(id)

        outbuf, errbuf, code = self.connection.execute('sudo {} -a --mc {}'.format(self.CLIENT_INSTANCE_CMD, str(id)))
        assert not code, "failed to add multi instance"

    def remove_client_instance(self, id: Any) -> None:
        self._validate_client_instance_id(id)

        outbuf, errbuf, code = self.connection.execute('sudo {} -d --mc {}'.format(self.CLIENT_INSTANCE_CMD, str(id)))
        assert not code, "failed to remove multi instance"

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(ClientNode, cls).map_props(propmap, source_type)
        if 'name' in propmap:
            propmap['name'] = host_name(propmap['name'])
        return propmap

    @staticmethod
    def build_services(host):
        return {'client': Service.instance(name='nvmeshclient', host=host, pid_path='nvmeshclient/monitor'),
                'nvmft': Service.instance(name='nvmeshnvmft', host=host, pid_path='nvmeshnvmft/nvmeshnvmft.pid'),
                'nvmeshum': Service.instance(name='nvmeshum', host=host, pid_path='nvmeshum/nvmeshum'),
                'rpcbind': Service.instance(name='rpcbind', host=host, pid_path='rpcbind.pid'),
                'nfs-server': Service.instance(name='nfs-server', host=host, pid_path='nfs-server.pid'),
                'nfs-idmapd': Service.instance(name='nfs-idmapd', host=host, pid_path='nfs-idmapd.pid'),
                'nvmeshcm': Service.instance(name='nvmeshcm', host=host, pid_path='managementCM'),
               }

    @property
    def services(self):
        return self.build_services(self.host)

    _attach_options = None
    _options_lock = Lock()
    @property
    def attach_options(self):
        ''' Hack to show supported attach options, since no versioning support :-( '''
        if self._attach_options is None:
            with self._options_lock:
                if self._attach_options is None:
                    out, err, code = self.connection.execute(
                        "sudo grep -Po -- '--\K[a-z][-a-z_]*' $(type -p {})".format(Client.NVMESH_ATTACH))
                    self._attach_options = out.split()
            self.logger.debug('ATTACH OPTIONS: {}'.format(self._attach_options))
        return self._attach_options

    @property
    def is_multi_instance(self):
        hostconf = Host.instance(name=self.name).nvmesh_config.config
        return hostconf and hostconf.get('MULTI_INSTANCE_ENABLED', 'no').lower() == 'yes' \
               and 'management_cluster' in self.attach_options

    @property
    def attach_supports_json(self):
        return 'json' in self.attach_options

    @property
    def attach_supports_sub_block(self):
        return 'sub-block-io-allowed' in self.attach_options


# TODO should we consider define channels as entities?
class Channel(object):

    CHANNEL_STATE_LIVE = 'Live'

    def __init__(self, state, local_gid, remote_gid):
        self.state = state
        self.local_gid = local_gid
        self.remote_gid = remote_gid


class IOChannel(Channel):
    def __init__(self, idx, in_use, state, local_gid, remote_gid):
        super(IOChannel, self).__init__(state, local_gid, remote_gid)
        self.idx = idx
        self.in_use = in_use == 'true'


class LIONic(object):
    def __init__(self, hw_gid, device, channels):
        self.hw_gid = hw_gid
        self.device = device
        self.channels = channels


class RIONic(object):
    def __init__(self, hw_gid, lionics):
        self.hw_gid = hw_gid
        self.lionics = lionics


class LockChannel(Channel):
    def __init__(self, state, local_gid, remote_gid):
        super(LockChannel, self).__init__(state, local_gid, remote_gid)


class AdminChannel(Channel):
    def __init__(self, state, local_gid, remote_gid):
        super(AdminChannel, self).__init__(state, local_gid, remote_gid)


class IOPerm(Enum):
    NVMEIB_IO_TYPE_PERMIT_NEVER = -10
    NVMEIB_IO_TYPE_PERMIT_NONE_SUS = -7
    NVMEIB_IO_TYPE_PERMIT_NONE_INV = -2
    NVMEIB_IO_TYPE_PERMIT_NONE_ERR = -1
    NVMEIB_IO_TYPE_PERMIT_NO_IO = 1
    NVMEIB_IO_TYPE_PERMIT_RDONLY = 2
    NVMEIB_IO_TYPE_PERMIT_ALL_NO_PROTECTION = 3
    NVMEIB_IO_TYPE_PERMIT_ALL = 5


@entity(sourcetypes=[SourceTypes.BINARY_TRACE])
class ClientVolumeTopology(NamedEntity):
    client : Client = PropertySpec(Client, key=True)
    topo_debug_id : int = PropertySpec(int, key=True)
    io_perm : IOPerm = PropertySpec(IOPerm)
    praids : List['PraidTopology'] = PropertySpec(['PraidTopology'], default=[])
    set_active_time : str = PropertySpec(str)

    @classmethod
    def get_volume_topologies(cls, client: "Client", volume: "Volume", start_time: datetime = None, end_time: datetime = None) -> List['ClientVolumeTopology']:

        # construct pager query and get results
        filter_query = "({vname}) && ({has_traces}) && ({multi_instance})".format(
            vname=PagerUtils.get_field_eq('DEV_NAME', volume.name),
            has_traces=PagerUtils.get_has_traces([TRACE_METHOD2TYPE['__set_error_state'],
                                                  TRACE_METHOD2TYPE['nvmeibc_raid1_calc_io_perm'],
                                                  TRACE_METHOD2TYPE['__set_active_topology']]),
            multi_instance=PagerUtils.get_multi_instance_idx_filter(client.inst_idx))
        time_query = PagerUtils.get_time_query(start_time, end_time, client.host)
        all_trace_entries = PagerUtils.get_pager_results(client.host, filter_query=filter_query, time_query=time_query)

        vol_topos = []

        for trace_entry in all_trace_entries:
            # fetch the relevant ClientVolumeTopology instance for every TraceEntry.
            # for every recieved trace type, set the relevant Topology entity (Client/Praid/Segement) props,
            # using TraceEntry.parsed_msg (in future will given automatically using 'pager --json')

            if trace_entry.topo_debug_id:
                client_vol_topo = ClientVolumeTopology.instance(name=volume.name, client=client,
                                                                topo_debug_id=trace_entry.topo_debug_id)
                if trace_entry.method == '__set_active_topology':
                    client_vol_topo.set_property('set_active_time', trace_entry["time_stamp"], SourceTypes.BINARY_TRACE)
                    vol_topos.append(client_vol_topo)

                elif trace_entry.method == '__set_error_state':
                    client_vol_topo.set_property('io_perm', int(trace_entry["io_perm"]), SourceTypes.BINARY_TRACE)

                elif trace_entry.method == 'nvmeibc_raid1_calc_io_perm':
                    praid_topo = PraidTopology.instance(volume=volume,
                                                        index=trace_entry["praid"],
                                                        chunk_index=trace_entry["chunk"],
                                                        version=trace_entry["version"])

                    if praid_topo not in client_vol_topo.praids:
                        list_index_insert(client_vol_topo.praids, praid_topo.index, praid_topo)

                    segment_topo = SegmentTopology.instance(praid_topology=praid_topo,
                                                            index=trace_entry["segment"])
                    segment_topo.set_properties(dict(acm=trace_entry["acm"],
                                                     disk=Drive.instance(name=trace_entry["disk_name"]),
                                                     disk_state=trace_entry["disk_state"]),
                                                SourceTypes.BINARY_TRACE)

                    if segment_topo not in praid_topo:
                        list_index_insert(praid_topo.segments, segment_topo.index, segment_topo)

        return vol_topos


def main():
    from xlro.core.util.cli_util import CLIArgumentParser
    parser = CLIArgumentParser(require_manager=True)
    parser.add_argument('command', choices=['attach', 'detach'])
    parser.add_argument('--clients', type=EntityArg('Client'), nargs='+')
    parser.add_argument('--volumes', type=EntityArg('Volume'), nargs='+')
    parser.add_argument('--additional-args', default=[], nargs='+')
    args = parser.parse_args()

    additional_args = {}
    for arg in args.additional_args:
        key, value = arg.split('=')
        if value.lower() == 'true':
            value = True
        elif value.lower() == 'false':
            value = False

        additional_args[key] = value

    if args.command == 'attach':
        for client in args.clients:
            client.attach(args.volumes, **additional_args)
    elif args.command == 'detach':
        for client in args.clients:
            client.detach(args.volumes, **additional_args)

    return 0


if __name__ == '__main__':
    main()
