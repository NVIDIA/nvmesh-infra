#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division

import time

from xlro.core.entities.rest_info import RestVersionManager
from future import standard_library

standard_library.install_aliases()
from builtins import zip
from builtins import str
from builtins import map
from builtins import range
from xlro.core.util.general_utils import old_div
from builtins import object
import re

from deprecated import deprecated
from uuid import UUID
import os
import json
import csv
import sys
PY3 = sys.version_info[0] == 3
if PY3:
    from io import StringIO
else:
    from StringIO import StringIO
from typing import Any,Dict,List,Optional,Sequence,Tuple,Union, Mapping, Set
from collections import namedtuple, deque
from threading import Lock

from xlro.core.entities.base import entity, prop_loader, PropertySpec, SourceTypes
from xlro.core.entities.etypes import Size, RAIDLevel, ECSeparationType, KeyValue
from xlro.core.entities import BaseEntity, UUIDEntity
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from xlro.core.entities import Drive, Target, Client, Manager, TargetClass, DriveClass
from xlro.core.entities.sdk_base import SdkObject, SDKEntity, sdk_entity, SdkException, UnknownEntity, D_OR_E, RE
from xlro.core.util.ssh import Connection
from xlro.core.util.lba import SwLBA, LBARange
from xlro.core.util.cli_util import raw_to_snake
from xlro.core.util.general_utils import wait_for_property_values, WaitResult
from xlro.core.util.block_objects import Page, PSlice, PageData, BlockSet
from xlro.core.sdk.Utils import Utils as SdkUtils, MongoObj

VOLUME_MDATA_SIZE = 8

# TODO: Naming is inconsistent w/ Volume, but consistent w/ REST.
# Might change for CLI, but for now, we won't try for mapping
class MDVSpec(SdkObject):
    limitByNodes: List['Target']
    limitByDisks: List['Drive']
    serverClasses: List['TargetClass']
    diskClasses: List['DriveClass']
    VPG: Optional['VPG']

    # TODO: SdkObject should support an init with type conversions based on self._get_specs()
    def __init__(self, limitByNodes = [], limitByDisks = [], serverClasses = [], diskClasses = [], VPG = None, **ignore):
        self.limitByNodes = limitByNodes
        self.limitByDisks = limitByDisks
        self.serverClasses = serverClasses
        self.diskClasses = diskClasses
        self.VPG = VPG

class EncryptionObj(SdkObject):
    headerSize: int = 16

class CDVConfig(SdkObject):
    """Configuration for a Capacity Data Volume."""
    cdvExtentSizeMib : int  # power-of-2 in range [64, 65536] MiB
    allocatorSizeGib : int  # size of the CDV_MGMT satellite volume in GiB; default 1
    maxTPVs         : int  # max number of TPVs allowed on this CDV; default 512

class TPVConfig(SdkObject):
    """Configuration for a Thin-Provisioned Volume."""
    # camelCase names must match what the management server expects
    cdvId            : str    # required; parent CDV name/_id
    tpvExtentSizeKB  : int    # power-of-2 in range [64, 65536] KB

@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT, SourceTypes.PROC])
class Chunk(SDKEntity):
    name : str = PropertySpec(str, key=True)
    vlbs : int = PropertySpec(int, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    vlbe : int = PropertySpec(int)
    size : int = PropertySpec(int)
    pRaids : List['PRaid'] = PropertySpec(['PRaid'])
    stripeSize : int = PropertySpec(int)
    stripeWidth : int = PropertySpec(int)
    class LBA(SwLBA): pass

    @prop_loader(None, ["stripeSize", "stripeWidth"])
    def load_from_parent(self, source):
        parent = Volume.instance(name=self.name, mgmt=self.mgmt)
        return {k: parent.get_property(k, source) for k in ["stripeSize", "stripeWidth"]}

    @property
    def stripe_data_pages_size(self):
        return self.pRaids[0]._stripe_data_pages_size

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(Chunk, cls).map_props(propmap, source_type)
        if 'dataDisks' in propmap:
            dataDisks = propmap.pop('dataDisks')
            for pdict in propmap['pRaids']:
                pdict['dataDisks'] = dataDisks

        if 'parityDisks' in propmap:
            parityDisks = propmap.pop('parityDisks')
            for pdict in propmap['pRaids']:
                pdict['parityDisks'] = parityDisks

        return propmap

    @property
    def blockSize(self) -> int:
        return Volume.get_block_size()

    def get_plba(self, clba: 'LBA') -> Tuple['PRaid', 'PRaid.LBA']:
        assert isinstance(clba, Chunk.LBA), 'clba {} is not of type Chunk.LBA'.format(clba)
        # clba = self.lba(clba)

        blockSetIndex = clba.addr // self.stripe_data_pages_size
        praid_index = blockSetIndex % self.stripeWidth
        praid = self.pRaids[praid_index]
        p_addr = blockSetIndex // self.stripeWidth * self.stripe_data_pages_size + clba.addr % self.stripe_data_pages_size
        plba = PRaid.LBA(p_addr)
        return praid, plba

    def to_foreign_sdk_entity(self) -> Dict:
        """ Chunks are never sent to REST """
        return {}


@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT, SourceTypes.PROC])
class PRaid(SDKEntity):
    uuid : UUID = PropertySpec(UUID, key=True)
    # consider making a key prop
    chunk : Chunk = PropertySpec(Chunk)
    RAIDlevel : RAIDLevel = PropertySpec(RAIDLevel)
    activated : bool = PropertySpec(bool)
    tomaLeader : str = PropertySpec(str)
    tomaLeaderConnectionSequence : int = PropertySpec(int)
    tomaLeaderRaftTerm : int = PropertySpec(int)
    diskSegments : List['Segment'] = PropertySpec(['Segment'])
    dataDisks : int = PropertySpec(int)
    parityDisks : int = PropertySpec(int)
    stripeIndex : int = PropertySpec(int)
    class LBA(SwLBA): pass

    @classmethod
    def _get_praid_uuid(cls, v_uuid: str, c_lbs: int, p_idx: int, target: Optional['Target'] = None, no_cache: Optional[bool] = False) -> str:
        """find praid (and its uuid) according to additional praid details (its chunk, and its praid idx)"""
        from xlro.core.entities import Target
        sec2csv = Target.get_toma_cfg_sec2csv(target=target, no_cache=no_cache)
        c_uuid = None
        for cdict in csv.DictReader(StringIO(sec2csv[Target.get_entity_cls2cfg_section()[Chunk]])):
            if cdict['block_device_uuid'] == v_uuid and int(cdict['vlb_s']) == c_lbs:
                c_uuid = cdict['uuid']
                break
        assert c_uuid, 'no chunk found'

        for pdict in csv.DictReader(StringIO(sec2csv[Target.get_entity_cls2cfg_section()[PRaid]])):
            if pdict['chunk_uuid'] == c_uuid and int(pdict['stripe_idx']) == p_idx:
                return pdict['uuid']
        raise AssertionError('no praid found')

    class RaidLevels(object):
        RAID0 = 'Striped RAID-0'
        RAID1 = 'Mirrored RAID-1'
        RAID10 = 'Striped & Mirrored RAID-10'
        ERASURE_CODING = 'Erasure Coding'
        CONCATENATED = 'Concatenated'
        JBOD = CONCATENATED
        EC = ERASURE_CODING
        ELECT = 'ELECT'

    @property
    def blockSize(self) -> int:
        return Volume.get_block_size()

    @property
    def pages(self) -> int:
        """return number of pages within all praid. equivalent to 'blocks' PropertySpec in Volume"""
        seg = self.get_dataSegments()[0]
        return seg.pages * self.dataDisks

    @property
    def width(self) -> int:
        return self.dataDisks + self.parityDisks

    @property
    def _stripe_data_pages_size(self):
        return self.chunk.stripeSize * self.dataDisks

    @property
    def volume(self):
        return Volume.instance(name=self.chunk.name, mgmt=self.mgmt)

    def get_segments(self: 'PRaid', segment_type: str, source: str = None, no_cache: bool = False) -> List['Segment']:
        disk_segments = self.get_property('diskSegments', source, no_cache)
        return [seg for seg in disk_segments if seg.segmentType == segment_type]

    def get_dataSegments(self: 'PRaid', source: str = None, no_cache: bool = False) -> List['Segment']:
        return self.get_segments('data', source, no_cache)

    def get_raftSegments(self: 'PRaid', source: str = None, no_cache: bool = False) -> List['Segment']:
        return self.get_segments('raftonly', source, no_cache)

    RotationCalc = namedtuple('CycleCalc', ['rotation_steps', 'sliceIndex', 'segmentIndex'])

    def rotation_calc(self, plba: 'PRaid.LBA') -> "RotationCalc":
        """
        * preform all the cycle math calculations for determine disks order and addresses
        * returns namedtuple object with the calculations results (rotation_steps, sliceIndex, segmentIndex)
        """
        assert isinstance(plba, PRaid.LBA), 'plba {} is not of type PRaid.LBA'.format(plba)
        # convenience variables
        cycle_size = 2 * self.chunk.stripe_data_pages_size
        totalDisks = self.dataDisks + self.parityDisks
        snake = self.volume.snake

        # pre calculations
        cycleIndex = plba.addr // cycle_size

        # results calculations
        rotation_steps = cycleIndex % totalDisks
        sliceIndex = (plba.addr // (self.dataDisks * snake)) * snake + (plba.addr % snake)
        segmentIndex = ((plba.addr // snake) % self.dataDisks + rotation_steps) % totalDisks

        return self.RotationCalc(rotation_steps, sliceIndex, segmentIndex)

    def get_slba(self, plba):
        assert isinstance(plba, PRaid.LBA), 'plba {} is not of type PRaid.LBA'.format(plba)
        cycle_results = self.rotation_calc(plba)

        segment = self.get_dataSegments()[cycle_results.segmentIndex]
        s_addr = cycle_results.sliceIndex

        slba = Segment.LBA(s_addr)
        role = (cycle_results.segmentIndex - cycle_results.rotation_steps) % (self.dataDisks + self.parityDisks)

        return segment, slba, role

    def get_pslice(self, plba: 'PRaid.LBA', vlba: Optional['Volume.LBA'] = None) -> PSlice:
        """
            - calc the praid parameters relevant for slice (rotation_calc method)
            - sort the praid.dataSegments by the disks 'role' ([0] -> D0 , [1] -> D1 , ...)
            - create a Block for for every segment in the sorted parid.dataSegments
            - construct & return a PSlice object from the ordered blocks list
        """
        assert isinstance(plba, PRaid.LBA), 'plba {} is not of type PRaid.LBA'.format(plba)
        assert vlba is None or isinstance(vlba, Volume.LBA), 'vlba {} is not of type Volume.LBA'.format(vlba)

        cycle_results = self.rotation_calc(plba)

        if vlba:
            slice_vlba = vlba - (cycle_results.segmentIndex - cycle_results.rotation_steps) % self.width
            return PSlice(self, cycle_results.sliceIndex, slice_vlba)

        return PSlice(self, cycle_results.sliceIndex)

    def page_role_to_index(self, pageref: Union[int, str]) -> int:
        d = self.dataDisks
        p = self.parityDisks
        if not pageref:
            role = 0
        elif isinstance(pageref, int) or pageref.isdigit():
            role = int(pageref)  # O-based role index
            if not 0 <= role < d + p:
                raise Exception('Invalid role index: ' + str(pageref))
        else:
            rtype = pageref[0].upper()
            role = int(pageref[1:])  # R/M/D are 1's based, I think
            if rtype == 'M':  # Mirror/Replica #
                if not 0 <= role < p:
                    raise Exception('Invalid mirror index: ' + pageref)
            elif rtype == 'D':  # Data disk #
                if not 0 <= role < d:
                    raise Exception('Invalid Data index: ' + pageref)
            elif rtype == 'P':  # Parity disk #
                if not 0 <= role < p:
                    raise Exception('Invalid Parity index: ' + pageref)
                role += d
            else:
                raise Exception('pageref is not initialized')

        return role


# TODO - make segment UUID entity (cause instantiation of raft segments
#      - same disk and lbs - 0 but different uuid and praid)
@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT, SourceTypes.PROC])
class Segment(SDKEntity):
    drive : 'Drive' = PropertySpec('Drive')
    diskID : str = PropertySpec(str, key=True)
    # TODO - rename to dlbs and consider typing as Drive.LBA
    lbs : int = PropertySpec(int, key=True)
    status : str = PropertySpec(str)
    pRaidTypeIndex : int = PropertySpec(int)
    diskUUID : UUID = PropertySpec(UUID)
    isDead : bool = PropertySpec(bool)
    volumeName : str = PropertySpec(str)
    pRaidIndex : int = PropertySpec(int)
    remainingDirtyBits : int = PropertySpec(int, default=0)
    node_id : str = PropertySpec(str)
    lbe : int = PropertySpec(int)
    nodeUUID : UUID = PropertySpec(UUID)
    pRaidUUID : UUID = PropertySpec(UUID)
    allocationIndex : int = PropertySpec(int)
    segmentType : str = PropertySpec(str)
    volumeUUID : UUID = PropertySpec(UUID)
    partition : 'Partition' = PropertySpec('Partition')  # NOTICE - there's no loader at all for this prop (unreachable)
    class LBA(SwLBA): pass

    DATA_SEGMENT = 'data'
    RAFTONLY_SEGMENT = 'raftonly'

    @classmethod
    def _get_segment_uuid(cls, p_uuid: str, s_idx: int, target: Optional[SDKEntity] = None, no_cache: Optional[bool] = False) -> str:
        """find segment (and its uuid) according to additional segment details (its praid, and its seg idx)"""
        from xlro.core.entities import Target
        assert target is None or isinstance(target, Target), 'Invalid target object: {}'.format(type(target))
        for sdict in csv.DictReader(StringIO(Target.get_toma_entity_csv(Segment, target=target, no_cache=no_cache))):
            if sdict['praid_uuid'] == p_uuid and sdict['idx_in_praid'] == s_idx:
                return sdict['uuid']
        raise AssertionError('no segment found')

    @property
    def blockSize(self) -> int:
        return Volume.get_block_size()

    @property
    def pages(self) -> int:
        hw_bs2sw_bs = self.blockSize / float(self.drive.blockSize)
        pages = old_div((self.lbe-self.lbs), hw_bs2sw_bs)
        assert pages.is_integer(), "segment must have int number of pages"
        return int(pages)

    def get_dlba_range(self, slba: 'Segment.LBA') -> Tuple['Drive', LBARange]:
        assert isinstance(slba, Segment.LBA), 'slba {} is not of type Segment.LBA'.format(slba)
        ratio = slba.blockSize / float(self.drive.blockSize)
        # for now we assume Sw block_size is >= Hw block_size, but can be changed
        assert ratio.is_integer(), 'ratio {} is not an integer'.format(ratio)
        dlbs = self.drive.local_dlba((self.lbs + slba.addr) * int(ratio))
        dlba_range = LBARange(dlbs, int(ratio))

        return self.drive, dlba_range

    @classmethod
    def map_props(cls, propmap, source=None):
        from xlro.core.entities import Drive
        propmap = super(Segment, cls).map_props(propmap, source)

        if 'diskID' in propmap:
            propmap['drive'] = Drive.instance(name=propmap['diskID'])

        if 'type' in propmap:
            propmap['segmentType'] = propmap.pop('type')

        if 'isDead' in propmap:
            # When segment is dead status sdk field doesn't hold a real value
            if propmap['isDead']:
                propmap['status'] = "Dead"

        return propmap


@entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT])
class Partition(UUIDEntity):
    lbs : int = PropertySpec(int)
    diskUUID : UUID = PropertySpec(UUID)
    lbe : int = PropertySpec(int)
    partitionName : str = PropertySpec(str)
    drive : 'Drive' = PropertySpec('Drive')
    partitionIndex : int = PropertySpec(int)
    # JW: Why were these props missing?
    name : str = PropertySpec(str)
    target : 'Target' = PropertySpec('Target')
    # TODO - add support to props below (map props and such)
    # partitionType : str = PropertySpec(str)
    # node_id : str = PropertySpec(str)
    # gptEnd : int = PropertySpec(int)
    # nodeUUID : UUID = PropertySpec(UUID)
    # owner : str = PropertySpec(str)
    # gptStart : int = PropertySpec(int)
    # diskID : str = PropertySpec(str)

    class ConstNames(object):
        JOURNAL_DATA = 'excelero_journal_data'

    @classmethod
    def map_props(cls, propmap, source_type=None):
        from xlro.core.entities import Drive
        try:
            propmap['drive'] = Drive.instance(name=propmap.pop('diskID'))
        except KeyError:
            try:
                propmap['drive'] = Drive.instance(name=propmap.pop('disk_id'))
            except KeyError:
                pass

        try:
            propmap['partitionName'] = propmap['volname']
        except KeyError:
            pass

        try:
            propmap['lbs'] = propmap['start']
        except KeyError:
            pass

        try:
            propmap['lbe'] = propmap['end']
        except KeyError:
            pass

        try:
            propmap['partitionIndex'] = int(propmap['part_num'])
        except KeyError:
            pass

        return propmap

    @prop_loader(SourceTypes.PROC, ['partitionIndex', 'lbs', 'lbe', 'partitionName'])
    def load_from_drive(self):
        # first read from serjio/<disk>/partitions.csv
        try:
            partitions = [self.drive.update_serjio_partition_fields(p) for p in csv.DictReader(StringIO(self.target.proc_content(
                os.path.join(self.target.PROC, os.path.join("serjio", self.name, "partitions.csv")))))]
        except:
            partitions = [p for p in csv.DictReader(StringIO(self.target.proc_for_partitions()))]
        for prt in partitions:
            if UUID(prt['uuid']) == self.uuid:
                return prt
        return {}


class Block(object):
    def __init__(self, data, metadata=None):
        self.data = data
        self.metadata = metadata


@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT, SourceTypes.PROC])
class Volume(SDKEntity):
    BLOCK_SET_WIDTH = 32
    BLOCK_SIZE = 4096
    _BLOCK_SIZE: int = -1
    MAX_INT = -1
    MAX_STR = 'MAX'
    AUTO_KEK = 'top-secret'
    name : str = PropertySpec(str, key=True)
    uuid : str = PropertySpec(str)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    capacity : Size = PropertySpec(Size)
    blockSize : int = PropertySpec(int, default=BLOCK_SIZE)
    stripeSize : int = PropertySpec(int)
    stripeWidth : int = PropertySpec(int)
    dataBlocks : int = PropertySpec(int)
    parityBlocks : int = PropertySpec(int)
    protectionLevel : ECSeparationType = PropertySpec(ECSeparationType) #, default='N/A')
    ignoreNodeSeparation : bool = PropertySpec(bool)
    blocks : int = PropertySpec(int)
    status : str = PropertySpec(str, readonly=True)
    health : str = PropertySpec(str, readonly=True)
    action : str = PropertySpec(str, readonly=True)
    chunks : List[Chunk] = PropertySpec([Chunk], readonly=True)
    description : str = PropertySpec(str)
    relativeRebuildPriority : int = PropertySpec(int)
    class LBA(SwLBA): pass
    RAIDlevel : RAIDLevel = PropertySpec(RAIDLevel)
    numberOfMirrors : int = PropertySpec(int)
    # TODO - change to List[Target] / List[Drive]
    limitByNodes : List[str] = PropertySpec([str], default=[])
    limitByDisks : List[str] = PropertySpec([str], default=[])
    targetClasses : List['TargetClass'] = PropertySpec(['TargetClass'])
    driveClasses : List['DriveClass'] = PropertySpec(['DriveClass'])
    domain : str = PropertySpec(str)
    reservation_mode : str = PropertySpec(str)
    reservation_version : int = PropertySpec(int)
    reserved_by : 'Client' = PropertySpec('Client', readonly=True)
    VPG : str = PropertySpec(str)
    VSGs : List['VolumeSecurityGroup'] = PropertySpec(['VolumeSecurityGroup'], default=[])
    nvmf_enabled : bool = PropertySpec(bool)
    enabled_nvmf_clients : List[str] = PropertySpec([str])
    crc_enabled : bool = PropertySpec(bool, default=False)
    raid_type : str = PropertySpec(str)
    use_debug_di : bool = PropertySpec(bool)
    sub_volumes : Mapping[str, 'Volume'] = PropertySpec({'role': 'Volume'}, default={})
    vtype : str = PropertySpec(str)
    snake : int = PropertySpec(int)
    combined_status : str = PropertySpec(str, readonly=True)
    combined_action : str = PropertySpec(str, readonly=True)
    combined_health : str = PropertySpec(str, readonly=True)
    is_snapshot : bool = PropertySpec(bool)
    sourceID : str = PropertySpec(str)
    sourceUUID : str = PropertySpec(str)  # may be a UUID string or sentinel e.g. 'sync_flush' for TPVs
    metadataVolumeID : str = PropertySpec(str)
    isReadOnly : bool = PropertySpec(bool, default=False)
    isEncrypted : bool = PropertySpec(bool, default=False)
    autoEncrypt : bool = PropertySpec(bool, default=False)
    encryption: Optional[EncryptionObj] = PropertySpec(EncryptionObj)
    encryption_ready : bool = PropertySpec(bool, default=False)
    isReady : bool = PropertySpec(bool, default=False)
    allowAllocationOnOfflineDrives : bool = PropertySpec(bool, default=False)
    metadata : KeyValue = PropertySpec(KeyValue, default=KeyValue())

    # Note: can be None. Too hard to differentiate projection from actual missing mdvSpec
    mdvSpec: Optional[MDVSpec] = PropertySpec(MDVSpec)

    # Thin-provisioning discriminator and sub-configs
    volumeClass : str                       = PropertySpec(str)
    cdvConfig   : Optional[CDVConfig]       = PropertySpec(CDVConfig)
    tpvConfig   : Optional[TPVConfig]       = PropertySpec(TPVConfig)
    tpvCount    : int                       = PropertySpec(int, readonly=True)
    # CDV ↔ satellite linkage (CDV_MGMT volumes; see SatelliteVolumeForCDVAlloc.md)
    allocatorVolumeId   : Optional[str]     = PropertySpec(str, readonly=True)
    allocatorVolumeUUID : Optional[str]     = PropertySpec(str, readonly=True)
    parentCDVId         : Optional[str]     = PropertySpec(str, readonly=True)
    parentCDVUUID       : Optional[str]     = PropertySpec(str, readonly=True)

    _action_shadows_status = None

    PENDING = 'pending'
    ONLINE = 'online'
    OFFLINE = 'offline'
    REBUILDING = 'rebuilding'
    DEGRADED = 'degraded'
    MARKED_FOR_DELETION = 'markedForDeletion'
    MARKED_FOR_FORCE_DELETION = 'markedForForceDeletion'
    MARKED_FOR_REBUILD = 'markedForRebuild'
    REBUILD_REQUIRED = 'rebuildRequired'
    MARKED_FOR_REBUILD_OLD = 'markedForRebuild_old'
    EXTENDING = 'extending'
    INITIALIZING = 'initializing'
    UNAVAILABLE = 'unavailable'
    TO_BE_DELETED = 'toBeDeleted'
    QUORUM_FAILED = 'quorumFailed'

    bs_lock = Lock()

    @classmethod
    def _set_mgmt(cls, obj, mgmt):
        if isinstance(obj, dict) and 'metadata' in obj:
            # Don't recurse into metadata -- it's opaque user data, not an entity dict
            metadata = obj.pop('metadata')
            super(Volume, cls)._set_mgmt(obj, mgmt)
            obj['metadata'] = metadata
            return
        super(Volume, cls)._set_mgmt(obj, mgmt)

    def __init__(self, *args, **kwargs):
        super(Volume, self).__init__(*args, **kwargs)
        self.last_attached_client: Optional[Client] = None

    @classmethod
    def get_block_size(cls) -> int:
        # TODO - add valid loading of system's 'software block size' (and clean this mess)
        if cls._BLOCK_SIZE == -1:
            with cls.bs_lock:
                if cls._BLOCK_SIZE == -1:
                    cached_vol = None
                    for ent in list(BaseEntity.ENTITY_CACHE.values()):
                        if isinstance(ent, Volume):
                            cached_vol = ent
                            break
                    if not cached_vol:
                        return Volume.BLOCK_SIZE
                    cls._BLOCK_SIZE = cached_vol.blockSize

        return cls._BLOCK_SIZE

    def to_foreign_sdk_entity(self, local_only: bool=False) -> Dict:
        # Override to apply property mapping to SDK sub-object
        from xlro.core.entities import VPG

        vol_dict = super(Volume, self).to_foreign_sdk_entity(local_only=local_only)
        # NOTE: post to_foreign_sdk_entity(), all keys are in management terms
        vol_dict.pop('chunks', None)

        for prop in ['protectionLevel', 'domain']:
            if prop in vol_dict and vol_dict[prop] is None:
                vol_dict.pop(prop)

        if vol_dict.get('capacity') == self.MAX_INT:
            vol_dict['capacity'] = self.MAX_STR

        # management's createTPV expects capacity in binary GiB, but the SDK's
        # useGB feature converts Size(bytes) → decimal GB (÷1000³), inflating
        # the size by ~7.4% (e.g. 8 GiB → 8.59 GiB).  Override for TPV after
        # the generic conversion has already run.
        if vol_dict.get('volumeClass') == 'TPV' and isinstance(self.capacity, Size) and self.capacity > 0:
            vol_dict['capacity'] = self.capacity.ivalue / (1024 ** 3)

        if vol_dict.get('parityBlocks') == 0:
            del vol_dict['parityBlocks']
        if 'mdv' in vol_dict and 'MDV' in self.sub_volumes:
            vol_dict['mdv'] = self.sub_volumes['MDV'].to_foreign_sdk_entity(local_only=local_only)

        if 'sourceID' in vol_dict:
            vol_dict['sourceUUID'] = str(Volume.instance(name=vol_dict['sourceID']).uuid)

        if not vol_dict.get('isEncrypted'):
            vol_dict.pop('encryption', None)

        self.logger.info(f'Volume {self.name} - API-Version: {self.rest_version} - has-defaults: {self.rest_feature("has-defaults")}')
        if not self.rest_feature('has-defaults'):
            # Until REST defaults, enableCrcCheck was required and/or ignored
            if not vol_dict.get('enableCrcCheck'):
                vol_dict['enableCrcCheck'] = False
        else:
            # Once REST has defaults, we leave all the flags as is.
            if 'use_debug_di' in vol_dict and vol_dict['use_debug_di'] is None:
                del vol_dict['use_debug_di']

            if local_only and 'name' in vol_dict:
                del vol_dict['name']

            # When VPG is specified, remove properties that are inherited from the VPG
            if vol_dict.get('VPG'):
                # Get VPG entity from management as REST dict for field comparison
                try:
                    vpg_dict = next(VPG._sdk_get(self.mgmt, count=1, filter_mongo_objs=[MongoObj(VPG.cls_rest_info(self.mgmt).dbkey, vol_dict['VPG'])]))
                except Exception as e:
                    self.logger.info(f"VPG {vol_dict['VPG']} not found: {repr(e)}")
                    vpg_dict = vol_dict # This will mean we'll ignore conflicts with VPG.  Let REST figure it out

                # Convert to external REST names, since we already called super.to_foreign_sdk_entity()
                # Not sure about caching. In self, not worth much and in Class could be problematic if dealing with multiple managers
                rest_info = self.rest_info
                vpg_props = [rest_info.infra2rest.get(prop, prop) for prop in VPG.VPG_VOLUME_FIELDS]
                vpg_props.extend(['limitByDisks', 'limitByNodes'])
                for prop in [p for p in vpg_props if p in vol_dict]:
                    if vol_dict[prop] and vol_dict[prop] != vpg_dict.get(prop, None):
                        raise Exception(f"Property {prop} conflicts with VPG {vol_dict['VPG']}: {vol_dict[prop]} != {vpg_dict.get(prop, None)}")
                    vol_dict.pop(prop)

        return vol_dict

    def update(self):
        """ Override update method because of change in resize REST """
        from xlro.core.util.volume_utils import check_for_resize
        check_for_resize(self)
        return super(Volume, self).update()

    @classmethod
    def _map_elect(cls, propmap, source_type=None):
        # ELECT from Mgmt is an ELECT Volume with 'mdv' property.
        # But we need to "re-model" as ELECT containing sub_volumes{} of QLC, MDV and WCV
        if 'sub_volumes' in propmap:
            # We already did this. map_props() can be called lots of times.
            return propmap

        cls.logger.debug('Initialize sub-volumes. {} {} {}'.format(cls.__name__, propmap.get('name', 'unnamed'), propmap.get(cls.TYPE_PROP, 'no-prop-type')))

        # TODO: while this is 'fixed' in IO-Test, it's a hack.
        # Maybe map_props() should always be called with key props?
        # Can't be an instance method, because it's called before object exists.
        name = propmap['name']
        # WCV
        sub_vols = {
                'WCV': {
                    cls.TYPE_PROP: 'Volume',
                    'name': propmap['wcvName'],
                    'vtype': 'WCV'
                }
            }

        # MDV
        mdv = propmap['mdv'].copy()
        mdv.setdefault('name', name + '_MDV')
        mdv.update({
            cls.TYPE_PROP: 'SubVolume',
            'vtype': 'MDV',
        })
        sub_vols['MDV'] = mdv

        # QLC
        qlc = propmap.copy()
        qlc.update({
            cls.TYPE_PROP: 'SubVolume',
            'RAIDlevel': PRaid.RaidLevels.EC,      # Need to differentiate from regular EC?  Or use vtype/class
            'vtype': 'QLC',
        })
        sub_vols['QLC'] = qlc

        propmap['sub_volumes'] = sub_vols
        # TODO: Should we empty the other fields?  The chunks should really be ONLY in the QLC.
        # The "Container" should forward everything to the QLC (or "carrier" volume)
        propmap['chunks'] = []

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(Volume, cls).map_props(propmap, source_type)

        # A kludge due to SDK breaking change... :-(
        if source_type == SourceTypes.MANAGEMENT and 'status' in propmap:
            # Tricky. map_props() can certainly be called with subset of properties, including 'status' only
            # So the kludge is to assume that FIRST load from MGMT including 'status' is a full response, and to set the mode then.
            if cls._action_shadows_status is None:
                cls._action_shadows_status = 'action' not in propmap
            if cls._action_shadows_status:
                propmap.setdefault('action', propmap['status'])

        mdvSpec = propmap.get('mdvSpec')
        if isinstance(mdvSpec, dict):
            propmap['mdvSpec'] = MDVSpec(**mdvSpec)

        if 'capacity' in propmap and isinstance(propmap['capacity'], str):
            try:
                propmap['capacity'] = int(str(propmap['capacity']))
            except ValueError as e:
                if propmap['capacity'] == Volume.MAX_STR:
                    propmap['capacity'] = Volume.MAX_INT
                else:
                    propmap['capacity'] = SdkUtils.convertUnitCapacityToBytes(propmap['capacity'])

        # loops for updating Volume children (Chunk and PRaid)
        chunks = propmap.get('chunks', None)
        if chunks and isinstance(chunks[0], dict):
            name = propmap.get('name', None)
            for chunk in chunks:
                chunk['name'] = name
            if 'pRaids' in chunks[0]:
                raid_level = propmap.get('RAIDlevel')

                disks_count_dict = {'parityDisks': propmap['numberOfMirrors'], 'dataDisks': 1} \
                    if propmap.get('numberOfMirrors', 0) > 0 else {}
                if source_type == SourceTypes.MANAGEMENT and raid_level:
                    if raid_level in ('Concatenated', 'Striped RAID-0'):
                        disks_count_dict.update({'parityDisks': 0, 'dataDisks': 1})
                    if raid_level == 'Erasure Coding':
                        disks_count_dict = {'parityDisks': propmap['parityBlocks'], 'dataDisks': propmap['dataBlocks']}

                for cdict in chunks:
                    key_cdict = {k: cdict[k] for k in Chunk._xlro_keyprops}
                    for pdict in cdict['pRaids']:
                        pdict['chunk'] = key_cdict
                        pdict.update(disks_count_dict)
                        if raid_level:
                            pdict['RAIDlevel'] = propmap['RAIDlevel']

                if 'dataDisks' in pdict:
                    propmap['dataBlocks'] = pdict['dataDisks']
                if 'parityDisks' in pdict:
                    propmap['parityBlocks'] = pdict['parityDisks']

        if 'RAIDlevel' in propmap:
            # dataBlocks and parityBlocks were set as a side-effect of chunks above, which is no longer present by default
            raid_level = propmap.get('RAIDlevel')
            if source_type == SourceTypes.MANAGEMENT and raid_level and raid_level != 'Erasure Coding':
                propmap.setdefault('dataBlocks', 1)
                propmap.setdefault('parityBlocks', propmap.get('numberOfMirrors', 1) if 'Mirror' in raid_level else 0)

        # TODO - adjust set_property to support sequenced props converting properly
        for prop in ('limitByNodes', 'limitByDisks'):
            if prop in propmap and propmap[prop] is not None:
                propmap[prop] = list(map(str, propmap[prop]))
        # TODO - adjust set_property to support converting str to {<key>:<val>} for when expecting BaseEntity
        for prop in ('targetClasses', 'driveClasses'):
            if prop in propmap and propmap[prop] is not None:
                propmap[prop] = [dict(name=val) if isinstance(val, str) else val for val in propmap[prop]]

        if source_type == SourceTypes.MANAGEMENT:
            if "reservation" in propmap:
                res_info = propmap.pop("reservation")
                for mode, id in propmap['mgmt'].reservation_modes.items():
                    if id == res_info["mode"]:
                        propmap["reservation_mode"] = mode
                propmap["reserved_by"] = dict(name=res_info["reservedBy"], mgmt=propmap['mgmt']) if res_info["reservedBy"] else None
                propmap["reservation_version"] = res_info["version"]

            if "nvmf_enabled" not in propmap:
                propmap["nvmf_enabled"] = False

            if "encryption" in propmap:
                propmap["encryption_ready"] = propmap["encryption"].get("isInitialized", False)

        if propmap.get('RAIDlevel', '').upper() == PRaid.RaidLevels.ELECT:
            cls._map_elect(propmap)
        return propmap

    def _get_proc_for_volume(self) -> Optional[str]:
        from xlro.core.entities.manager import Manager
        # order the clients list for 'last_attached_client' to be first if exist
        clients = sorted(Manager.get_manager().clients, key=lambda c: c is self.last_attached_client, reverse=True)
        # an attempt to fetch the 'volume_status_proc' from the 'cached client'
        for client in clients:
            try:
                content = client.proc_for_volume_legacy(self.name, no_cache=True)
                self.last_attached_client = client
                return content
            # second 'bare' Exception meant to catch simulated clients
            except OSError as e:
                self.logger.debug('{}:client - {} failed fetching proc_for_volume: {}'.format(type(e), client, e))
                continue
            # TODO - change to specific Exception
            except Exception as e:
                self.logger.debug('{}:client - {} failed fetching proc_for_volume: {}'.format(type(e), client, e))
                break

        # in case no valid / attached client was found
        self.last_attached_client = None
        return None

    def get_current_attached_clients(self, source: Optional[str] = None, hidden=True) -> List:
        """query all clients attachments and return all attached clients"""
        from xlro.core.entities import Manager
        clients = [c for c in Manager.get_manager().clients if self.name in c.get_property('attachments', source=source, no_cache=True)]
        if hidden:
            return clients
        else:
            return [c for c in clients if not c.attachments[self.name].is_hidden]

    # TODO - replace this 'None' 'proc loader' with 'proc/nvmeibs/toma_status/<some file>' loader
    @prop_loader(SourceTypes.PROC, None)
    def load_from_volume_status(self, client: Optional[Union['Client', str]] = None) -> dict:
        # TODO - don't accept 'client' as input, and remove the code below
        if client:
            from xlro.core.entities import Client

            client_obj = client if client and isinstance(client, Client) else Client.instance(name=client, mgmt=self.mgmt)
            # ! notice - the below might not to work properly (proc_for_volume is called with (default) no_cache=False)
            content: Optional[str] = client_obj.proc_for_volume_legacy(self.name)
        else:
            content = self._get_proc_for_volume()
        if content is None:
            self.logger.debug('no possible client could read proc, returning - {}')
            return {}

        # TODO - consider saving compiled patterns on Volume cls instead
        vpattern = r'\s*Name=(?P<name>.*), UUID=(?P<uuid>[^,]*), size=(?P<blocks>\d+)\[blocks\], (?P<cap_count>\d+)\[(?P<cap_units>[^]]+)\].*Sector Size=(?P<blockSize>\d+).*'
        cpattern = r'^Chunk #\d+: Stripe{Size=(?P<stripeSize>\d+), Width=(?P<stripeWidth>\d+)} (?:Replicas=(?P<replicas>\d+)|Slice{(?P<dataDisks>\d+)\+(?P<parityDisks>\d+)}) .* \[(?P<vlbs>\d+)\.\.(?P<vlbe>\d+)\]\n(?P<self_str>([ \t]+.+\n)+)'
        ppattern = r'^[ \t]*(?P<stripe>\d+)[ \t]*(?P<replica>\d+)[ \t]*(?P<status>\S+)[ \t]*(?P<diskID>\S+)[ \t]*(?P<lbs>\d+)[ \t]*(?P<lbe>\d+)[ \t]*(?P<target>\S+)'

        vmatch = re.match(vpattern, content, re.MULTILINE)
        if vmatch is None:
            raise Exception('failed to parse volume-info on volume status for {} on {}'.format(self.name, client))
        vdict: Dict[str, Any] = vmatch.groupdict()
        chunks: List[Dict] = []
        vdict['chunks'] = chunks
        vdict.update({k: int(vdict[k]) for k in ('blocks', 'cap_count', 'blockSize')})
        for cmatch in re.finditer(cpattern, content, re.MULTILINE):
            if cmatch is None:
                raise Exception('failed to parse chunk-info on volume status for {} on {}'.format(self.name, client))
            cdict = {k: vdict[k] for k in ('name',)}
            cdict.update(cmatch.groupdict())
            self_str = cdict.pop('self_str')
            cdict['dataDisks'] = cdict['dataDisks'] or 1
            cdict['parityDisks'] = cdict['parityDisks'] or int(cdict['replicas']) - 1
            cdict['pRaids'] = []
            key_cdict = {k: cdict[k] for k in ['name', 'vlbs']}
            disks_count_cdict = {k: cdict[k] for k in ('dataDisks', 'parityDisks')}
            for pRaid_index in range(int(cdict['stripeWidth'])):
                pdict = disks_count_cdict.copy()
                pdict['chunk'] = key_cdict
                pdict['uuid'] = PRaid._get_praid_uuid(v_uuid=vdict['uuid'],
                                                      c_lbs=int(cdict['vlbs']),
                                                      p_idx=pRaid_index,
                                                      no_cache=True)
                pdict['diskSegments'] = []
                cdict['pRaids'].append(pdict)

            # TODO - add a volume property to the chunk
            for smatch in re.finditer(ppattern, self_str, re.MULTILINE):
                if smatch is None:
                    raise Exception(
                        'failed to parse segment-info on volume status for {} on {}'.format(self.name, client))
                segprops = {}
                segprops.update(smatch.groupdict())
                segprops['uuid'] = Segment._get_segment_uuid(p_uuid=cdict['pRaids'][int(segprops['stripe'])]['uuid'],
                                                             s_idx=segprops['replica'],
                                                             no_cache=False)
                segprops['drive'] = Drive.instance(name=segprops['diskID'], mgmt=self.mgmt)
                segprops['segmentType'] = 'data'
                # adding the segment dict to a self.diskSegments list according to the stripe index of the segment
                cdict['pRaids'][int(segprops['stripe'])]['diskSegments'].append(segprops)
            chunks.append(cdict)
        if not chunks:
            raise Exception('No chunks found. OUT: {}'.format(content))
        return vdict

    # TODO: Need a better way to handle defaults, and specifically calculated default
    @prop_loader(None, ["stripeSize", "stripeWidth"])
    def load_stripe_info(self, source):
        # Accessing dataBlocks forces a general load of the volume.
        # Otherwise, chance we'll send a default before even trying to load.
        if source in [SourceTypes.DEFAULT, SourceTypes.LOCAL]:
            # Do not trigger load, if only looking for local values
            raise Exception(f'Not triggering Volume.dataBlocks load for {source}')
        dataBlocks = self.dataBlocks
        existing = self.get_property_values('stripeSize', [SourceTypes.MANAGEMENT, SourceTypes.PROC])
        size = existing[0][1] if existing else dataBlocks * self.BLOCK_SET_WIDTH
        existing = self.get_property_values('stripeWidth', [SourceTypes.MANAGEMENT, SourceTypes.PROC])
        width = existing[0][1] if existing else 1
        return {'stripeSize': size, 'stripeWidth': width }

    @classmethod
    def post_create(cls, mgr: 'Manager', objs: List[RE], results: List[Dict]):
        """ Method called after create, to allow updating container - e.g., create-volume might update manager.volumes """
        cls._clear_cached_volumes(mgr)
        super(Volume, cls).post_create(mgr, objs, results)
        for v in objs:
            try:
                if v.isEncrypted and v.autoEncrypt:
                    v.initEncryption(v.AUTO_KEK)
            except Exception as e:
                cls.logger.warning(f'Volume.post_create-auto-encrypt failed: {repr(e)}')
        return

    @classmethod
    def post_delete(cls, mgr: 'Manager', ids: List[str], results: List[Dict]):
        """ Method called after delete, to allow updating container - e.g., delete-volume might update manager.volumes """
        # TODO: This is very wasteful at scale. We have a list of IDs, we could just query those?
        cls._clear_cached_volumes(mgr)
        super(Volume, cls).post_delete(mgr, ids, results)
        return

    @classmethod
    def wait_for_deletion(cls, volumes: Sequence['Volume'], *args: Any, **kwargs: Any) -> WaitResult:
        return cls.wait_for_volumes_statuses(volumes, True, ['Removed'], *args, **kwargs)

    @classmethod
    def wait_for_creation(cls, volumes: Sequence['Volume'], *args: Any, **kwargs: Any) -> WaitResult:
        # TODO: Don't want to touch what's not broken, but do we need to also skip "Initializing" here?
        return cls.wait_for_volumes_statuses(volumes, False, ['Removed'], *args, **kwargs)

    @classmethod
    def wait_for_volumes_statuses(cls, volumes: Sequence['Volume'], is_in_statuses: bool, statuses: List[str],
                                  source: Optional[str] = SourceTypes.MANAGEMENT, is_snapshot=False, **wait_kwargs: Any) -> WaitResult:
        if not volumes:
            return WaitResult(True)
        assert len(set(v.mgmt for v in volumes)) == 1, "{} doesn't have the same mgmt".format(volumes)
        status = 'combined_status' if is_snapshot else 'status'
        refresh_func = None if source != SourceTypes.MANAGEMENT else lambda entities: cls.get_headlines([status], query=dict(name=[str(v.name) for v in entities]),
                                                                                                        mgmt=volumes[0].mgmt)
        return wait_for_property_values(volumes, status, statuses, source,
                                        is_matching=is_in_statuses, refresh_func=refresh_func,
                                        refresh_arg='entities' if source == SourceTypes.MANAGEMENT else None,
                                        non_values=['Removed'], **wait_kwargs)

    @classmethod
    def wait_for_volumes_actions(cls, volumes: List['Volume'], is_in_actions: bool, actions: List[str],
                                 source: Optional[str] = SourceTypes.MANAGEMENT, **wait_kwargs: Any) -> WaitResult:
        if not volumes:
            return WaitResult(True)
        assert len(set(v.mgmt for v in volumes)) == 1, "{} doesn't have the same mgmt".format(volumes)
        refresh_func = None if source != SourceTypes.MANAGEMENT else lambda entities: cls.get_headlines(['status', 'action'], query=dict(name=[str(v.name) for v in entities]),
                                                                                                        mgmt=volumes[0].mgmt)
        return wait_for_property_values(volumes, 'action', actions, source,
                                        is_matching=is_in_actions, refresh_func=refresh_func,
                                        refresh_arg='entities' if source == SourceTypes.MANAGEMENT else None,
                                        non_values=['Removed'], **wait_kwargs)

    @staticmethod
    def _clear_cached_volumes(mgr: Optional['Manager'] = None):
        ''' Clear the cached volumes '''
        # We used to update proactively, but this can cause a lot of # unnecessary traffic.
        from xlro.core.entities.manager import Manager
        m = mgr or Manager.get_manager()
        m.reset_property('volumes')

    def attach(self, client_name: str, wait_till_completed: bool = True, **kwargs: Any) -> Optional[WaitResult]:
        # TODO - change to accept client obj ?
        from xlro.core.entities import Client
        client = Client.instance(name=client_name, mgmt=self.mgmt)
        attach_result = client.attach(volumes=[self], wait_till_completed=wait_till_completed, **kwargs)
        # TODO - decide if to update the attachments prop
        return attach_result

    def wait_for_attach(self, client_name: str, **kwargs: Any) -> WaitResult:
        from xlro.core.entities import Client
        client = Client.instance(name=client_name, mgmt=self.mgmt)
        return client.wait_for_attach(volumes=[self], **kwargs)

    def detach(self, client_name: str, wait_till_completed: bool = True, **kwargs: Any) -> Optional[WaitResult]:
        # TODO - change to accept client obj ?
        from xlro.core.entities import Client
        client = Client.instance(name=client_name, mgmt=self.mgmt)
        # TODO - decide if to update the attachments prop
        detach_result = client.detach(volumes=[self], wait_till_completed=wait_till_completed, **kwargs)
        return detach_result

    def wait_for_detach(self, client_name: str, **kwargs: Any) -> WaitResult:
        # TODO - change client_name to Client ?
        from xlro.core.entities import Client
        client = Client.instance(name=client_name, mgmt=self.mgmt)
        return client.wait_for_detach(volumes=[self], **kwargs)

    @classmethod
    def rebuild_volumes(cls, volumes: List['Volume'], allowAllocationOnOfflineDrives: bool = False) -> List[dict]:
        return cls.do_operation(volumes[0].mgmt, 'rebuild', volumes, allowAllocationOnOfflineDrives=allowAllocationOnOfflineDrives)

    # These operations could be auto-generated via @sdk_entity using rest.yaml...
    def initEncryption(self, passphrase,
            slot: Optional[int] = None, numberOfSlots: Optional[int] = None, keySize: Optional[int] = None):
        return self._do_op()

    def addPassphrase(self, current_passphrase, new_passphrase, slot: Optional[int] = None):
        return self._do_op()

    def rotatePassphrase(self, current_passphrase, new_passphrase, slot: Optional[int] = None):
        return self._do_op()

    def deletePassphrase(self, current_passphrase, slot: Optional[int] = None):
        return self._do_op()

    def get_clba(self, vlba: 'Volume.LBA') -> Tuple[Chunk, Chunk.LBA]:
        """
        input - Volume.addr / tuple or volume object and a 'int' address
        :return: a Chunk.addr / tuple of chunk pbject and 'int' address
        """
        assert isinstance(vlba, Volume.LBA), 'vlba {} is not of type Volume.LBA'.format(vlba)
        assert 0 <= vlba.addr <= self.blocks, "Volume lba address {} is not valid".format(vlba.addr)


        i = 0
        j = len(self.chunks)
        chunk_index = (i + j) // 2
        while j - i > 1:
            chunk_index = (i + j) // 2
            chunk = self.chunks[chunk_index]
            if vlba.addr < chunk.vlbs:
                j = chunk_index
                chunk_index = (i + j) // 2
            else:
                i = chunk_index + 1

        chunk = self.chunks[chunk_index]
        c_addr = vlba.addr - chunk.vlbs
        clba = Chunk.LBA(c_addr)
        return chunk, clba

    def read(self, client_name: str, vlba_obj: Union[LBARange, 'Volume.LBA'], count: int = 1) -> str:
        from xlro.core.entities import Attachment, Client
        client = Client.instance(name=client_name, mgmt=self.mgmt)
        attachment = Attachment.instance(client=client, volume=self)
        return ''.join(str([pg.data for pg in attachment.read(vlba_obj, count)]))

    def get_page(self, vlba: 'Volume.LBA') -> Page:
        assert isinstance(vlba, Volume.LBA), 'vlba {} is not of type Volume.LBA'.format(vlba)
        assert 0 <= vlba.addr <= self.blocks, "Volume lba address {} is not valid".format(vlba.addr)
        chunk, clba = self.get_clba(vlba)
        praid, plba = chunk.get_plba(clba)
        segment, slba, role = praid.get_slba(plba)
        drive, dlba_range = segment.get_dlba_range(slba)
        return Page(drive, dlba_range, role)

    def get_pslice(self, vlba: 'Volume.LBA') -> PSlice:
        assert isinstance(vlba, Volume.LBA), 'vlba {} is not of type Volume.LBA'.format(vlba)
        assert 0 <= vlba.addr <= self.blocks, "Volume lba address {} is not valid".format(vlba.addr)
        chunk, clba = self.get_clba(vlba)
        praid, plba = chunk.get_plba(clba)
        my_slice = praid.get_pslice(plba, vlba)
        return my_slice

    def get_block_set(self, vlba: 'Volume.LBA') -> BlockSet:
        assert isinstance(vlba, Volume.LBA), 'vlba {} is not of type Volume.LBA'.format(vlba)
        assert 0 <= vlba.addr <= self.blocks, "Volume lba address {} is not valid".format(vlba.addr)
        chunk, clba = self.get_clba(vlba)
        praid, plba = chunk.get_plba(clba)
        return BlockSet.plba2block_set(praid, plba)

    def foreign_sdk_type_conversion(self, value: Any, attr: str = None) -> Any:
        if attr in ('driveClasses', 'targetClasses'):
            return [getattr(cls_or_str, 'name', cls_or_str) for cls_or_str in value]

        elif attr == 'capacity' and value == Volume.MAX_INT:
            return Volume.MAX_STR

        # get rid of that! - very mgmt - validation schemes specific condition
        elif attr == 'parityBlocks':
            assert value >= 1, 'parityBlocks must be >= 1'

        return super(Volume, self).foreign_sdk_type_conversion(value, attr)

    @prop_loader(SourceTypes.PROC, ['raid_type', 'status'])
    def _load_raid_type(self):
        from xlro.core.entities import Manager, Attachment

        manager = Manager.get_manager()
        for c in manager.clients:
            try:
                raid_type = c.attachments[self.name].get_property('raid_type', no_cache=True)
                ret = {'raid_type': raid_type}
                if raid_type == Attachment.MULTI_TIER_RAID:
                    ret['status'] = self.ONLINE
            except Exception:
                pass
        raise Exception("couldn't find any client attach to the volume")

    @prop_loader(SourceTypes.PROC, ['snake'])
    def _load_snake(self):
        try:
            snake = int(self.mgmt.clients[0].get_client_parameter('nvmeibc_snake_size_override'))
        except:
            snake = 1

        return {'snake': snake}

    @prop_loader(SourceTypes.MANAGEMENT, ['chunks'])
    def _load_chunks(self):
        ''' Get chunks, which are now projected out by default '''
        #TODO: this can be automated is sdk_base from rest.yaml 'projection'
        # Sending empty list vs. None prevents default projection
        self.get_self(projection_mongo_objs=[])
        # TODO: loader process in base.property() should check for side-effects to avoid re-processing props via map_props
        return {'chunks': self.chunks }

    @prop_loader(SourceTypes.MANAGEMENT, ['combined_status', 'combined_health', 'combined_action','is_snapshot'])
    def _load_combined(self):
        combined_status_route = f'/volumes/combinedStatus/{self.name}'
        err, res = self.mgmt.connection.get(combined_status_route)
        assert not err, f'Unable to get results from {combined_status_route} - {err}'
        return {
            'combined_status': res.get('combinedStatus'),
            'combined_health': res.get('combinedHealth'),
            'combined_action': res.get('combinedAction'),
            'is_snapshot': res.get('isSnapshot', False)
            }

    def snapshot_create_and_attach(self, client: 'Client', wait_for_attached: bool = True, timeout: int = 60):
        if not self.rest_info.kwargs['ops'].get('snapshot-create-and-attach'):
            self.sourceUUID = Volume.instance(name=self.sourceID).uuid
            Volume.wait_for_volumes_statuses([self.create()], True, [Volume.ONLINE], timeout=timeout).assert_result(
                    f'Timed out waiting for snapshot {self.name} to be online')
            res = client.attach([self], wait_for_attached)
            if wait_for_attached:
                res.assert_result(f'Timed out waiting for snapshot attachment {self.name} to be online')
        else:
            # Operations params are currently in "snake" because of CLI.
            # We SHOULD promote this into do_operation, but for now, can't take the risk
            op_params = {raw_to_snake(k):v for k,v in self.get_properties().items() if k != 'mgmt'}
            result = self.do_operation(self.mgmt, 'snapshot-create-and-attach', [self.name], client=client,
                                       source=Volume.instance(name=self.sourceID), timeout=timeout, **op_params)
            try:
                assert result[0]['success'], 'REST API did not return success'
            except Exception as e:
                raise Exception(f'Snapshot create and attach failed or invalid response: {e}, result: {result}')

        if wait_for_attached:
            wait_for_property_values(
                [self], 'combined_status', ['online'], SourceTypes.MANAGEMENT
            ).assert_result(f'Timed out waiting for snapshot {self.name} to be online')

            attachment = client.get_property('attachments',SourceTypes.MANAGEMENT, no_cache=True)[self.name]
            wait_for_property_values([attachment], 'is_snapshot_ready', [True], SourceTypes.MANAGEMENT,timeout=timeout)\
                .assert_result(f"Timed out waiting for snapshot {self.name} for {timeout} sec")

    def snapshot_detach_and_delete(self, wait_for_deletion: bool = True, timeout: int = 60):
        if not self.rest_info.kwargs['ops'].get('snapshot-detach-and-delete'):
            for c in self.mgmt.clients:
                if self.name in c.get_property('attachments', no_cache=True):
                    c.detach([self], wait_till_completed=True).assert_result(
                        f'Timed out waiting for snapshot {self.name} to detach')
                    # snapshot can only be attached to a single client
                    break
            self.delete()
        else:
            result = self.do_operation(self.mgmt, 'snapshot-detach-and-delete', [self], timeout=timeout)
            try:
                assert result[0]['success'], 'REST API did not return success'
            except Exception as e:
                raise Exception(f'Snapshot detach and delete failed or invalid response: {e}, result: {result}')

        if wait_for_deletion:
            Volume.wait_for_deletion([self]).assert_result(f'Timed out waiting for snapshot {self.name} to detach and delete')

    #TODO: these are really expensive functions, all for the purpose of nvmeshcli. need to optimize somehow

    @property
    def dirty_bits(self):
        return sum(s.remainingDirtyBits for c in self.chunks for p in c.pRaids for s in p.diskSegments)

    @property
    def drives(self):
        return set(s.drive for c in self.chunks for p in c.pRaids for s in p.diskSegments)

    @property
    def targets(self):
        return set(d.target for d in self.drives)

    @property
    def targets_(self):
        return set(s.node_id for c in self.chunks for p in c.pRaids for s in p.diskSegments)

    @property
    def drives_(self):
        return set(s.diskID for c in self.chunks for p in c.pRaids for s in p.diskSegments)

@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT])
class CDV(Volume):
    """Capacity Data Volume — a Volume with volumeClass='CDV'.

    Overrides _get_filter so every _sdk_get call (show, dicts_by_name,
    delete pre-fetch) is automatically restricted to CDV volumes.
    """

    @classmethod
    def _get_filter(cls, mgmt=None, **kwargs):
        return [MongoObj('volumeClass', 'CDV')] + super()._get_filter(mgmt, **kwargs)


@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT])
class TPV(Volume):
    """Thin-Provisioned Volume — a Volume with volumeClass='TPV'.

    Overrides _get_filter so every _sdk_get call (show, dicts_by_name,
    delete pre-fetch) is automatically restricted to TPV volumes.
    """

    @classmethod
    def _get_filter(cls, mgmt=None, **kwargs):
        return [MongoObj('volumeClass', 'TPV')] + super()._get_filter(mgmt, **kwargs)

    @property
    def cdv(self):
        return self.tpvConfig.cdvId if self.tpvConfig else None


@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT])
class CDVMgmt(Volume):
    """Allocator-satellite volume — a Volume with volumeClass='CDV_MGMT'.

    Every CDV is created together with a fixed-size '<cdvName>-mgmt' satellite
    volume that holds the allocator metadata (header + extent records).  The
    satellite is managed automatically by the system:
      * Created atomically with its parent CDV (management).
      * Attached EXCLUSIVE_READ_WRITE to the elected allocator TOMA via an
        internal Kafka handshake.
      * Deleted atomically when the parent CDV is deleted.

    It is exposed in the CLI/SDK for inspection only — create, update,
    delete, attach, and detach are not supported by design.  See
    nvmesh-kernel/design/SatelliteVolumeForCDVAlloc.md.
    """

    @classmethod
    def _get_filter(cls, mgmt=None, **kwargs):
        return [MongoObj('volumeClass', 'CDV_MGMT')] + super()._get_filter(mgmt, **kwargs)

    @property
    def parent_cdv(self):
        return self.parentCDVId


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class KeyPair(SDKEntity):
    name : str = PropertySpec(str, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    uuid : str = PropertySpec(str)
    dbUUID : str = PropertySpec(str)
    description : str = PropertySpec(str)
    KEYS_DIR = '/etc/nvmesh/keys/'

    def __init__(self, *args, **kwargs):
        super(KeyPair, self).__init__(*args, **kwargs)

    def install_key(self, client_node):
        path = self.KEYS_DIR + self.name
        key = self.get_key()
        return Connection.err2exc(client_node.host.connection.execute("sudo sh -c 'mkdir -p {} && cat > {}.key <<!\n"
                                                                      "{}\n!'".format(self.KEYS_DIR, path, key)))

    def uninstall_key(self, client_node):
        path = self.KEYS_DIR + self.name
        return Connection.err2exc(client_node.host.connection.execute("sudo rm {}.key".format(path)))

    def get_key(self):
        return json.dumps({"name": self.name, "uuid": self.uuid, "dbUUID": self.dbUUID})

@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class VolumeSecurityGroup(SDKEntity):
    name : str = PropertySpec(str, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    uuid : UUID = PropertySpec(UUID)
    key_pairs : List[str] = PropertySpec([str])
    modifiedBy : str = PropertySpec(str)
    dateModified : str = PropertySpec(str)
    description : str = PropertySpec(str)

    def __init__(self, *args, **kwargs):
        super(VolumeSecurityGroup, self).__init__(*args, **kwargs)

@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.PROC, SourceTypes.MANAGEMENT])
class SubVolume(Volume):
    container : str = PropertySpec(str) # Name of the container volume.  Not a Volume to avoid circular ref.
