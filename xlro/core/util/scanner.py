# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from builtins import str
from builtins import map
from builtins import hex
from xlro.core.util.general_utils import old_div
from builtins import object
import re
from typing import Any,Container,Dict,Iterable,List,Optional,Tuple
from collections import defaultdict
import json

from xlro.core.util.ssh import Connection
from xlro.core.util.monitor import CmdMonitor, PatternHandler, MultiMonitor
from xlro.core.entities import Target, Drive, Volume, Segment
from xlro.core.util.thread_manager import ThreadPoolManager

SCAN_LOCKS_PATH = "/opt/nvmesh/common-repo/tools/scan_locks_ec"

DISK_ID_RE_PATTERN = re.compile("^#{3}\sDisk\s(?P<disk_id>\S+)")


class Lock(object):
    """representation of single lock (per block_set) info
        correlative to 'scan_locks_ec' (full) output"""
    LOCK_RE_PATTERN = re.compile(
        'Blkset 0x(?P<block_set_idx>\w+): all=0x(?P<lock_data>\w+), lock-id=0x(?P<lock_id>\w+):\w, is_stale=(?P<is_stale>\d), is_read=(?P<is_read>\d), txid=0x(?P<tx_id>\w+), dirty=0x(?P<dirty>\w+)')
    PROP2SLOCKS_ARG = {'lock_data': 'set_lock', 'dirty': 'set_dbits', 'tx_id': 'set_txid'}

    def __init__(self, block_set_idx: str, lock_data: str, lock_id: str, is_stale: str, is_read: str, tx_id: str, dirty: str) -> None:
        self.block_set_idx = int(block_set_idx, 16)
        self.lock_data = int(lock_data, 16)
        self.lock_id = int(lock_id, 16)
        self.is_stale = bool(is_stale)
        self.is_read = bool(is_read)
        self.tx_id = int(tx_id, 16)
        self.dirty = int(dirty, 16)

    @classmethod
    def from_event(cls, cmd_mon_event: dict) -> 'Lock':
        """transform a ScanLockMonitor event into Lock obj"""
        return cls(**{k: cmd_mon_event[k] for k in cls.LOCK_RE_PATTERN.groupindex})

    @classmethod
    def from_string(cls, lock_string):
        ldict = {}
        for lprop in lock_string.split(','):
            key, val = tuple(lprop.split(':'))
            try:
                val = hex(int(val))
            except ValueError:
                pass

            ldict[key] = val

        return cls(**ldict)

    def __str__(self):
        return ",".join(["%s:%s" % (key, val) for key, val in self.__dict__.items()])

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        if not isinstance(other, Lock):
            raise NotImplementedError('Unable to compare Lock object with {}'.format(type(other)))
        return vars(self) == vars(other)

    def __ne__(self, other):
        return not self.__eq__(other)


class DriveLockCounts(object):
    """representation of locks summary info per Drive
        correlative to 'scan_locks_ec' (summary) output"""
    def __init__(self, name: str, stales: int = 0, stalessp: int = 0, taken: int = 0, dbits: int = 0, unk_dbits: int = 0, unk_txid: int = 0, resets: int = 0, mem_corrupt: int = 0) -> None:
        self.drive = Drive.instance(name=name)
        self.stales = stales
        self.stalessp = stalessp
        self.taken = taken
        self.dbits = dbits
        self.unk_dbits = unk_dbits
        self.unk_txid = unk_txid
        self.resets = resets
        self.mem_corrupt = mem_corrupt

    def __str__(self):
        return str(self.__dict__)

    def __repr__(self):
        return self.__str__()


def set_lock_txid(value: int, drive: Drive, ranges: Optional[List[Tuple[int, int]]] = None) -> List[Lock]:
    return set_lock_fields(drive, ranges, set_txid=str(value))


def set_lock_dbits(value: int, drive: Drive, ranges: Optional[List[Tuple[int, int]]] = None) -> List[Lock]:
    return set_lock_fields(drive, ranges, set_dbits=str(value))


def set_lock_data(value: int, drive: Drive, ranges: Optional[List[Tuple[int, int]]] = None) -> List[Lock]:
    return set_lock_fields(drive, ranges, set_lock=str(value))


def set_lock_fields(drive: Drive, ranges: Optional[List[Tuple[int, int]]] = None, **scan_lock_flags: Any) -> List[Lock]:
    return get_full_locks_from_drive(drive, ranges, **scan_lock_flags)


def get_full_locks_from_drive(drive: Drive, ranges: Optional[List[Tuple[int, int]]] = None, **scan_lock_flags: Any) -> List[Lock]:
    """By given drive return full list of locks
        if range  is None - return all locks """
    drive2ranges = {drive: ranges or [(0, old_div(drive.blocks, Volume.BLOCK_SET_WIDTH) + 1)]}
    drive_locks_monitor = ScanLocksMonitor(drive2ranges=drive2ranges, host=drive.target.name,
                                           scan_lock_flags=scan_lock_flags)
    drive_locks_monitor.start()
    assert all(not exit_code for exit_code in drive_locks_monitor.wait_all()), 'not all monitors ran successfully'
    drive_locks_monitor.stop()
    return ScanLocksMonitor.get_drive2locks(drive_locks_monitor)[drive]


def run_scan_locks(drive: Drive, ranges: Optional[List[Tuple[int, int]]] = None, **scan_lock_flags: Any) -> DriveLockCounts:
    """run scan lock cmd by given drive and ranges
        scan_locks_flags - dict that contain the argument and its value in order to support new flags
        if range is None - return sum of all drive range"""
    drive2ranges = {drive: ranges or [(0, old_div(drive.blocks, Volume.BLOCK_SET_WIDTH) + 1)]}
    filtered_ranges = convert_drives2ranges_to_filter_str(drive2ranges)
    s_args = ""
    for arg, val in list(scan_lock_flags.items()):
        s_args += " -{} {}".format(arg, val or "")
    out = Connection.err2exc(
        drive.target.connection.execute("sudo {} -json {}  {} ".format(SCAN_LOCKS_PATH, filtered_ranges,
                                                                       s_args)))
    return [DriveLockCounts(**ddict) for k, ddict in json.loads(out).items() if k.startswith('disk_')][0]


def reset_target_locks(target: Target) -> str:
    """
    reset locks on specific target
    :param target: run reset locks on target
    :type target: Target object
    :return: str
    """
    return Connection.err2exc(target.connection.execute("sudo {0} -reset -silent > /dev/null 2>&1".format(SCAN_LOCKS_PATH)))


def convert_drives2ranges_to_filter_str(drive2ranges: Dict[Drive, List[Tuple[int, int]]]) -> str:
    """compose 'scan_locks_ec' formatted 'filter' query str from requested 'drive2ranges' dict"""
    drives_ranges_list = ["{%s=%d:%s}" % (d.name, len(rs), "/".join(['%d-%d' % t for t in rs])) for d, rs in
                          drive2ranges.items()]
    filter_str = "-filter={}{}*".format(len(drives_ranges_list), "".join(drives_ranges_list))
    return filter_str


class ScanLocksMonitor(CmdMonitor):
    """CmdMonitor for getting 'scan_locks_ec' (full) info"""

    def __init__(self, drive2ranges: Dict[Drive, List[Tuple[int, int]]], scan_locks_path: Optional[str] = SCAN_LOCKS_PATH, scan_lock_flags: Optional[Dict[str, str]] = None, *args: Any, **kwargs: Any) -> None:
        scan_locks_cmd = self.build_scan_locks_cmd(drive2ranges, scan_lock_flags, scan_locks_path)
        super(ScanLocksMonitor, self).__init__(scan_locks_cmd, *args, **kwargs)
        self.add_msg_handler(PatternHandler([Lock.LOCK_RE_PATTERN, DISK_ID_RE_PATTERN]))

    @staticmethod
    def build_scan_locks_cmd(drive2ranges, scan_lock_flags, scan_locks_path):
        filter_str = convert_drives2ranges_to_filter_str(drive2ranges)
        scan_lock_flags = scan_lock_flags or {}
        s_args = ""
        for arg, val in list(scan_lock_flags.items()):
            s_args += " -{} {}".format(arg, val if val is not None else "")
        scan_locks_cmd = "sudo {} {} {}".format(scan_locks_path, filter_str, s_args)
        return scan_locks_cmd

    @classmethod
    def get_drive2locks(cls, scan_locks_mon: 'ScanLocksMonitor') -> Dict[Drive, List[Lock]]:
        """converts given ScanLockMonitor events -> drive2locks dict"""
        drive2locks: Dict[Drive, List[Lock]] = defaultdict(list)
        current_drive = None
        for event in scan_locks_mon.events:
            if 'disk_id' in event:
                current_drive = Drive.instance(name=event['disk_id'])
            else:
                assert current_drive, 'Got Lock event before Drive event? {}'.format(event)
                drive2locks[current_drive].append(Lock.from_event(event))

        return drive2locks


def seg2bs_idx_range(seg: Segment) -> Tuple[int, int]:
    """convenience method to convert given Segment to a range of block_sets"""
    seg_bls_ratio = old_div(Volume.BLOCK_SET_WIDTH * Volume.BLOCK_SIZE, seg.drive.blockSize)
    seg_bs_len = old_div((seg.lbe - seg.lbs + 1), seg_bls_ratio)
    seg_bs_st = old_div(seg.lbs, seg_bls_ratio)
    return seg_bs_st, seg_bs_st + seg_bs_len


def split_drive2ranges_by_target(drive2ranges: Dict[Drive, List[Tuple[int, int]]]) -> Dict[Target, Dict[Drive, List[Tuple[int, int]]]]:
    """groups given drive2ranges dict by drives's targets (to run seperate 'scan_locks' on each target)"""
    target2drive2ranges: Dict[Target, Dict[Drive, List[Tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    for drive, ranges in drive2ranges.items():
        target2drive2ranges[drive.target][drive] = list(ranges)
    return target2drive2ranges


def convert_volume_to_drive2ranges(volume: Volume, include_targets: Optional[Container[Target]] = None) -> Dict[Drive, List[Tuple[int, int]]]:
    """compose a 'drive2ranges' dict out of given Volume"""
    drive2ranges = defaultdict(list)
    for seg in [s for c in volume.chunks for p in c.pRaids for s in p.get_dataSegments()]:
        if (not include_targets) or (seg.drive.target in include_targets):
            drive2ranges[seg.drive].append(seg2bs_idx_range(seg))
    return drive2ranges


def unite_dicts(dicts_list: Iterable[dict]) -> dict:
    """return dict which is updated with all given dicts_list with ordered prioritize"""
    united_dict = {}
    # TODO - consider paralleling events -> dict
    for new_dict in dicts_list:
        united_dict.update(new_dict)

    return united_dict


def get_locks_full(drive2ranges: Dict[Drive, List[Tuple[int, int]]], scan_lock_flags: Optional[Dict] = None) -> Dict[Drive, List[Lock]]:
    """return full locks info on all given drive2ranges"""
    target2drive2ranges = split_drive2ranges_by_target(drive2ranges)

    scan_mons = [ScanLocksMonitor(drive2ranges=drives_dict, host=target.name, scan_lock_flags=scan_lock_flags,
                                  log_option=0)
                 for target, drives_dict in target2drive2ranges.items()]
    vol_locks_monitor = MultiMonitor(monitors=scan_mons)

    # notice that a ScanMon that fails running it command won't cause fail, just won't sums its events
    vol_locks_monitor.start()
    vol_locks_monitor.wait_all()
    vol_locks_monitor.stop()

    return unite_dicts(list(map(ScanLocksMonitor.get_drive2locks, scan_mons)))


def get_vol_locks_full(volume: Volume, include_targets: Optional[Container[Target]] = None) -> Dict[Drive, List[Lock]]:
    """return full locks info for given volume on requested targets (or all)"""
    drive2ranges = convert_volume_to_drive2ranges(volume, include_targets)
    return get_locks_full(drive2ranges)


def get_target_locks_summary(target: Target, drive2ranges: Dict[Drive, List[Tuple[int, int]]]) -> Dict[Drive, DriveLockCounts]:
    """return locks summary info for given target according to requested drive2ranges
        calls 'scan_locks_ec' with --json mode"""
    scan_locks_summary_cmd = "sudo {} -json {}".format(SCAN_LOCKS_PATH,
                                                       convert_drives2ranges_to_filter_str(drive2ranges))
    res = Connection.err2exc(target.connection.execute(scan_locks_summary_cmd))
    drive2drive_locks = {Drive.instance(name=ddict['name']): DriveLockCounts(**ddict)
                         for k, ddict in json.loads(res).items() if k.startswith('disk_')}

    return drive2drive_locks


def get_vol_locks_summary(volume: Volume, include_targets: Optional[Container[Target]] = None) -> Dict[Drive, DriveLockCounts]:
    """return locks summary info for given volume on requested targets (or all)
        calls 'scan_locks_ec' with --json mode"""
    drive2ranges = convert_volume_to_drive2ranges(volume, include_targets)
    target2drive2ranges = split_drive2ranges_by_target(drive2ranges)
    return unite_dicts(ThreadPoolManager().map(get_target_locks_summary,
                                               iter(target2drive2ranges.keys()),
                                               iter(target2drive2ranges.values())))
