#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from builtins import str
from builtins import range
from xlro.core.util.general_utils import old_div
from builtins import object
import argparse
import datetime
import glob
import os
import re
import logging

from os import path, mkdir

from raven.utils import json
from typing import Optional, Iterable, Any, DefaultDict
from collections import namedtuple, defaultdict
from xlro.core.entities import Volume, Client, Host, Manager
from collections import namedtuple
from xlro.core.entities import Volume, Client, Host, Manager, Attachment
from xlro.core.util.cli_util import add_common_args, handle_common_args
from xlro.core.util.compare_block_util import compare_slice
from xlro.core.util.di_timeline.addresses_calc import get_vlba_tree, get_ioctl_addr_output
from xlro.core.util.di_timeline.parse_block import parse_remotely
from xlro.core.util.di_timeline.scan_locks import get_blockset_locks
from xlro.core.util.general_utils import dmsg_mtv_info
from xlro.core.util.trace_utils import PagerUtils

PagerQuery = namedtuple("PagerQuery", ("channels", "filter", "time_query"))
logger = logging.getLogger('di_tool')
PARSE_BLOCK_UM_CLIENT_PATH = "/opt/nvmesh/nvmeshum/bin/parse_block"


class TimeLineEntry(object):
    def __init__(self, time, hostname, message, origin, stamp):
        self.time = time
        self.hostname = hostname
        self.message = message
        self.origin = origin
        self.stamp = stamp


def pager_query_to_cmd(query):
    channels, fltr, time = query
    return PagerUtils.construct_pager_cmd(filter_query=fltr, time_query=time, channels=channels)


def query_pager(hostname, query):
    return PagerUtils.run_pager_cmd(pager_query_to_cmd(query), Host.instance(name=hostname))


def _get_short_id_query(volume, client):
    return PagerQuery(["nvmeibc_trace_eter"],
                      " && ".join((PagerUtils.get_field_eq("DEV_NAME", volume.name),
                                   PagerUtils.get_func_filer("dup_topology"),
                                   PagerUtils.get_multi_instance_idx_filter(client.inst_idx))),
                      "")


def _get_goodpath_query(short_id, volume, vlba, client):
    slice = volume.get_pslice(vlba)
    return PagerQuery(["nvmeibc_trace_goodpath", "nvmeibc_trace_long"],
                      " && ".join((PagerUtils.get_field_eq("VOL_ID", short_id),
                                   PagerUtils.get_field_in("VLBA", "{}+{}".format(slice.vlba.addr, volume.dataBlocks)),
                                   PagerUtils.get_multi_instance_idx_filter(client.inst_idx))),
                      "")


def _get_htr_query(volume, vlba_tree, client):
    pad = "0x{:016x}".format(vlba_tree.blkset.more['slba_blkset'])
    return PagerQuery(["nvmeibc_trace_long"],
                      " && ".join((PagerUtils.get_field_like("HTR_PARAM", "V.{}.B.{}*".format(volume.name, pad)),
                                   PagerUtils.get_multi_instance_idx_filter(client.inst_idx))),
                      "")


def _get_praid_topo_query(volume, vlba_tree, client):
    chunk_idx = volume.chunks.index(vlba_tree.chunk.infra_obj)
    raid_idx = vlba_tree.chunk.infra_obj.pRaids.index(vlba_tree.raid.infra_obj)

    fltr1 = " && ".join((PagerUtils.get_field_eq("CHUNK_IDX", chunk_idx),
                         PagerUtils.get_field_eq("PRAID_IDX", raid_idx),
                         PagerUtils.get_field_eq("DEV_NAME", volume.name),
                         PagerUtils.get_has_traces(["trace_1_topology_raid1_is_ioable",
                                                    "t_02_prioperm"]),
                         PagerUtils.get_multi_instance_idx_filter(client.inst_idx)))
    fltr2 = " && ".join((PagerUtils.get_field_eq("DEV_NAME", volume.name),
                         PagerUtils.get_has_traces(["trace_topology_set_active_topology"]),
                         PagerUtils.get_multi_instance_idx_filter(client.inst_idx)))

    # we limit the results by using SI=0 as it should always exists
    return PagerQuery(["nvmeibc_trace_eter"],
                      "({}) || ({})".format(fltr1, fltr2),
                      "")


def _get_btest_dat_file(client, dat_path, dc_logpath):
    if dat_path:
        btest_block_data = path.join(dc_logpath, 'btest_block_data')
        if not path.exists(btest_block_data):
            mkdir(btest_block_data)
        lpath = path.join(btest_block_data, os.path.basename(dat_path))
        client.connection.get_file(dat_path,
                                   lpath)
        return lpath


def _get_pslice(volume, vlba, dc_logpath, folder_name='page_content'):
    cb_path = path.join(dc_logpath, folder_name)
    if not path.exists(cb_path):
        mkdir(cb_path)
    volume.get_pslice(vlba).copy(cb_path)
    return cb_path


def _compare_blocks(client, volume, vlba, slice_dir, dc_logpath):
    cb_path = path.join(dc_logpath, 'compare_blocks')
    if not path.exists(cb_path):
        mkdir(cb_path)
    compare_slice(client, volume, vlba, dc_path=cb_path, slice_folder=slice_dir, cmp_path=client.cmp_blocks_path)
    with open(os.path.join(slice_dir, 'cmp_results'), 'r') as f:
        return f.read()


def _get_parsed_slice(client, slice_dir):
    data_files = sorted([os.path.join(slice_dir, os.path.basename(f)) for f in glob.glob(os.path.join(slice_dir, 'data_*'))],
                        key=lambda k: int(re.match(r".*data_(\d+)", k).group(1)))  # type: ignore
    ret = []
    for idx, f in enumerate(data_files):
        parsed = parse_remotely(Host.instance(name=client.name), f, client.parse_blocks_path)[0]
        with open(os.path.join(slice_dir, 'data_{}_parsed'.format(idx)), 'wb') as fp:
            fp.write(parsed)
        ret.append(parsed)
    return ret


def _find_start_time(parsed_slice):
    dates = []
    for parsed in parsed_slice:
        try:
            match = re.search(r"(\d+-\d+-\d+ \d+:\d+:\d+)", parsed)
            assert match, "couldn't find time in parsed"
            date_time_str = match.group(1)
            dates.append(datetime.datetime.strptime(date_time_str, '%Y-%m-%d %H:%M:%S'))
        except:
            pass
    return sorted(dates)[0], "first block written to slice"


def _find_start_time_no_dbg_di(client, volume):
    JCTL_CMD = "sudo journalctl SYSLOG_IDENTIFIER=\"sudo\" --no-pager -o json | grep -E \"btestEX|fio\" | grep {}".format(
        volume.name)

    out, err, code = client.host.execute(JCTL_CMD)
    last = out.splitlines()[-1]
    return datetime.datetime.fromtimestamp(old_div(int(json.loads(last)["_SOURCE_REALTIME_TIMESTAMP"]), 1000000)), "btest start ran"


def header_section(manager: Manager, volume: Volume, client: Client, vlba: Volume.LBA, logpath: str, dat_path: Optional[str] = None, local: bool = False) -> dict:
    from collections import defaultdict
    section_to_ret: DefaultDict[str, Optional[Any]] = defaultdict(lambda: None)

    logger.debug(f'Running header section function on {client} : {volume}')

    try:
        section_to_ret['volume_proc'] = _get_volume_proc(volume, [client] + manager.clients[:])
    except:
        pass

    section_to_ret['pager_cmds'] = [_get_short_id_query(volume, client)]
    _handle_short_id(manager, section_to_ret, volume)

    try:
        section_to_ret["address_calc"] = get_vlba_tree(volume, vlba)
    except:
        pass

    stdout, err, code = client.exec_vlba_trace(volume, vlba)
    if code == 0:
        section_to_ret['vlba trace steps'] = err
        section_to_ret['vlba trace output'] = stdout

    try:
        section_to_ret["ioctl_response"] = get_ioctl_addr_output(volume,  vlba, client, *manager.clients)
    except:
        pass

    try:
        section_to_ret["locks"] = get_blockset_locks(section_to_ret["address_calc"])
        with open(os.path.join(logpath, 'locks'), 'w') as f:
            f.write(str(section_to_ret["locks"]))
    except:
        pass

    if dat_path:
        local_dat_path = _get_btest_dat_file(client, dat_path, logpath)
        section_to_ret["parsed_dat"] = parse_remotely(Host.instance(name=client.name), local_dat_path,
                client.parse_blocks_path)[0]

    slice_dir = ""
    try:
        slice_dir =_get_pslice(volume, vlba, logpath)
        section_to_ret["cmp_blocks"] = _compare_blocks(client, volume, vlba, slice_dir, logpath)
    except:
        logger.exception("Ignoring failure to collect pslice")
    if slice_dir:
        section_to_ret["parsed_slice"] = _get_parsed_slice(client, slice_dir)

    try:
        other_clients = manager.clients[:]
        other_clients.remove(client)
        section_to_ret["time_diff_from_di_client"] = {c: client.host.time_delta - c.host.time_delta for c in other_clients}
    except:
        pass

    try:
        section_to_ret["start_time"] = _find_start_time(section_to_ret["parsed_slice"])
    except:
        try:
            section_to_ret["start_time"] = _find_start_time_no_dbg_di(client, volume)
        except:
            pass

    try:
        _handle_goodpath_traces(manager, section_to_ret, vlba, volume)
    except:
        pass

    try:
        _handle_htr_traces(manager, section_to_ret, volume)
    except:
        pass

    try:
        _handle_praid_topo_traces(manager, section_to_ret, volume)
    except:
        pass

    return section_to_ret


def _handle_short_id(manager, section_to_ret, volume):
    section_to_ret['client_to_short_id'] = {}
    try:
        for c in manager.clients:
            traces = query_pager(c.name, _get_short_id_query(volume, c))
            if not traces:
                continue
            section_to_ret['client_to_short_id'][c] = traces[-1]['short_id']
    except:
        pass


def _handle_htr_traces(manager, section_to_ret, volume):
    section_to_ret['htrs'] = []
    for c in manager.clients:
        try:
            htr_query = _get_htr_query(volume, section_to_ret["address_calc"], c)
            section_to_ret['pager_cmds'].append(htr_query)
            traces = query_pager(c.name, htr_query)
            section_to_ret['htrs'] += traces
        except:
            pass


def _handle_goodpath_traces(manager, section_to_ret, vlba, volume):
    section_to_ret['goodpaths'] = []
    for c in manager.clients:
        if c not in section_to_ret["client_to_short_id"]:
            continue
        io_query = _get_goodpath_query(section_to_ret["client_to_short_id"][c], volume, vlba, c)
        section_to_ret['pager_cmds'].append(io_query)
        try:
            traces = query_pager(c.name, io_query)
            section_to_ret['goodpaths'] += traces
        except:
            pass


def _handle_praid_topo_traces(manager, section_to_ret, volume):
    from collections import defaultdict
    section_to_ret['praid_topos'] = defaultdict(list)
    vlba_tree = section_to_ret['address_calc']
    chunk_idx = volume.chunks.index(vlba_tree.chunk.infra_obj)
    raid_idx = vlba_tree.chunk.infra_obj.pRaids.index(vlba_tree.raid.infra_obj)

    for c in manager.clients:
        praid_topo_q = _get_praid_topo_query(volume, section_to_ret["address_calc"], c)
        section_to_ret['pager_cmds'].append(praid_topo_q)
        try:
            traces = query_pager(c.name, praid_topo_q)
            # as we are using sticky we have to filter again by volume,chunk,praid
            section_to_ret['praid_topos'][c.name] += [t for t in traces if t.trace_name in
                                                      ('trace_topology_set_active_topology',
                                                       'trace_1_topology_raid1_is_ioable',
                                                       't_02_prioperm')
                                                      and t['volume'] == volume.name and
                                                      (t.trace_name == 'trace_topology_set_active_topology' or
                                                       (t['chunk'] == chunk_idx and t['praid'] == raid_idx))]
        except:
            pass
    pass


def _get_volume_proc(volume: Volume, clients: Iterable[Client]) -> str:
    vol_name = volume.name

    for c in clients:
        try:
            # Multi-instance: shouldn't this use proc_for_volume?  (Which, should, internally do the legacy thing)
            return c.proc_for_volume_legacy(vol_name)
        except:
            pass
    raise Exception("collecting volume proc status failed")


def _dump_header_section(client, volume, vlba, header_dict, dump):
    def _print_section(header, value):
        dump.write("====================\n{}\n====================\n{}\n".format(header, value).encode())

    _print_section("Client", client)
    _print_section("Volume", volume)
    _print_section("Vlba", vlba)

    _print_section("Volume Proc", header_dict['volume_proc'])
    _print_section("Short ID", header_dict['client_to_short_id'])
    _print_section("IOCTL Addresses", header_dict['ioctl_response'])
    _print_section("Infra Addresses Calculation", str(header_dict['address_calc']))

    locks_output = ""
    if header_dict['locks']:
        for drive, locks in header_dict['locks'].items():
            locks_output += "{} ({})\n {}\n".format(drive.name, drive.target.name, locks)
        _print_section("Blockset Locks", locks_output)

    _print_section("Parsed Btest", header_dict['parsed_dat'])
    parsed_output = ""
    for role, parsed in enumerate(header_dict['parsed_slice']):
        parsed_output += "====Role {}:n====\n{}\n".format(role, parsed)
    _print_section("Parsed Slice", parsed_output)

    _print_section("Compare Blocks", header_dict['cmp_blocks'])

    important_greps = ""
    for q in header_dict['pager_cmds']:
        important_greps += "{}\n".format(pager_query_to_cmd(q))
    _print_section("Important Greps", important_greps)

    _print_section("Start Time", str(header_dict['start_time']))

    clients_diff = ""
    if header_dict['time_diff_from_di_client']:
        for c, diff in header_dict['time_diff_from_di_client'].items():
            # days is the only field that can be positive/negative , microseconds are absolute
            clients_diff += "{}: {} microseconds\n".format(c.name, str((diff.days or 1) * diff.microseconds))
    _print_section("Client Diff from DI client", clients_diff)

    _print_section("GoodPath Traces",  "\n".join(["{} {} {}".format(t.hostname, t.time_stamp, t.message) for t in header_dict['goodpaths']]))
    _print_section("HTR Traces", "\n".join(["{} {} {}".format(t.hostname, t.time_stamp, t.message) for t in header_dict['htrs']]))

    if 'vlba trace output' in header_dict and header_dict['vlba trace output']:
        #_print_section("vlba trace steps\n", header_dict['vlba trace steps']) ## for debugging
        _print_section("vlba trace output\n", header_dict['vlba trace output'])

def _topos_to_time_lines(topos_trace):
    def _iter_bulks(traces):
        """
        Generator for each client topology trace bulks
        """
        i = 0
        for next_i in range(len(traces)):
            if traces[next_i].method == '__set_active_topology':
                yield [t for t in traces[i:next_i + 1]]
                i = next_i + 1

    def _print_segment_summary(bulk):
        acm_to_segment = defaultdict(list)
        active_to_segment = defaultdict(list)

        for trace in bulk:
            if trace['acm'] != 'RW ':
                acm_to_segment[trace['acm']].append("S{}".format(trace['segment']))
            if trace['act'] != '1':
                active_to_segment[trace['act']].append("S{}".format(trace['segment']))
        if not acm_to_segment and not active_to_segment:
            summary = "All RW + ACTIVE"
        else:
            summary = "acm {} active {}".format(dict(acm_to_segment), dict(active_to_segment))

        return "client_ver:{} toma_ver:{}: {}".format(bulk[0].topo_debug_id, bulk[0]['version'], summary)

    ret = []
    for bulk in _iter_bulks(topos_trace):
        if len(bulk) <= 1:
            # this is a WA for pager miss entries, bulk should at least be 2 entries(segments info + summary)
            continue
        ret.append(TimeLineEntry(datetime.datetime.fromtimestamp(bulk[-1].time_stamp // 1000000000),
                                 bulk[0].hostname, _print_segment_summary(bulk[:-1]), "pager", bulk[-1].time_stamp))
    return ret


def _handle_mtv(client, volume, vlba):
    attachment = Attachment.instance(client=client, volume=volume)
    try:
        if attachment.raid_type != attachment.MULTI_TIER_RAID:
            return None
    except AttributeError:
        # No raid_type attribute on 1.3
        return None
    attachment.set_destager(0)
    return dmsg_mtv_info(volume, vlba.addr, client)


def main(manager: Manager, volume: Volume, client: Client, vlba: Volume.LBA, logpath: str, dat_path: Optional[str] = None, local: bool = False, more_events: Iterable[dict] = ()) -> None:
    mtv_dict = _handle_mtv(client, volume, vlba)
    if mtv_dict:
        qlc_vol = Volume.instance(name=mtv_dict['qlc_name'])
        qlc_vlba = Volume.LBA(mtv_dict['qlc_vlba'])
        os.mkdir(os.path.join(logpath, qlc_vol.name))
        main(manager, qlc_vol, client, qlc_vlba, os.path.join(logpath, qlc_vol.name), dat_path, local, more_events)

        mdv_vol = Volume.instance(name=mtv_dict['mdv_name'])
        mdv_vlba = Volume.LBA(mtv_dict['mdv_vlba'])
        os.mkdir(os.path.join(logpath, mdv_vol.name))
        main(manager, mdv_vol, client, mdv_vlba, os.path.join(logpath, mdv_vol.name), dat_path, local, more_events)

        # TODO: should run cmp_blocks now with mdv + qlc blocks - waiting for Ofir support it

        wcv_vol = Volume.instance(name=mtv_dict['wcv_name'])

        UINT_MINUS_1 = 18446744073709551615
        if mtv_dict['wcv_vlba'] != UINT_MINUS_1:
            wcv_vlba = Volume.LBA(mtv_dict['wcv_vlba'])
            os.mkdir(os.path.join(logpath, wcv_vol.name))
            main(manager, wcv_vol, client, wcv_vlba, os.path.join(logpath, wcv_vol.name), dat_path, local, more_events)
        return

    header_dict = header_section(manager, volume, client, vlba, logpath, dat_path, local)

    with open(path.join(logpath, "header"), 'wb') as o1:
        _dump_header_section(client.name, volume.name, vlba.addr, header_dict, o1)

    timeline = time_line_section(client, header_dict, manager, more_events)

    with open(path.join(logpath, "timeline"), 'w') as o2:
        for trace in sorted(timeline, key=lambda t: t.time):
            time = trace.time.strftime('%H:%M:%S')
            o2.write("{:<10} {:<10} {:<85} {:<10} {:<10}\n".format(time, trace.hostname.split('.')[0],
                                                                  trace.message.strip(), trace.origin,
                                                                  trace.stamp))


def time_line_section(client, header_dict, manager, more_events=()):
    timeline = []
    for c in manager.clients:
        timeline += _topos_to_time_lines(header_dict['praid_topos'][c.name])
    for t in header_dict['goodpaths']:
        timeline.append(TimeLineEntry(datetime.datetime.fromtimestamp(old_div(t.time_stamp, 1000000000)),
                                      t.hostname, t.message, "pager", t.time_stamp))
    for t in header_dict['htrs']:
        timeline.append(TimeLineEntry(datetime.datetime.fromtimestamp(old_div(t.time_stamp, 1000000000)), t.hostname, t.message,
                                      "pager", t.time_stamp))
    start_time = header_dict.get('start_time', None)
    if start_time:
        timeline.append(TimeLineEntry(start_time[0], client.name, start_time[1], "infra", ""))

    timeline += more_events

    for entry in timeline:
        if header_dict['time_diff_from_di_client']:
            diff = header_dict['time_diff_from_di_client'].get(Client.instance(name=entry.hostname), datetime.timedelta(0))
            entry.time += diff
    return timeline

def _get_vlba_from_dc_event(dc_event: dict) -> int:
    try:
        return int(dc_event['btest_vlba'])
    except:
        return int(dc_event['vlba'])

def handle_io_monitor_event(manager, dc_event, logpath, infra_events=()):
    """
    an hook to be called from tests when needed
    """
    main(manager,
         Volume.instance(name=dc_event['nvmesh_volume_name']),
         Client.instance(name=dc_event['client']),
         Volume.LBA(_get_vlba_from_dc_event(dc_event)),
         logpath,
         dc_event.get('block_data', None),
         more_events=infra_events)


def init_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--volume', type=lambda vname: Volume.instance(name=vname), required=True, help='volume name')
    parser.add_argument('-c', '--client', type=lambda cname: Client.instance(name=cname), required=True, help='client name')
    parser.add_argument('-l', '--local_dir', help='dump data to directory path', default='./failed-io-logs')
    parser.add_argument('-a', '--vlba', type=lambda vaddr: Volume.LBA(int(vaddr)), required=True, help="volume address")
    parser.add_argument('-d', '--dat_path', help="Btest dat path", default=None)
    return parser


if __name__ == '__main__':
    parser = init_argparse()
    add_common_args(parser)
    args = parser.parse_args()
    handle_common_args(args)
    manager = args.manager

    if not os.path.isdir(args.local_dir):
        os.makedirs(args.local_dir)

    main(args.manager, args.volume, args.client, args.vlba, args.local_dir, args.dat_path, False)
