# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from future import standard_library
standard_library.install_aliases()
from builtins import zip
from builtins import next
from builtins import range
from xlro.core.util.general_utils import old_div
from builtins import object
import enum
import os
import csv
import logging
import pathlib2
import sys
import threading

from collections import defaultdict
PY3 = sys.version_info[0] == 3
if PY3:
    from io import StringIO
else:
    from StringIO import StringIO

from typing import Optional, Generator, Iterable, Union, List, Dict, DefaultDict, Mapping

from xlro.core.entities import Volume, PRaid, Partition, Drive, Client, Host, Manager
from xlro.core.entities.volume import VOLUME_MDATA_SIZE
from xlro.core.entities.base import entity, prop_loader, PropertySpec, SourceTypes, BaseEntity
from xlro.core.util.block_objects import PageData, StaticSliceRange
from xlro.core.util.lba import LBARange, SwLBA, HwLBA
from xlro.core.util.general_utils import host_name
from xlro.core.util.thread_manager import ThreadPoolManager

journal_logger = logging.getLogger('xlro.core.entities.journal')


class JOURNAL_CONSTS(object):
    JR_RANGES = 1024
    JR_PAGES = 512
    SERVER_DPATH = pathlib2.PosixPath('/proc/nvmeibs')
    RANGES_CSV = 'ranges.csv'
    CLIENTS_CSV = 'clients.csv'
    SERJIO = 'serjio'


class JRIStatus(object):
    ALLOCATED = "ALLOCATED"
    ABANDONED = "ABANDONED"
    FREE = "FREE"
    STORED = "STORED"
    TAKEN = "TAKEN"
    RECALLED = "RECALLED"
    RESERVED = "RESERVED"


class TRANSACTION_STATUS(object):
    COMPLETED = "COMPLETED"
    PARTLY = "PARTLY"
    ERROR = "ERROR"


class JournalStatus(enum.Enum):
    READY = "READY"
    NO_METADATA = "NO_METADATA"


class JournalPage(object):
    def __init__(self, page_index, meta_data, journal_entry, data=None):
        # real journal entry fields
        self.meta_data = meta_data
        self.data = data
        # this is our MD for JournalEntry instance
        self.page_index = page_index
        self.journal_entry = journal_entry
        jblock_md_ctype = self.drive.target.host.get_ctype('jblock_md')
        self.md_struct = jblock_md_ctype.from_buffer(bytearray(meta_data))
        self.host = Host.instance(name=self.journal_entry.journal_range.journal_partition.drive.target.name)

    @property
    def j2d(self) -> int:
        try:
            return self.md_struct.j2d  # for older versions support
        except:
            get_j2d = self.host.get_ctype('get_j2d', is_func=True)
            return int(get_j2d(self.md_struct).value)

    @j2d.setter
    def j2d(self, value: int) -> None:
        set_j2d = self.host.get_ctype('set_j2d', is_func=True)
        set_j2d(self.md_struct, value)

    @property
    def tx_id(self) -> int:
        return int(self.md_struct.tx_id)

    @property
    def tx_bmp(self) -> int:
        return int(self.md_struct.tx_bmp)

    @property
    def dlbs(self) -> SwLBA:
        return self.journal_entry.dlbs + self.page_index

    @property
    def drive(self):
        return self.journal_entry.drive

    def read(self) -> Generator[PageData, None, None]:
        return self.drive.read_pages(self.dlbs)

    def __str__(self):
        return "{}_{}".format(self.tx_id, self.j2d)

    def __repr__(self):
        return "JPage idx:{0} Values = (Drive: {1} J2D:{2} TxID:{3} TxBmp: {4})".format(
            self.page_index, self.journal_entry.journal_range.drive.name, self.j2d, self.tx_id, self.tx_bmp)


class JournalEntry(object):
    def __init__(self, jentry_idx, pages, jrange):
        self.jentry_index = jentry_idx
        self.journal_range = jrange
        self.journal_pages = self.pages2jpages(pages)
        self.tx_id = self.journal_pages[0].tx_id

    def pages2jpages(self, pages):
        jpages = []
        for page_idx, block in enumerate(pages):
            # Assuming pages are packed with useful pages starting at 0
            jpage = JournalPage(page_idx, block.metadata, self, data=block.data)
            jpages.append(jpage)
            if not jpage.md_struct.v1.version or not jpage.md_struct.v1.has_next:
                break

        return jpages

    @property
    def dlbs(self) -> SwLBA:
        return self.journal_range.dlbs + self.jentry_index * self.journal_range.client.binje

    @property
    def drive(self):
        return self.journal_range.drive

    def read(self) -> Generator[PageData, None, None]:
        return self.drive.read_pages(LBARange(self.dlbs, n_blocks=len(self.journal_pages)))

    def __repr__(self):
        return "JEntry idx:{}".format(self.jentry_index)


@entity(sourcetypes=[SourceTypes.PROC])
class JournalRange(BaseEntity):
    index : int = PropertySpec(int, key=True)
    journal_partition : "JournalPartition" = PropertySpec("JournalPartition", key=True)
    entries : List[JournalEntry] = PropertySpec([JournalEntry])  # might need a better mapping according to how d2j works
    client_id : int = PropertySpec(int)
    status : str = PropertySpec(str)
    client_uuid : str = PropertySpec(str)
    client_host : str = PropertySpec(str)
    client : Client = PropertySpec(Client)
    unknown_entries : int = PropertySpec(int)
    synced_entries : int = PropertySpec(int)
    free_entries : int = PropertySpec(int)
    abandoned_entries : int = PropertySpec(int)
    taken_entries : int = PropertySpec(int)
    wait_return_entries : int = PropertySpec(int)
    last_allocated : str = PropertySpec(str)
    last_returned : str = PropertySpec(str)
    # TODO: need to replace this const value with n_ents_rng of relevant drive via serjio.csv or other source
    n_blocks = JOURNAL_CONSTS.JR_PAGES

    def __init__(self, *args, **kwargs):
        super(JournalRange, self).__init__(*args, **kwargs)
        dblocks_per_jri = int(old_div(JOURNAL_CONSTS.JR_PAGES * Volume.BLOCK_SIZE, self.drive.blockSize))
        # This was written with Drive.LBA, but that seems wrong. Chaning to SwLBA and using read_pages()
        orig_dlbs = self.drive.local_dlba(self.journal_partition.jr_partition.lbs + (self.index * dblocks_per_jri))
        self.dlbs = SwLBA(HwLBA.convert(orig_dlbs, Volume.BLOCK_SIZE).addr)

    @prop_loader(SourceTypes.PROC, ['entries'])
    def load_entries(self):
        return {'entries': self.get_jres()}

    def get_jres(self, with_data=False):
        """
        From memory issues data is filled only if request explicitly
        """
        entries = []
        read_page_generator = self.drive.read_pages(self.dlbs, count=self.n_blocks, data=with_data)
        binje = self.client.binje
        for jentry_idx in range(0, self.n_blocks, binje):
            jentry_pages = []
            for jpage_idx in range(binje):
                page = next(read_page_generator)
                jentry_pages.append(page)

            entries.append(JournalEntry(jentry_idx // binje, jentry_pages, self))

        return entries

    def read(self) -> Generator[PageData, None, None]:
        """reads all JournalPages of this JournalRange"""
        return self.drive.read_pages(LBARange(self.dlbs, n_blocks=JOURNAL_CONSTS.JR_PAGES))

    @prop_loader(SourceTypes.PROC, ['client'])
    def _load_client(self):
        try:
            client = Manager.get_manager().client_uuid_to_client(self.client_uuid)
        except Exception:
            # we try to match it by old way - just take hostname (won't work for multi instance)
            client = Client.instance(name=self.client_host.split('_')[0])

        return {'client': client}

    @property
    def drive(self):
        return self.journal_partition.drive


@entity(sourcetypes=[SourceTypes.PROC])
class JournalPartition(BaseEntity):
    jr_partition : Partition = PropertySpec(Partition, key=True)
    journals : Mapping[int, JournalRange] = PropertySpec({int: JournalRange})

    @property
    def drive(self) -> Drive:
        return self.jr_partition.drive

    @prop_loader(SourceTypes.PROC, ['journals'])
    def load_journal_ranges(self):
        # check if ranges is exist
        data = {}
        for row in csv.DictReader(StringIO(self.drive.proc_for_journal_ranges(no_cache=True))):
            jri = int(row['index'])
            if 0 > jri > 1023:
                raise Exception("invalid journal index.")
            # parsing need to be changed (csv format bug)
            if row['status'] in [JRIStatus.ALLOCATED, JRIStatus.RESERVED]:
                # row['index'] = jri
                journal_range = JournalRange.instance(index=jri, journal_partition=self)
                journal_range.client_id = int(row['client_id'])
                journal_range.status = row['status']
                journal_range.client_uuid = row['client_uuid']
                journal_range.client_host = row['client_host']
                journal_range.unknown_entries = int(row['unknown_entries'])
                journal_range.synced_entries = int(row['synced_entries'])
                journal_range.free_entries = int(row['free_entries'])
                journal_range.abandoned_entries = int(row['abandoned_entries'])
                journal_range.taken_entries = int(row['taken_entries'])
                journal_range.wait_return_entries = int(row['wait_return_entries'])
                journal_range.last_allocated = row['last_allocated']
                journal_range.last_returned = row['last_returned']
                data[jri] = journal_range
        return {'journals': data}

    def get_journal_range_by_index(self, jri: int) -> JournalRange:
        if 0 > jri > 1023:
            raise Exception("invalid journal index.")
        return JournalRange.instance(journal_partition=self, index=jri)

    @staticmethod
    def get_journal_from_journals_by_client(journal_ranges: Dict[int, JournalRange], client: Client) -> JournalRange:
        for jri, jr_val in journal_ranges.items():
            # until we call host_name as default, this place might fall for no reason
            if host_name(client.name) == jr_val.client_host.split('_')[0]:
                return jr_val
        raise Exception("no Journal exist for {0}".format(client))

    def get_journal_range_by_client(self, client: Client, source: Optional[str] = None, no_cache: Optional[bool] = False) -> JournalRange:
        all_journal_ranges = self.get_property(prop='journals', source=source, no_cache=no_cache)
        return JournalPartition.get_journal_from_journals_by_client(all_journal_ranges, client)


@entity(sourcetypes=[SourceTypes.PROC])
class Transaction(BaseEntity):
    SORT_FIELDS = ['tx_id', 'client', 'client_uuid']
    tx_id : int = PropertySpec(int, key=True)
    journal_entries : list = PropertySpec(list, default=[])
    praid : PRaid = PropertySpec(PRaid, required=True)

    @property
    def tx_bmp(self):
        return self.journal_entries[0].journal_pages[0].tx_bmp

    @property
    def client(self):
        return self.journal_entries[0].journal_range.client

    @property
    def client_uuid(self):
        return self.journal_entries[0].journal_range.client_uuid

    @property
    def status(self):
        return TRANSACTION_STATUS.COMPLETED if Transaction.countSetBits(self.tx_bmp) + self.praid.parityDisks == len(
            self.journal_entries) else TRANSACTION_STATUS.PARTLY

    @staticmethod
    def countSetBits(n):
        count = 0
        while (n):
            count += n & 1
            n >>= 1
        return count

    def __str__(self):
        return 'tx_id#{} {}/{}'.format(self.tx_id, self.client.name, self.client_uuid)


class SliceRangeTransactions(object):
    JOURNAL_RANGES_DEFAULT_STATUSES = (JRIStatus.ALLOCATED, JRIStatus.RESERVED,)

    def __init__(self, slice_range: StaticSliceRange, statuses: Iterable[str] = JOURNAL_RANGES_DEFAULT_STATUSES, client: Optional[Client] = None, with_data: Optional[bool] = False) -> None:
        # configuration fields
        self.statuses = statuses
        self.slice_range = slice_range
        self.with_data = with_data
        self.client = client
        # return field
        self._cuuid2txid2jres: Dict[str, Dict[str, List[JournalEntry]]] = {}
        self.slice_range_transactions: List[Transaction] = []
        self._get()

    def _get_filtered_journal_ranges(self, block_range):
        drive_journal_ranges = block_range.drive.journal_partition.journals
        journal_ranges = []
        for jr in drive_journal_ranges.values():
            if (not self.statuses or jr.status in self.statuses) and (not self.client or self.client == jr.client):
                journal_ranges.append(jr)

        return journal_ranges

    def _get_jres(self, block_range, ret):
        for journal_range in self._get_filtered_journal_ranges(block_range):
            c_uuid = journal_range.client_uuid
            for jre in journal_range.get_jres(with_data=self.with_data):
                for jpage in jre.journal_pages:
                    if block_range.dlba_range.lbs.addr <= jpage.j2d < block_range.dlba_range.lbs.addr + self.slice_range.slice_count:
                        ret.setdefault(c_uuid, defaultdict(list))
                        ret[c_uuid][jre.tx_id].append(jre)
                        break

    def _get(self):
        with ThreadPoolManager() as executor:
            list(executor.map(lambda block_range: self._get_jres(block_range, self._cuuid2txid2jres),
                              self.slice_range.ordered_content))

        # TODO: probably could be done inside the _get_jres loop
        for c_uuid, tx_id2jres in self._cuuid2txid2jres.items():
            for tx_id, jres in tx_id2jres.items():
                self.slice_range_transactions.append(
                    Transaction.instance(tx_id=tx_id, journal_entries=jres, praid=self.slice_range.praid))


