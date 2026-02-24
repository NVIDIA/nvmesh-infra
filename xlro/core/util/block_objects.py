# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from builtins import str
from builtins import object
import os
from typing import Optional, Tuple, List, Union, Generator, Iterable, TYPE_CHECKING, Type
from collections import OrderedDict
import logging

if TYPE_CHECKING:
    from xlro.core.entities import Volume, Block, PRaid, Drive, Client

from xlro.core.util.lba import HwLBA, LBARange
from xlro.core.util.thread_manager import ThreadPoolManager
from xlro.core.util.general_utils import copy_blocks, old_div

logger = logging.getLogger('block_objects')

class PageData(object):
    """representation of sequenced 'HwBlocks' equal to the return of 'BlockInfo'.read"""
    CODE2STATUS = {0: 'OK', 66: 'BAD_SECTOR', 67: 'WRONG_CRC', 86: 'VIRGIN_DATA', 90: 'LOGICAL_ZERO'}

    def __init__(self, data, metadata=None):
        self.data = data
        self.metadata = metadata

    @classmethod
    def from_blocks(cls, blocks: Iterable['Block']) -> 'PageData':
        from xlro.core.entities import Volume

        data = b''
        metadata = None
        for hw_block in blocks:
            data += hw_block.data
            if hw_block.metadata:
                metadata = metadata or b'' + hw_block.metadata
        dsize = len(data)
        assert dsize == Volume.BLOCK_SIZE, 'data size: {}, expected block size ({})'.format(dsize, Volume.BLOCK_SIZE)
        return cls(data, metadata)

    # TODO: find out limitations and add the same for metadata
    @classmethod
    def pad_data(cls, data, metadata=None):
        d_size = len(data)
        pad_s = Volume.BLOCK_SIZE - d_size
        if pad_s >= 0:
            data += '\0' * pad_s
        else:
            raise IOError('Incompatible page size {}, expected <= {}'.format(d_size, Volume.BLOCK_SIZE))

        return PageData(data, metadata)

    def calc_edic(self, rlba, dbg_di=False, is_parity=False):
        from xlro.core.entities import Client
        DB_DESCRIPTOR_BITS = 4  # TODO: remove after Ofir adds 'NVMEIBC_DP_EC_PMD_BITS_EDIC' to cflags

        client = Client.local_client()
        cflags = client.get_cflags()
        n_data_edic_bits = int(cflags['NVMEIBC_DP_EC_DMD_BITS_EDIC'])
        n_edic_bits = n_data_edic_bits - 2 * DB_DESCRIPTOR_BITS if is_parity else n_data_edic_bits
        _calc_edic = client.host.get_ctype('calc_edic_from_rlba_and_data', is_func=True)
        return int(_calc_edic(rlba, self.data, dbg_di).value) & ((1 << n_edic_bits) - 1)

    def get_page_status(self, rlba, dbg_di=False, is_parity=False):
        from xlro.core.entities import Client

        client_host = Client.local_client().host
        _has_problems_in_block = client_host.get_ctype('infra_has_problems_in_block', is_func=True)
        md_struct = client_host.get_ctype('union__nvmeibc_block_dp_ec_data_block_md'.partition('__')[-1])
        _md_struct = md_struct.from_buffer(bytearray(self.metadata))
        return PageData.CODE2STATUS.get(_has_problems_in_block(rlba, self.data, dbg_di, is_parity, _md_struct), 'UNKNOWN_STATUS')

    def is_never_written(self):
        from xlro.core.entities import Client

        client_host = Client.local_client().host
        _is_never_written = client_host.get_ctype('infra_was_data_never_written', is_func=True)
        md_struct = client_host.get_ctype('union__nvmeibc_block_dp_ec_data_block_md'.partition('__')[-1])
        _md_struct = md_struct.from_buffer(bytearray(self.metadata))
        return _is_never_written(_md_struct)

class BlockRange(object):
    """sequenced HW blocks on specific Drive"""

    def __init__(self, drive: 'Drive', dlba_obj: Union[LBARange, HwLBA], count: int = 1) -> None:
        self.drive = drive
        self.dlba_range = LBARange.get_by_union(dlba_obj, count)

    def read(self) -> Generator['Block', None, None]:
        return self.drive.read(self.dlba_range)

    def copy(self, local_dir: str, suffix: Optional[Union[str, int]] = None) -> Tuple[str, str]:
        if not os.path.isdir(local_dir):
            os.makedirs(local_dir)
        suffix = suffix if suffix is not None else str(self)
        local_data_path = "{}/data_{}".format(local_dir, suffix)
        local_metadata_path = "{}/metadata_{}".format(local_dir, suffix)
        return copy_blocks(self.read(), local_data_path, local_metadata_path)

    def write(self, data: bytes = bytes(), metadata: bytes = bytes(), is_data_blocks: bool = True) -> None:
        self.drive.netlink_write(self.dlba_range.lbs.addr, data, metadata, is_data_blocks)

    def __str__(self):
        return "{target}_{dev}_{dlba_range}" \
            .format(target=self.drive.target.name, dev=self.drive.name, dlba_range=self.dlba_range)

# TODO: Support a SegBlockRange or PageRange - i.e., extended BlockRange which is MDV aware
# support reading data+metadata for an MTV/ELECT


class Page(BlockRange):
    """
        representation of a 'software block'
        -   the new version of the 'BlockInfo'
        -   sequenced Sw blocks representation
        -   include some 'page' unique properties (such as role)
        -   the 'building block' of a 'PSlice' object

    """
    def __init__(self, drive: 'Drive', dlba_obj: Union[HwLBA, LBARange], role: Optional[int] = None) -> None:
        from xlro.core.entities.volume import Volume
        if not isinstance(dlba_obj, LBARange):
            ratio = Volume.BLOCK_SIZE / float(dlba_obj.blockSize)
            assert ratio.is_integer(), 'block size ratio must be integer'
            dlba_obj = LBARange(dlba_obj, int(ratio))

        if dlba_obj.lbs.blockSize != Volume.BLOCK_SIZE:
            assert dlba_obj.lbs.blockSize * dlba_obj.n_blocks == Volume.BLOCK_SIZE, "Illegal LBA range Obj {} not equal to {} ".format(
                dlba_obj.lbs.blockSize * dlba_obj.n_blocks, Volume.BLOCK_SIZE)
            # when change to netlink uncomment
            # dlba_obj.lbs.blockSize = Volume.BLOCK_SIZE
            # dlba_obj.n_blocks = 1

        super(Page, self).__init__(drive, dlba_obj)
        self.role = role

    def read(self):
        return self.drive.read(self.dlba_range, metadata=0 if not self.drive.metadata else None)

    def __str__(self):
        return "Page:role={},drive={},LBA={}".format(self.role, self.drive, self.dlba_range.lbs)


class SliceRange(object):
    """sequenced 'slices' on given praid"""
    BLOCK_CLS: Union[Type[Page], Type[BlockRange]] = BlockRange
    # Calculate per instance
    _vlbs: Optional['Volume.LBA'] = None

    def __init__(self, praid: 'PRaid', slice_idx: int, slice_count: int = 1) -> None:
        self.praid: 'PRaid' = praid
        self.slice_count: int = slice_count
        self.slice_idx: int = slice_idx
        self._content = None

    @property
    def content(self):
        if self._content is None:
            self._content = self.get_content()
        return self._content

    def get_drive_info(self) -> List[Tuple['Drive', LBARange]]:
        from xlro.core.entities import Segment
        return [seg.get_dlba_range(Segment.LBA(self.slice_idx)) for seg in sorted(self.praid.get_dataSegments(), key=lambda ds: ds.pRaidIndex)]

    def get_content(self) -> List[BlockRange]:
        content = []
        for drive, dlbs_range in self.get_drive_info():
            full_dlba_range = LBARange(dlbs_range.lbs, dlbs_range.n_blocks * self.slice_count)
            content.append(self.BLOCK_CLS(drive, full_dlba_range))
        return content

    def read(self) -> List[PageData]:
        return [PageData.from_blocks(br.read()) for br in self.content]

    def copy(self, local_dir: str) -> List[Tuple[str, str]]:
        with ThreadPoolManager() as executor:
            return list(executor.map(lambda i_br: i_br[1].copy(local_dir, i_br[0]), enumerate(self.content)))

    def __str__(self):
        return "{}_from_slice-{}_count_{}" .format(self.praid, self.slice_idx, self.slice_count)

    @property
    def vlbs(self) -> 'Volume.LBA':
        ''' Get Starting VLBA '''
        if self._vlbs is None:
            from xlro.core.entities.volume import Volume
            p = self.praid
            c = p.chunk
            snake = p.volume.snake
            stripe_in_chunk = self.slice_idx // c.stripeSize * c.stripeWidth + p.stripeIndex
            slice_in_stripe = self.slice_idx % c.stripeSize
            self._vlbs = Volume.LBA(c.vlbs
                    + stripe_in_chunk * p.dataDisks * c.stripeSize
                    + (slice_in_stripe // snake) * (p.dataDisks * snake)
                    + slice_in_stripe % snake)
        return self._vlbs


class StaticSliceRange(SliceRange):
    """representation of SliceRange which spread within single rotation (2 blockSets) """
    # TODO - consider making an abstract class

    @property
    def rotational_steps(self) -> int:
        return (self.slice_idx // (BlockSet.ROTATION_HEIGHT * BlockSet.SLICE_COUNT)) % self.praid.width

    @property
    def ordered_content(self) -> List[BlockRange]:
        """return the block_ranges list in correlate order to seg role in slice"""
        # TODO - same as property(rotational_steps) 'TODO'
        return self.content[self.rotational_steps:] + self.content[:self.rotational_steps]

    def read(self) -> List[PageData]:
        with ThreadPoolManager() as executor:
            return list(executor.map(lambda segment: PageData.from_blocks(segment.read()), self.ordered_content))

    def copy(self, local_dir: str, roles: Optional[Iterable[int]] = None) -> List[Tuple[str, str]]:
        if not os.path.isdir(local_dir):
            os.makedirs(local_dir)
        with ThreadPoolManager() as executor:
            to_copy = [(role, block_range) for role, block_range in enumerate(self.ordered_content) if not roles or role in roles]
            return list(executor.map(lambda i_br1: i_br1[1].copy(local_dir, i_br1[0]), to_copy))

    def write(self, slice_range_pages_data: List[Optional[List[Optional[PageData]]]]) -> None:
        """
        :param slice_range_pages_data: a d+p size list where each cell contains a list of PageData to be written or None
        """
        def write_segment(role, block_range):
            if slice_range_pages_data[role]:
                def write_page(pidx, page):
                    if page:
                        page_dlba = LBARange(block_range.dlba_range.lbs + pidx, 1)
                        Page(block_range.drive, page_dlba, role).write(page.data, page.metadata)

                with ThreadPoolManager() as executor:
                    executor.map(lambda pidx_page: write_page(pidx_page[0], pidx_page[1]), enumerate(slice_range_pages_data[role]))

        with ThreadPoolManager(max_workers=len(slice_range_pages_data)) as executor:
            executor.map(lambda role_block_range: write_segment(role_block_range[0], role_block_range[1]), enumerate(self.ordered_content))

    def drive2role(self, drive):
        ordered_drives = [br.drive for br in self.ordered_content]
        return ordered_drives.index(drive)


class BlockSet(StaticSliceRange):
    """SliceRange which describe a single and full block_set"""
    SLICE_COUNT = 32
    ROTATION_HEIGHT = 2

    def __init__(self, praid, block_set_idx):
        super(BlockSet, self).__init__(praid=praid,
                                       slice_idx=block_set_idx * self.SLICE_COUNT,
                                       slice_count=self.SLICE_COUNT)

    def slice(self, relative_slice):
        assert 0 <= relative_slice < self.SLICE_COUNT, 'Invalid slice within blockset: {}'.format(relative_slice)
        return PSlice(self.praid, self.slice_idx+relative_slice)

    @property
    def block_set_idx(self):
        return self.slice_idx // self.SLICE_COUNT

    def get_drives2ranges(self):
        blockset_content = self.ordered_content
        drives2ranges = OrderedDict()
        for page in blockset_content:
            bs_idx = page.dlba_range.lbs.addr // 32
            drives2ranges[page.drive] = [(bs_idx, bs_idx + 1)]

        return drives2ranges

    def get_rmbinfo(self):
        from xlro.core.util.scanner import get_locks_full
        drives2ranges = self.get_drives2ranges()
        locks_info = get_locks_full(drives2ranges, {'verbose': None})
        return [locks_info[drive][0] for drive in list(drives2ranges.keys())]

    def role2drive(self, role):
        return list(self.get_drives2ranges().keys())[role]

    @classmethod
    def vlba2block_set(cls, volume: 'Volume', vlba: 'Volume.LBA') -> 'BlockSet':
        chunk, clba = volume.get_clba(vlba)
        praid, plba = chunk.get_plba(clba)
        return cls.plba2block_set(praid, plba)

    @classmethod
    def plba2block_set(cls, praid: 'PRaid', plba: 'PRaid.LBA') -> 'BlockSet':
        return cls(praid, praid.rotation_calc(plba).sliceIndex // cls.SLICE_COUNT)

    def __str__(self):
        return "Blockset:idx={}:startLba={}".format(self.block_set_idx, self.vlbs)


class PSlice(StaticSliceRange):
    # TODO - change to refer the recent updates and refactor
    """
        -   nvmesh slice representing
        -   'constrained' version of the 'SliceRange' for a single slice
        -   support the same properties and method from the former 'Slice'
        -   'slice_content' property return the ordered 'content' as in former 'Slice'
        -   support a __getitem__ method to address its blocks by roles (via indexing)
    """
    BLOCK_CLS = Page

    def __init__(self, praid, slice_idx, vlba=None):
        super(PSlice, self).__init__(praid, slice_idx)
        # JW: This is BAD. Should probaly be role. VLBA could even be out of Slice!
        self.vlba = vlba

    @property
    def slice_content(self):
        return self.ordered_content

    @property
    def index_in_bs(self):
        return self.slice_idx % BlockSet.SLICE_COUNT

    def __getitem__(self, item):
        return self.slice_content[item]

    @property
    def d_content(self):
        return self.slice_content[:self.praid.dataDisks]

    @property
    def p_content(self):
        return self.slice_content[-self.praid.parityDisks:]

    def blockset(self):
        ''' Return containing blockset '''
        return BlockSet(self.praid, old_div(self.slice_idx, BlockSet.SLICE_COUNT))

    @staticmethod
    def locate_dlba(drive: 'Drive', addr: int, volumes: Optional[List['Volume']] = None) -> Tuple['PSlice', int]:
        ''' Find (slice, role) for given HW addr within given volumes.
            If no volumes given, get all from Manager
            Can't really do vlba, because it could be a Parity block.
        '''
        from xlro.core.entities import Manager, PRaid
        volumes = volumes or Manager.get_manager().volumes
        for v in volumes:
            logger.debug(f'Checking volume: {v}')
            for c in v.chunks:
                logger.debug(f'Checking chunk: {c}')
                for p in c.pRaids:
                    logger.debug(f'Checking praid: {p}')
                    for s in p.diskSegments:
                        logger.debug(f'Check {addr}@{drive} in {s.drive} {s.lbs}-{s.lbe} ({s})')
                        if s.drive == drive and s.lbs <= addr <= s.lbe:
                            # Assuming ADDR and LBS are both HW
                            # Get SW offset in Segment
                            seg_addr = (addr - s.lbs) * (old_div(v.blockSize, drive.blockSize))
                            pslice = PSlice(p, seg_addr)
                            role = [pg.drive for pg in pslice.ordered_content].index(drive)

                            # Double check, for now...
                            page = pslice.ordered_content[role]
                            assert page.drive == drive, 'Drive found does not match requested drive!'
                            dr = page.dlba_range
                            assert dr.lbs.addr <= addr < (dr.lbs.addr + dr.n_blocks), \
                                    'Addr {} not on page {}!'.format(addr, dr)

                            return pslice, role
        raise Exception('Invalid drive/addr: {}/{}'.format(drive.name, addr))

    def __str__(self):
        return "Slice:idx={}".format(self.slice_idx)
