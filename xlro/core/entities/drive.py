#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from future import standard_library

from xlro.core.entities.etypes import Size


standard_library.install_aliases()
from builtins import map
from builtins import range
from builtins import object
import warnings
import re
import uuid
import os
import io
import csv
import sys
import json
PY3 = sys.version_info[0] == 3
if PY3:
    from io import StringIO
else:
    from StringIO import StringIO

from typing import List, Dict, Optional, Iterator, Tuple, Union, Generator, Any, TYPE_CHECKING, Type, Sequence
from packaging import version

if TYPE_CHECKING:
    from xlro.core.util.scanner import DriveLockCounts
    from xlro.core.entities import Manager, Target, Partition, JournalPartition

from xlro.core.entities.base import entity, prop_loader, PropertySpec, SourceTypes, UUIDEntity, NamedEntity
from xlro.core.entities.sdk_base import sdk_entity, SDKEntity, MongoComparison, RE
from xlro.core.entities.volume import Block, Segment, Volume, VOLUME_MDATA_SIZE
from xlro.core.util.lba import HwLBA, LBARange, SwLBA, BaseLBA
from xlro.core.util.ssh import Connection, temp_dir
from xlro.core.util.general_utils import wait_for_property_values
from xlro.core.util.block_objects import PageData


@entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT])
class GPT(UUIDEntity):
    pass


class DriveStatus(object):
    OK = 'Ok'
    ERROR = 'Error'
    FORMAT_ERROR = 'Format_Error'
    EXCLUDED = 'Excluded'
    NOT_INITIALIZED = 'Not_Initialized'
    INGESTING = 'Ingesting'
    FROZEN = 'Frozen'
    FORMATTING = 'Formatting'
    INITIALIZING = 'Initializing'
    MISSING = 'Missing'


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT, SourceTypes.PROC, SourceTypes.OS])
class Drive(SDKEntity):
    IGNORE_OOB = True
    MAX_NVME_READ = 1
    LBA = type('DLBA', (HwLBA,), {})
    name : str = PropertySpec(str, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    target : 'Target' = PropertySpec('Target')
    model : str = PropertySpec(str)
    pci_address : str = PropertySpec(str)
    pci_slot : str = PropertySpec(str)
    blockSize : int = PropertySpec(int)
    Vendor : str = PropertySpec(str)
    metadata : int = PropertySpec(int)
    availableBlocks : int = PropertySpec(int)
    Serial_Number : str = PropertySpec(str)
    usableBlocks : int = PropertySpec(int)
    health : str = PropertySpec(str)
    diskID : str = PropertySpec(str)
    uuid : str = PropertySpec(str)
    status : str = PropertySpec(str)
    blocks : int = PropertySpec(int)
    nodeID : str = PropertySpec(str)
    segments : List[Segment] = PropertySpec([Segment])
    # gpt : GPT = PropertySpec(GPT)
    partitions : List['Partition'] = PropertySpec(['Partition'])
    # TODO - find the suitable sdk name
    seq : int = PropertySpec(int)
    dev_name : str = PropertySpec(str)
    is_inline : bool = PropertySpec(bool)
    # TODO - find the suitable sdk name
    format_type : str = PropertySpec(str)
    evicted : bool = PropertySpec(bool, default=False)
    journal_partition : "JournalPartition" = PropertySpec("JournalPartition")  # for every drive exist max one 'JOURNAL_DATA_PARTITION'
    excluded : bool = PropertySpec(bool)
    formatInProgress : bool = PropertySpec(bool, transient=True, default=False)

    FLBAS_IS_INLINE_DICT: Dict[str, bool] = {'at End of Data LBA': True,
                            'in Separate Contiguous Buffer': False}

    def local_dlba(self, addr: int) -> HwLBA:
        return Drive.LBA(addr, self.blockSize)

    def is_uuid_changed(self, no_cache=False):
        # Ignore UUID changes on Drive.  It's not an out-of-band change as in Volume
        return False

    @property
    def drive_locks(self) -> 'DriveLockCounts':
        from xlro.core.util.scanner import run_scan_locks
        return run_scan_locks(self) # type: ignore

    @classmethod
    def map_props(cls, propmap, source_type=None):
        from xlro.core.entities import Target, Manager
        propmap = super(Drive, cls).map_props(propmap, source_type)
        # TODO - add default source to at BaseEntity whenever calling map_props
        # little different from the rest cause 'name' should overrun 'diskID', and 'diskID' should remain in drive
        if 'name' not in propmap:
            if 'diskID' in propmap:
                propmap['name'] = propmap['diskID']
            elif 'id' in propmap:
                propmap['name'] = propmap['id']

        if 'nodeID' in propmap:
            propmap['target'] = Target.instance(name=propmap['nodeID'], mgmt=propmap.get('mgmt', Manager.get_manager()))

        if 'block_size' in propmap:
            propmap['blockSize'] = propmap.pop('block_size')

        return propmap

    def load_from_proc(self):
        for drive in csv.DictReader(StringIO(self.target.proc_for_drives(no_cache=True))):
            if self.name == drive['id']:
                return drive

    @prop_loader(SourceTypes.PROC, ['target'])
    def load_target(self):
        # TODO - when possible .get_property('targets', source=) should be by input source as well
        # relaying on Manager.targets, by referring to Manager as cluster object rather then MANAGEMENT source
        all_targets = self.mgmt.get_property('targets', no_cache=True)
        for target in all_targets:
            if self in target.get_property('drives', SourceTypes.PROC):
                return {'target': target}

        raise AttributeError('no matching target found')

    @prop_loader(SourceTypes.PROC, ['pci_slot'])
    def load_pci_slot(self):
        cmd = "sudo lspci -s {pci_address} -v | grep 'Physical Slot' | cut -d ':' -f 2".format(pci_address=self.pci_address)
        slot, err, code = Connection.execute_on_host(self.target.name, cmd)
        if code != 0 or not slot:
            raise AttributeError('Could not load property pci_slot, cmd={}, code={}, err={}'.format(cmd, code, err))
        slot = slot.strip().strip('\n')
        return {'pci_slot': slot}

    @prop_loader(SourceTypes.PROC, None)
    def load_from_smart_file(self):
        self.logger.info('LOADING DRIVE FROM SMART on target:{}'.format(self.target))
        if not self.target:
            raise Exception('No target property on Drive: {}. Cannot load properties.'.format(self.name))
        self.target.get_property('drives', SourceTypes.PROC, no_cache=True)
        ret_prop = self.load_from_proc()

        cmd = 'cat /proc/nvmeibs/smart{}'.format(ret_prop['seq'])
        out, err, code = Connection.execute_on_host(self.target.name, cmd)
        if code != 0:
            raise Exception('remote execution failed. Code={}, Cmd={}, Err={}.'.format(code, cmd, err))

        for line in out.strip().split('\n'):
            prop, equals, value = line.partition('=')
            if not equals:
                continue
            ret_prop[prop.lower().replace(' ', '_')] = value
        return ret_prop

    @prop_loader(SourceTypes.PROC, ['is_inline', 'blockSize', 'metadata', 'format_type'])
    def load_from_id_ns(self) -> Dict[str,Any]:
        flbas_pattern = r'flbas.*Metadata Transferred (?P<metadata_transfer_method>[^\n]*).*LBA Format.*Metadata Size: (?P<metadata>\d+).*Data Size: (?P<blockSize>\d+).*(in use)'
        id_ns_cmd = r'sudo nvme id-ns {dev_name} --human-readable'.format(
            dev_name=self.get_property("dev_name", no_cache=True))
        out, err, code = self.target.connection.execute(id_ns_cmd)
        idns_dict = next(re.finditer(flbas_pattern, out, re.S | re.M)).groupdict()
        idns_dict.update({k: int(idns_dict[k]) for k in ('metadata', 'blockSize')})
        idns_dict['is_inline'] = self.FLBAS_IS_INLINE_DICT[idns_dict.pop('metadata_transfer_method')]
        return idns_dict

    def dd_read(self, lba_obj: Union[LBARange, HwLBA], count: int = 1) -> str:
        lba_range = LBARange.get_by_union(lba_obj, count)

        with temp_dir(self.target.name) as dir_path:
            opath = os.path.join(dir_path, 'new_data')
            dd_cmd = "sudo dd if={ipath} of={opath} bs={bs} skip={lba} count={range} oflag=direct iflag=direct" \
                .format(ipath=self.dev_name, opath=opath, bs=lba_range.lbs.blockSize, lba=lba_range.lbs.addr,
                        range=lba_range.n_blocks)
            cat_cmd = "cat {opath}".format(opath=opath)
            del_file_cmd = "sudo rm {opath}".format(opath=opath)

            _, _, _ = Connection.execute_on_host(self.target.name, dd_cmd)
            data, errbuf, code = Connection.execute_on_host(self.target.name, cat_cmd)
            _, _, _ = Connection.execute_on_host(self.target.name, del_file_cmd)
        return data

    def netlink_write(self, start_dlba: int, data: bytes, metadata: bytes = bytes(), is_data_blocks: bool = True) -> None:
        stream = self.target.netlink('write', inbuf=data + metadata, disk_id=self.name,
                                     start_lba=start_dlba, is_hw=0 if is_data_blocks else 1,
                                     data_len=len(data), meta_len=len(metadata) if metadata else 0)
        stream.read()

    def netlink_read_blocks(self, start_dlba: int, block_data_size: int, block_meta_size: int = 0, n_blocks: int = 1, is_data_blocks: bool = True) -> Generator[Block, None, None]:

        more_kwargs = {}
        if not block_data_size:
            more_kwargs['data'] = '/dev/null'
        max_blocks = 512 if version.parse(self.target.netlink_util_version) >= version.parse("1.02") else 32

        blocks_left_to_read = n_blocks
        while blocks_left_to_read > 0:
            blocks_to_read = min(n_blocks, max_blocks)
            data_len = (block_data_size or Volume.BLOCK_SIZE) * blocks_to_read
            meta_len = block_meta_size * blocks_to_read

            stream = self.target.netlink('read', disk_id=self.name,
                                         start_lba=start_dlba, data_len=data_len, meta_len=meta_len,
                                         is_hw=0 if is_data_blocks else 1, **more_kwargs)
            if isinstance(stream, tuple):
                # local netlink gives the buffers directly
                data_buf, meta_buf = stream
            else:
                # A real stream
                meta_buf = Connection.read_bytes_from_socket(stream, meta_len) if self.metadata and meta_len else b''
                if block_data_size:
                    data_buf = stream.read()

            for i in range(blocks_to_read):
                meta_offset = i * block_meta_size
                meta = meta_buf[meta_offset:meta_offset+block_meta_size] if meta_buf else None
                if block_data_size:
                    data_offset = i * block_data_size
                    yield Block(data_buf[data_offset:data_offset+block_data_size], meta)
                else:
                    yield Block('', meta)

            blocks_left_to_read -= blocks_to_read
            start_dlba += blocks_to_read

    def read_blocks(self, lba_obj: Union[LBARange, HwLBA], metadata: int = 0, count: int = 1) -> Generator[Block, None, None]:
        lba_range = LBARange.get_by_union(lba_obj, count)
        assert not metadata or self.blockSize == lba_range.lbs.blockSize, 'You cannot read metadata for mis-matched blocksize.'
        assert lba_range.lbs.blockSize % self.blockSize == 0, 'LBA blocks must be multiples of HW blocks.'
        return self._nvme_read(lba_range, metadata)

    def read_pages(self, lba_obj: Union[LBARange, SwLBA], metadata: int = 0, count: int = 1, data: bool = True) -> Generator[PageData, None, None]:
        range_obj = LBARange.get_by_union(lba_obj, count)
        assert range_obj.lbs.blockSize == Volume.BLOCK_SIZE, "drive block size {} not equal to request block size {}".format(
            range_obj.lbs.blockSize,
            Volume.BLOCK_SIZE)
        blocks = self.netlink_read_blocks(range_obj.lbs.addr, Volume.BLOCK_SIZE if data else 0,
                                          metadata or self.metadata,
                                          n_blocks=range_obj.n_blocks, is_data_blocks=True)
        for block in blocks:
            yield PageData(block.data, block.metadata)

    def _nvme_read(self, lba_range: Union[LBARange], metadata: int = 0) -> Generator[Block, None, None]:
        left_to_read = lba_range.n_blocks
        dlbs = lba_range.lbs.addr
        metadata = metadata or self.metadata
        if self.get_property("is_inline", no_cache=True):
            data_len, meta_len = self.blockSize + self.metadata, 0
        else:
            data_len, meta_len = self.blockSize, self.metadata

        while left_to_read > 0:
            chunk_range = min(64, left_to_read)
            left_to_read -= chunk_range
            nvme_read_cmd = 'for DLBA in {{{dlbs}..{dlbe}}}; \
                            do \
                                sudo nvme read {dev_name} --start-block $DLBA --block-count 0 \
                                    --data-size {data_len} --metadata-size {meta_len} || exit 1; \
                            done'.format(dev_name=self.dev_name, dlbs=dlbs, dlbe=dlbs + chunk_range - 1,
                                         data_len=data_len, meta_len=meta_len)

            stdin, stdout, stderr = Connection.get_connection(self.target.name).spawn(
                nvme_read_cmd, '')
            with warnings.catch_warnings():
                for blk in range(chunk_range):
                    data = Connection.read_bytes_from_socket(stdout, lba_range.lbs.blockSize)
                    meta = stdout.read(metadata)
                    if len(meta) != metadata:
                        raise Exception('read error on {}. {}'.format(self.name, stderr.read()))
                    yield Block(data, meta)
                dlbs += chunk_range

    def read(self, lba_obj: Union[LBARange, HwLBA], count: int = 1, metadata: int = None) -> Generator[Block, None, None]:
        lba_range = LBARange.get_by_union(lba_obj, count)
        return self.netlink_read_blocks(lba_range.lbs.addr, lba_range.blockSize,
                                        metadata if metadata is not None else VOLUME_MDATA_SIZE,
                                        n_blocks=lba_range.n_blocks, is_data_blocks=True)

    def x_read(self, lba_obj: Union[LBARange, HwLBA], count: int = 1) -> Generator[Block, None, None]:
        lba_range = LBARange.get_by_union(lba_obj, count)

        # TODO - get rid of this
        if self.blockSize == 512 and self.metadata:
            raise NotImplementedError('Drive is not supported by NVMesh')

        with temp_dir(self.target.name) as dir_path:
            data_path = os.path.join(dir_path, '{drive}_{dlba}_data'.format(drive=self.name, dlba=lba_range.lbs.addr))
            metadata_path = os.path.join(dir_path, '{drive}_{dlba}_metadata'.format(drive=self.name, dlba=lba_range.lbs.addr))
            left_to_read = lba_range.n_blocks
            dlbs = lba_range.lbs.addr
            while left_to_read > 0:
                chunk_range = min(Drive.MAX_NVME_READ, left_to_read)
                left_to_read -= chunk_range
                data_len = chunk_range * self.blockSize
                metadata_len = chunk_range * self.metadata
                if self.is_inline:
                    data_len += metadata_len
                    metadata_len = 0

                nvme_read_cmd = "sudo nvme read {dev_name} --start-block {dlbs} --block-count {c_range}" \
                    .format(dev_name=self.dev_name, dlbs=dlbs, c_range=chunk_range - 1)
                data_cmd = '--data-size {data_len} --data {data_path}'.format(data_len=data_len, data_path=data_path)
                nvme_read_cmd = "{} {}".format(nvme_read_cmd, data_cmd)
                metadata_cmd = "--metadata-size {metadata_len} --metadata {metadata_path}" \
                    .format(metadata_len=metadata_len, metadata_path=metadata_path)
                nvme_read_cmd = "{} {}".format(nvme_read_cmd, metadata_cmd)

                del_files_cmd = 'sudo rm {data_path} {metadata_path}'.format(data_path=data_path,
                                                                             metadata_path=metadata_path)

                Connection.err2exc(Connection.execute_on_host(self.target.name, nvme_read_cmd))

                cat_data_cmd = "cat {data_path}".format(data_path=data_path)
                cat_metadata_cmd = "cat {metadata_path}".format(metadata_path=metadata_path)

                chunk_data = io.BytesIO(Connection.err2exc(Connection.execute_on_host(self.target.name, cat_data_cmd)).encode())
                chunk_metadata = io.BytesIO(Connection.err2exc(
                    Connection.execute_on_host(self.target.name, cat_metadata_cmd)).encode()) if metadata_len > 0 else chunk_data
                for blk in range(chunk_range):
                    yield Block(chunk_data.read(lba_range.lbs.blockSize), chunk_metadata.read(self.metadata))
                dlbs += chunk_range

                Connection.err2exc(Connection.execute_on_host(self.target.name, del_files_cmd))

    # TODO - need to be deleted
    def copy(self, lba_obj: Union[LBARange, HwLBA], count: int = 1, local_data_path: str = 'copy.data', local_metadata_path: str = 'copy.meta') -> Tuple[str, str]:
        lba_range = LBARange.get_by_union(lba_obj, count)

        with open(local_data_path, 'wb') as df:
            with open(local_metadata_path, 'wb') as mf:
                for blk in self.read(lba_range):
                    df.write(blk.data)
                    mf.write(blk.metadata)
        return local_data_path, local_metadata_path

    """
    def load_gpt_from_fdisk(self):
        raw_gpt = Connection.err2exc(self.target.connection.execute("sudo fdisk -l {}".format(self.dev_name)))
        fdisk_pattern = r"^\s*(?P<partitionIndex>\d+)\s+(?P<dlbs>\d+)\s+(?P<dlbe>\d+)\s+(?P<size>\S+)\s+(?P<type>\w+)\s+(?P<name>\S+)"
        gpt_match_iter = re.finditer(fdisk_pattern, raw_gpt, re.M)
        partition_dict_list = [prtn.groupdict() for prtn in gpt_match_iter]
        gpt_dict = {prtn.pop('name'): prtn for prtn in partition_dict_list}

        return gpt_dict
    """

    @prop_loader(SourceTypes.PROC, ['partitions'])
    def load_partitions_from_target(self):
        # partitions_uuid_pattern = re.compile(r"^(?P<diskID>{}),.*,(?P<uuid>\S*?),\S+$".format(self.name), re.MULTILINE)
        # partitions = [match.groupdict() for match in re.finditer(partitions_uuid_pattern, self.target.proc_for_partitions())]
        # return {'partitions': partitions}
        try:
            # first read from serjio/<disk>/partitions.csv
            serjio_partitions = os.path.join("serjio", self.name, "partitions.csv")
            proc_partitions = self.target.proc_for_partitions(proc_partitions_path=serjio_partitions, no_cache=True)
            partitions = [self.update_serjio_partition_fields(p) for p in csv.DictReader(StringIO(proc_partitions))]

            return {'partitions': partitions}
        except Exception as e:
            csv_dict_reader = csv.DictReader(StringIO(self.target.proc_for_partitions(no_cache=True)))
            return {'partitions': [pdict for pdict in csv_dict_reader if pdict['disk_id'] == self.name]}

    @prop_loader(SourceTypes.PROC, ['journal_partition'])
    def load_journal_partition(self):
        from xlro.core.entities import Partition, JournalPartition
        all_partitions = self.get_property('partitions', no_cache=True)
        for partition in all_partitions:
            if partition.name == Partition.ConstNames.JOURNAL_DATA:  # journal partitions always have the same name
                return {'journal_partition': JournalPartition.instance(jr_partition=partition)}

        raise AttributeError('no journal data partition found')

    def update_serjio_partition_fields(self,prt):
        prt['disk_id'] = self.name
        prt['uuid'] = prt['guid']
        prt['diskUUID'] = uuid.UUID(prt['guid'])
        prt['volname'] = prt['name']
        prt['start'] = prt['start_lba']
        prt['end'] = prt['end_lba']
        return prt

    @classmethod
    def remove_pci(cls, drives):
        for drive in drives:
            drive.target.remove_pci(drive)

    @classmethod
    def rescan_pci(cls, drives):
        for drive in drives:
            drive.target.rescan_pci()

    @classmethod
    def remove_drives(cls, drives):
        for drive in drives:
            drive.target.remove_drive(drive)
        return True

    @classmethod
    def return_drives(cls, drives):
        for drive in drives:
            drive.target.return_drive(drive)
        return True

    @classmethod
    def reset_nvme(cls, drives):
        for drive in drives:
            drive.target.reset_nvme(drive.seq)
        return True

    @classmethod
    def evict_drives(cls, drives: List['Drive']) -> List[dict]:
        return cls.do_operation(drives[0].mgmt, 'evict', [d.name for d in drives])

    @classmethod
    def format_drives(cls, drives, format_type=None):
        return cls.do_operation(drives[0].mgmt, 'format', [d.name for d in drives], formatType=format_type)

    @classmethod
    def _do_operation(cls: Type[RE], mgmt: 'Manager', op: str, entities: Optional[Sequence[Union[str, dict, RE]]] = None, timeout=None, **kwargs) -> List[Dict]:
        ''' On "format", we reset the base_uuid '''
        if op == 'format':
            entities = cls.get_objs(entities, mgmt)
            for e in entities:
                e.base_uuid = None
        return super(Drive, cls)._do_operation(mgmt, op, entities, timeout, **kwargs)

    @classmethod
    def format_drives_and_verify(cls, drives, wait_for_states=None, timeout=None):
        if wait_for_states is None:
            wait_for_states = [DriveStatus.OK]

        Drive.format_drives(drives)
        if timeout is None:
            timeout = min(len(drives) * 30, 900)

        return wait_for_property_values(drives, 'status', wait_for_states, SourceTypes.MANAGEMENT,
                timeout=timeout).assert_result(f'Not all drives were properly formatted in {timeout} seconds')

    @staticmethod
    def bulk_wait_for_allocation_allowed(drives, source=SourceTypes.MANAGEMENT, timeout=60, **kwargs):
        """
        Wait until all given drives can be used for volume allocation.
        The drive must be: status == ok, health == healthy, isPendingFormat == false, and not have the formatInProgress
        property (it only exists during format).
        If any of these conditions aren't met, mgmt won't allocate the drive to a volume (for creating/rebuilding).
        """
        return wait_for_property_values(objects=drives, prop_name='status', values=[DriveStatus.OK], source=source,
                                        timeout=timeout, **kwargs) and \
               wait_for_property_values(objects=drives, prop_name='health', values=['healthy'], source=source,
                                        timeout=timeout, **kwargs) and \
               wait_for_property_values(objects=drives, prop_name='isPendingFormat', values=[False], source=source,
                                        timeout=timeout, **kwargs) and \
               wait_for_property_values(objects=drives, prop_name='formatInProgress', values=[False], source=source,
                                        non_values=[False], timeout=timeout, **kwargs)

    # TODO - can be generalized using parent property (father, and father prop name)
    # @classmethod
    # def get_info(cls, entities=None): AFAIK, not used.

    def write_uncor(self, lba_obj: Union[LBARange, HwLBA], count: int = 1) -> str:
        lba_range = LBARange.get_by_union(lba_obj, count)
        # TODO change to netlink when it will be supported
        cmd = 'sudo nvme write-uncor {drive_name} -s {dlba} -c {count}'.format(drive_name=self.dev_name,
                                                                               dlba=lba_range.lbs.addr,
                                                                               count=int(lba_range.n_blocks)-1)

        return Connection.err2exc(self.target.connection.execute(cmd))

    @property
    def adjusted_model(self) -> str:
        """
        For drive class payload, we need to change the drive self.model to end with underscores instead of spaces.
        """

        i = len(self.model) - 1
        while self.model[i] == " " and i >= 0:
            i -= 1
        i += 1
        adjusted_model_str = self.model[:i] + "_" * (len(self.model) - i)
        return adjusted_model_str

    def proc_for_journal_ranges(self, no_cache=False):
        from xlro.core.entities.journal import JOURNAL_CONSTS
        journals_path = os.path.join(JOURNAL_CONSTS.SERJIO, self.name, JOURNAL_CONSTS.RANGES_CSV)
        return self.target.proc_content(proc_path=journals_path, no_cache=no_cache)

    @property
    def used_capacity(self):
        return f'{Size((self.usableBlocks - self.availableBlocks) * Volume.BLOCK_SIZE if self.usableBlocks else 0)}/{Size(self.usableBlocks * Volume.BLOCK_SIZE)}'


def main():
    from datetime import datetime
    from xlro.core.entities import Drive, Target
    from xlro.core.sdk.Utils import Utils
    from xlro.core.util.cli_util import CLIArgumentParser, EntitiesArg
    argparser = CLIArgumentParser(require_manager=True)

    argparser.add_argument('-t', '--targets', help='Target(s) whose drives to list', type=EntitiesArg('Target'))
    argparser.add_argument('-d', '--drives', help='Drive(s) to list', type=EntitiesArg(Drive))
    argparser.add_argument('-v', '--volumes', help='Volume(s) whose drives to list', type=EntitiesArg('Volume'))
    readargs = argparser.add_mutually_exclusive_group()
    readargs.add_argument('-p', '--pages', default=None, help='Count of pages to read', type=int)
    readargs.add_argument('-b', '--blocks', default=None, help='Count of pages to read', type=int)
    argparser.add_argument('-o', '--offset', default=0, help='Offset to start reading', type=int)
    args = argparser.parse_args()

    mgr = args.manager
    for v in args.volumes or mgr.volumes:
        for c_n, c in enumerate(v.chunks):
            for p_n, p in enumerate(c.pRaids):
                for s_n, s in enumerate(p.diskSegments):
                    d = s.drive
                    if args.drives and d not in args.drives:
                        continue
                    if args.targets and s.drive.target not in args.targets:
                        continue
                    print(f'{v.name} ({c_n}, {p_n}, {s_n}) {s.drive.target.name} {s.diskID} {s.lbs}-{s.lbe} ({d.blockSize}+{d.metadata}) {Utils.convertBytesToUnit((s.lbe-s.lbs)*d.blockSize)}')
                    if args.pages is not None:
                        # Read by logical blocks (4K)
                        count = args.pages if args.pages else (s.lbe - s.lbs - args.offset)
                        dbytes = 0
                        mbytes = 0

                        time_started = datetime.now()
                        for i, block in enumerate(d.read(d.local_dlba(s.lbs + args.offset), count)):
                            dbytes += len(block.data)
                            mbytes += len(block.metadata)
                        elapsed_time = (datetime.now() - time_started).total_seconds()
                        print(f'Bytes read: {Utils.convertBytesToUnit(dbytes)} in {elapsed_time}s')
                    elif args.blocks is not None:
                        # Read by disk blocks
                        print('NVME not currently working.')
                        # for i, block in enumerate(d.read_blocks(d.local_dlba(s.lbs + args.offset), args.blocks)):


if __name__ == '__main__':
    main()
