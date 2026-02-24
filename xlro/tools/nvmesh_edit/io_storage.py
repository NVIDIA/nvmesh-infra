#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from builtins import map, hex, str, range, object
import random
import os
import re
import threading
import json
from datetime import datetime
from glob import glob
from shutil import rmtree, copy
from distutils.dir_util import copy_tree
from typing import Iterable
from collections import namedtuple, defaultdict
from tempfile import mkdtemp
from enum import Enum
from getpass import getuser
from xlro.core.entities import Manager, Volume
from xlro.core.entities.journal import SliceRangeTransactions
from xlro.core.util.block_objects import BlockSet, PageData, Page
from xlro.core.util.general_utils import copy_blocks
from xlro.core.util.lba import *
from xlro.core.util.thread_manager import ThreadPoolManager
from xlro.core.util.scanner import Lock, set_lock_fields
from xlro.core.util.trace_utils import PagerUtils
from xlro.core.entities.volume import VOLUME_MDATA_SIZE


class IOOp(Enum):
    # Only ops that modify internal storage or disk?
    READ = 1
    WRITE = 2
    SET = 3
    IMPORT = 4


IOLog = namedtuple('IOLog', ['op', 'vlba_range', 'time'])


class BlockStorageVersion(object):
    def __init__(self, name, description):
        self.name = name
        self.creation_date = datetime.now()
        self.last_modified = self.creation_date
        self.description = description

    def __str__(self):
        return self.name


class DirtyMap(object):
    def __init__(self, blockset):
        self.blockset = blockset
        self.dirty_map = DirtyMap.get_empty_map(self.blockset)
        self.rmbinfo = {}
        self.is_dirty = False

    @staticmethod
    def get_empty_map(blockset):
        return [[None for _ in range(blockset.praid.width)] for _ in range(blockset.SLICE_COUNT)]

    def update_is_dirty(self):
        self.is_dirty = any(self.rmbinfo.values()) or any(any(row) for row in self.dirty_map)

    def set_rmbinfo(self, role, lock):
        self.rmbinfo[role] = lock
        self.update_is_dirty()

    def get_rmbinfo(self, role):
        return self.rmbinfo.get(role, None)

    def set(self, slice, role, to_set):
        self.dirty_map[slice][role] = to_set
        self.update_is_dirty()

    def get(self, slice, role):
        return self.dirty_map[slice][role]

    def clear(self, slices=None, roles=None):
        for s in slices or list(range(self.blockset.SLICE_COUNT)):
            for r in roles or list(range(self.blockset.praid.width)):
                self.dirty_map[s][r] = None

        self.update_is_dirty()  # is_dirty could still be True if map is clean but rmbinfo is dirty

    def clear_rmbinfo(self, role=None):
        self.rmbinfo[role] = None
        self.update_is_dirty()


class BlocksetStorage(object):
    ''' BS Storage Class '''
    RMBINFO = 'rmbinfo'
    JOURNALS = 'journals'
    CONCAT = 'concatenated'
    storage_root = mkdtemp('-nvmesh-edit-storage')
    logger = logging.getLogger('nvmesh-edit-storage')

    def __init__(self, blockset: BlockSet, version: Optional[str] = None, description: Optional[str] = None) -> None:
        self.blockset = blockset
        self.version = BlockStorageVersion(version or 'v0', description)
        self.blxt_dir = os.path.join(self.storage_root, blockset.praid.chunk.name, str(blockset.vlbs.addr))
        self.dir = os.path.join(self.blxt_dir, str(self.version))
        self.dirty_map = DirtyMap(blockset)
        self.audit_records: Dict[Any, List[Dict[str, str]]] = defaultdict(list)
        os.makedirs(self.dir)

    def _read(self, role, br, slices, roles, override):
        if roles and role not in roles:
            return
        blocks = br.read()
        for slice_idx, block in enumerate(blocks):
            if slices and slice_idx not in slices:
                continue

            slice_path = os.path.join(self.dir, str(slice_idx))
            os.makedirs(slice_path, exist_ok=True)
            data_path = os.path.join(slice_path, f"data_{role}")
            metadata_path = os.path.join(slice_path, f"metadata_{role}")
            if os.path.exists(data_path) and os.path.exists(metadata_path) and not override:
                continue

            with open(data_path, 'wb') as dfp, open(os.path.join(slice_path, "metadata_{}".format(role)), 'wb') as mdfp:
                dfp.write(block.data)
                mdfp.write(block.metadata)

            self.dirty_map.clear([slice_idx], [role])

    def read(self, slices: Optional[Iterable[int]] = (), roles: Optional[Iterable[int]] = (), override=False) -> None:
        self.logger.debug(f'reading blockset {self.blockset}. slices: {slices}. roles: {roles}, override: {override}')
        with ThreadPoolManager() as executor:
            list(executor.map(lambda role_br: self._read(role_br[0], role_br[1], slices, roles, override),
                              enumerate(self.blockset.ordered_content)))

    def write(self, slices: Optional[Iterable[int]] = None, roles: Optional[Iterable[int]] = None) -> None:
        pages_data: List[Optional[List[Optional[PageData]]]] = [None] * (max(roles) + 1) if roles else []
        slices = slices or list(range(BlockSet.SLICE_COUNT))
        roles = roles or list(range(self.blockset.praid.width))
        for role in roles:
            segment_page_data: List[Optional[PageData]] = [None] * (max(slices) + 1)
            with ThreadPoolManager() as executor:
                executor.map(lambda slice_idx: segment_page_data.insert(slice_idx, self.get(slice=slice_idx, role=role)), slices)

            pages_data.insert(role, segment_page_data)

        self.blockset.write(pages_data)
        self.dirty_map.clear(slices, roles)

    def journal_dir_name(self, c_uuid: str, tx_id: int, j2d: int) -> str:
        return os.path.join(self.dir, self.JOURNALS, c_uuid, str(tx_id), str(j2d))

    def journal_path_name(self, role: int, c_uuid: str, tx_id: int, j2d: int, is_data: bool = True, verify_path_exist: bool = False) -> str:
        path = self.journal_dir_name(c_uuid, tx_id, j2d)
        if verify_path_exist and not os.path.isdir(path):
            os.makedirs(path)

        fname = ('data_{}' if is_data else 'metadata_{}').format(str(role))
        return os.path.join(path, fname)

    def page_path_name(self, slice: int, role: int, is_data: bool = True) -> str:
        fname = ('data_{}' if is_data else 'metadata_{}').format(str(role))
        return os.path.join(self.dir, str(slice), fname)

    def get_jpage_path_names(self, role: int, c_uuid: str, tx_id: int, j2d: int, data_path: Optional[str] = None, metadata_path: Optional[str] = None) -> Tuple[str, str]:
        role_data_path = data_path or self.journal_path_name(role, c_uuid, tx_id, j2d, verify_path_exist=True)
        role_metadata_path = metadata_path or self.journal_path_name(role, c_uuid, tx_id, j2d, is_data=False)
        return role_data_path, role_metadata_path

    def get_page_path_names(self, slice: int, role: int, data_path: Optional[str] = None, metadata_path: Optional[str] = None) -> Tuple[str, str]:
        role_data_path = data_path or self.page_path_name(slice, role)
        role_metadata_path = metadata_path or self.page_path_name(slice, role, is_data=False)
        if not (os.path.exists(role_data_path) and os.path.exists(role_metadata_path)):
            self.read([slice], [role])
        return role_data_path, role_metadata_path

    def get(self, **get_kwargs):
        if 'c_uuid' in get_kwargs:
            role_data_path, role_metadata_path = self.get_jpage_path_names(**get_kwargs)
        else:
            role_data_path, role_metadata_path = self.get_page_path_names(**get_kwargs)

        with open(role_data_path, 'rb') as rdp, open(role_metadata_path, 'rb') as rmp:
            data = rdp.read()
            metadata = rmp.read()

        return PageData(data, metadata)

    def set_page(self, page_data: PageData, slice: int, role: int) -> None:
        old_page_data = self.dirty_map.get(slice, role) or self.get(slice=slice, role=role)
        role_data_path = self.page_path_name(slice, role)
        with open(role_data_path, 'wb') as rdp:
            rdp.write(page_data.data)

        if page_data.metadata:
            role_metadata_path = self.page_path_name(slice, role, is_data=False)
            with open(role_metadata_path, 'wb') as rmp:
                rmp.write(page_data.metadata)

        if old_page_data.metadata == page_data.metadata and old_page_data.data == page_data.data:
            self.dirty_map.clear([slice], [role])
        else:
            self.dirty_map.set(slice, role, old_page_data)

    def set_jpage(self, page_data: PageData, role: int, c_uuid: str, tx_id: int, j2d: int) -> None:
        role_data_path = self.journal_path_name(role, c_uuid, tx_id, j2d)
        with open(role_data_path, 'wb') as rdp:
            rdp.write(page_data.data)

        if page_data.metadata:
            role_metadata_path = self.journal_path_name(role, c_uuid, tx_id, j2d, is_data=False)
            with open(role_metadata_path, 'wb') as rmp:
                rmp.write(page_data.metadata)

    def read_jpage(self, jpage, role):
        journal_data_path, journal_metadata_path = self.get_jpage_path_names(
            role, jpage.journal_entry.journal_range.client_uuid, jpage.tx_id, jpage.j2d)
        return copy_blocks(jpage.read(), journal_data_path, journal_metadata_path)

    def write_jpage(self, jpage):
        page_data = self.get(role=jpage.pageref.role, c_uuid=jpage.journal_entry.journal_range.client_uuid,
                             tx_id=jpage.tx_id, j2d=jpage.j2d)
        Page(jpage.drive, jpage.dlbs, jpage.pageref.role).write(page_data.data, page_data.metadata)

    def read_rmbinfo(self) -> List[Lock]:
        rmbinfo = self.blockset.get_rmbinfo()
        for role, lock in enumerate(rmbinfo):
            if role == 0 or role >= self.blockset.praid.dataDisks:
                self.set_rmbinfo(role, lock)

        return rmbinfo

    def write_rmbinfo(self) -> None:
        for role in range(self.blockset.praid.width):
            drive = self.blockset.role2drive(role)
            lock = self.get_rmbinfo(role)
            ranges = self.blockset.get_drives2ranges().get(drive)
            scan_lock_flags = {}
            for prop, val in lock.__dict__.items():
                if prop in list(Lock.PROP2SLOCKS_ARG.keys()):
                    try:
                        val = hex(int(val))
                    except ValueError:
                        pass
                    scan_lock_flags[Lock.PROP2SLOCKS_ARG[prop]] = val

            set_lock_fields(drive, ranges, **scan_lock_flags)
            self.dirty_map.clear_rmbinfo(role)

    def role2rmbinfo_path(self, role):
        return os.path.join(self.dir, self.RMBINFO, '{}_{}'.format(self.RMBINFO, role))

    def get_rmbinfo(self, role: int, path: Optional[str] = None) -> Optional[Lock]:
        path = path or self.role2rmbinfo_path(role)
        try:
            with open(path, 'r') as fp:
                lock = fp.read()
            return Lock.from_string(lock)
        except IOError:
            return None

    def set_rmbinfo(self, role: int, lock: Lock) -> None:
        path = self.role2rmbinfo_path(role)
        rmbinfo_dir = os.path.dirname(path)
        if not os.path.isdir(rmbinfo_dir):
            os.makedirs(rmbinfo_dir)

        old_lock = self.dirty_map.get_rmbinfo(role) or self.get_rmbinfo(role)
        if old_lock and lock == old_lock:
            self.dirty_map.clear_rmbinfo(role)
        else:
            self.dirty_map.set_rmbinfo(role, old_lock)

        with open(path, 'w+') as fp:
            fp.write(str(lock))

    @staticmethod
    def get_files_from_path(from_path, to_import):
        if os.path.isfile(from_path) and os.path.basename(from_path) in to_import:
            return [from_path]
        elif os.path.isdir(from_path):
            paths = []
            for ifile in [os.path.join(from_path, ipath) for ipath in to_import]:
                if os.path.isfile(ifile):
                    paths.append(ifile)
            return paths
        return None

    def import_rmbinfo(self, from_path: str) -> None:
        from xlro.tools.nvmesh_edit.util import printStatuses, click_print
        to_import = ['rmbinfo_{}'.format(role) for role in range(self.blockset.praid.width)]
        for file in self.get_files_from_path(from_path, to_import):
            try:
                copy(file, os.path.join(self.dir, self.RMBINFO))
                click_print('Succesfully imported: {}'.format(file), printStatuses.SUCCESS)
            except (IOError, OSError) as e:
                click_print('Failed to import: {} - {}'.format(file, e), printStatuses.FAILURE)

    def export_rmbinfo(self, to_path: str) -> None:
        if not os.path.isdir(to_path):
            os.makedirs(to_path)
        copy_tree(os.path.join(self.dir, self.RMBINFO), to_path)

    def import_jpage(self, from_path: str, slice: int, role: int, c_uuid: str, tx_id: int, j2d: int) -> None:
        from xlro.tools.nvmesh_edit.util import printStatuses, click_print
        to_import = ['data_{}'.format(role), 'metadata_{}'.format(role)]
        for file in self.get_files_from_path(from_path, to_import):
            d_or_m = file.split('/')[-1].split('_')[0]
            try:
                self.validate_fsize(d_or_m, file)
                self.set_jpage(self.get(role=role, c_uuid=c_uuid, tx_id=tx_id, j2d=j2d), role=role, c_uuid=c_uuid, tx_id=tx_id, j2d=j2d)
                copy(file, self.journal_dir_name(c_uuid, tx_id, j2d))
                click_print('Succesfully imported: {}'.format(file), printStatuses.SUCCESS)
            except (IOError, OSError, AssertionError) as e:
                click_print('Failed to import: {} - {}'.format(file, e), printStatuses.FAILURE)

    def export_jpage(self, to_path: str, role: int, c_uuid: str, tx_id: int, j2d: int) -> None:
        if not os.path.isdir(to_path):
            os.makedirs(to_path)
        list(map(lambda f: copy(f, to_path), list(self.get_jpage_path_names(role, c_uuid, tx_id, j2d))))

    @staticmethod
    def validate_fsize(d_or_m, fpath):
        fsize = os.stat(fpath).st_size
        assert d_or_m == 'data' and fsize == Volume.BLOCK_SIZE \
            or d_or_m == 'metadata' and os.stat(fpath).st_size == VOLUME_MDATA_SIZE, \
            'Incompatible {} size ({}) in {}'.format(d_or_m, fsize, fpath)

    def import_geometry(self, from_path: str, slice: Optional[int] = None, role: Optional[int] = None) -> None:
        from xlro.tools.nvmesh_edit.util import printStatuses, click_print
        if slice is None:
            to_path = self.dir
            to_import = ['{}/{}_{}'.format(s, f, r) for s in range(self.blockset.SLICE_COUNT)
                         for r in range(self.blockset.praid.width) for f in ('data', 'metadata')]
        elif role is None:
            to_path = os.path.join(self.dir, str(slice))
            to_import = ['{}_{}'.format(f, r) for r in range(self.blockset.praid.width) for f in ('data', 'metadata')]
        else:
            to_path = os.path.join(self.dir, str(slice))
            to_import = ['data_{}'.format(role), 'metadata_{}'.format(role)]

        for file in self.get_files_from_path(from_path, to_import):
            page_info = file.split('/')[-2:]
            _slice = slice if slice is not None else int(page_info[0])
            split_fname = page_info[-1].split('_')
            _role = role if role is not None else int(split_fname[-1])
            d_or_m = split_fname[0]
            try:
                self.validate_fsize(d_or_m, file)
                page_to_import = self.get(slice=_slice, role=_role, data_path=file) if os.path.basename(file).startswith('data')\
                    else self.get(slice=_slice, role=_role, metadata_path=file)
                self.set_page(page_to_import, slice=_slice, role=_role)
                copy(file, to_path)
                click_print('Succesfully imported: {}'.format(file), printStatuses.SUCCESS)
            except (IOError, OSError, AssertionError) as e:
                click_print('Failed to import: {} - {}'.format(file, e), printStatuses.FAILURE)

    def export_geometry(self, to_path: str, slice: Optional[int] = None, role: Optional[int] = None) -> None:
        if slice is None:
            self.read()
            from_path = self.dir
        elif role is None:
            self.read([slice])
            from_path = os.path.join(self.dir, str(slice))
        else:
            self.read([slice], [role])
            from_path = os.path.join(self.dir, str(slice), '*_{}'.format(role))

        if not os.path.isdir(to_path):
            os.makedirs(to_path)

        if os.path.isdir(from_path):
            copy_tree(from_path, to_path)
        else:
            list(map(lambda f: copy(f, to_path), glob(from_path)))

    def backup(self, name: str) -> str:
        assert '/' not in name, "Backup name cannot contain '/'"
        new_path = os.path.join(self.blxt_dir, name)
        assert not os.path.isdir(new_path), "Version {} already exist".format(name)
        os.makedirs(new_path)
        copy_tree(self.dir, new_path)
        return new_path

    def dirty(self) -> Optional[DirtyMap]:
        if not self.dirty_map.is_dirty:
            return None
        return self.dirty_map

    def history(self) -> List[IOLog]:
        pass

    def restore(self, name: str) -> None:
        ver_dir = os.path.join(self.blxt_dir, name)
        assert os.path.isdir(ver_dir), 'No such version {}'.format(name)
        self.import_geometry(ver_dir)

    def versions(self):
        return [os.path.basename(v) for v in os.listdir(self.blxt_dir)]

    def delete(self):
        rmtree(self.blxt_dir)

    def concat_blockset_segments_files(self):
        concat_path = os.path.join(self.dir, self.CONCAT)
        if not os.path.isdir(concat_path):
            os.makedirs(concat_path)

        copy_tree(os.path.join(self.dir, '0'), concat_path)
        for role in range(self.blockset.praid.width):
            with open(f'{concat_path}/data_{role}', 'wb') as dfd, open(f'{concat_path}/metadata_{role}', 'wb') as mfd:
                for slice in range(1, self.blockset.SLICE_COUNT):
                    path = os.path.join(self.dir, str(slice))
                    with open(f'{path}/data_{role}', 'rb') as _dfd, open(f'{path}/metadata_{role}', 'rb') as _mfd:
                        dfd.write(_dfd.read())
                        mfd.write(_mfd.read())
        return concat_path

    def do_audit(self, ctx, prop='', value=''):
        from xlro.tools.nvmesh_edit.util import get_page_coords_by_ctx, curr_prompt
        audit_rec = {
            'command': ' '.join([curr_prompt() + ctx.command_path.split()[-1], prop, value]),
            'time': str(datetime.now()),
            'user': getuser()
        }
        for coords in get_page_coords_by_ctx(ctx):
            self.audit_records[coords].append(audit_rec)

    def get_audit(self, ctx, keep_records=True):
        from xlro.tools.nvmesh_edit.util import get_page_coords_by_ctx

        nvmesh_edit_cmds = {}
        get_or_pop = 'get' if keep_records else 'pop'
        for coords in get_page_coords_by_ctx(ctx):
            for rec in self.audit_records.__getattribute__(get_or_pop)(coords, []):
                nvmesh_edit_cmds[rec['time']] = rec

        return sorted(nvmesh_edit_cmds.values(), key=lambda rec: rec['time'])

    def commit_audit(self, ctx, host):
        audit_msg = dict()
        audit_msg['nvmesh_edit_id'] = str(random.randint(10000, 99999))
        for audit_rec in self.get_audit(ctx, keep_records=False):
            audit_msg['command'] = audit_rec
            out, err, code = PagerUtils.echo_pager([host], json.dumps(audit_msg))[0][1]
            assert not code, "Unable to echo pager on host {}: {}".format(host.name, err)


if __name__ == '__main__':
    pass
