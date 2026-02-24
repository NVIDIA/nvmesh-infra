# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import re

from xlro.core.util.cli_util import CLIArgumentParser, EntityArg
from xlro.core.util.trace_utils import PagerUtils
from xlro.core.entities import Volume, Client, Host, Drive
from datetime import datetime

DISCOVER_TRACE = 'trace_nvmeibc_disk_discover_reason'
DISK_RELEASED_TRACE = 'trace_disk_nvmeibc_disk_release'
DISK_OK = 'NVMEIBC_DISK_DISCOVER_OK(1)'

TOMA_START_CONFIG_TRACE = 'trace_3_mm_conf_pre'
TOMA_DUMP_CONFIG_TRACE = 'trace_3_mm_conf_buffer_dump'


class TimeSpan(object):
    def __init__(self, start=None, end=None, reason=None):
        self.start = start
        self.end = end
        self.reason = reason
        self.failures = []

    def __repr__(self):
        return f'Connection start: {self.start}  Connection end: {self.end} Release reason: {self.reason} ' \
                   f'Failures until connected: ' + ("\n" if self.failures else "") + "\n".join(self.failures)


def _get_connected_time_spans(traces):
    ret = []
    disconnected = False
    span = TimeSpan()

    for t in traces:
        if t.trace_name == DISCOVER_TRACE and t['disk_discover_op'] != DISK_OK:
            span.failures.append(re.sub(r'\((.*)\)', '', t['disk_discover_op']) + f'({t.datetime}/{t.time_stamp})')
        elif t.trace_name == DISCOVER_TRACE and t['disk_discover_op'] == DISK_OK:
            span.start = t.datetime
        elif t.trace_name == DISK_RELEASED_TRACE and span.start:
            span.end = t.datetime
            span.reason = t['disk_release_op']
            ret.append(span)
            span = TimeSpan()
            disconnected = True
    if span.start and not span.end:
        # means we are still connected to he disk
        span.end = datetime.now()
        ret.append(span)
    if not span.start and not span.end and span.failures:
        disconnected = True
        ret.append(span)
    return ret, disconnected


def process_online(client, volume, fp):
    """
    The api will write transport info into fp and will return True if all disks where online the whole Time else False.
    """
    segments = [s for c in volume.chunks for p in c.pRaids for s in p.get_dataSegments()]
    drives = [s.drive for s in segments]

    multi_inst_filter = PagerUtils.get_multi_instance_idx_filter(client.inst_idx)

    return _process(Host.instance(name=client.name), fp, multi_inst_filter, drives)


def _process(host, fp, multi_inst_filter, drives, **pager_cmd_kwargs):
    ret = True
    needed_traces = PagerUtils.get_has_traces([DISK_RELEASED_TRACE, DISCOVER_TRACE])

    for drive in drives:
        disks_filter = PagerUtils.get_field_eq('DISK_NAME', drive.name)
        cmd = PagerUtils.construct_pager_cmd(filter_query="({}) && ({}) && ({})".
                                             format(needed_traces, disks_filter, multi_inst_filter),
                                             **pager_cmd_kwargs)
        results = PagerUtils.run_pager_cmd(cmd, host)
        spans, disconnected = _get_connected_time_spans(results)
        if disconnected:
            ret = False
        try:
            target = drive.target.name
        except:
            target = "unknown"
        fp.write(f'Time spans for {drive.name} {target}:\n')
        for i, s in enumerate(spans):
            fp.write(str(i) + ') ' + str(s) + '\n')
        fp.write('\n')
    return ret


def _raw_config_to_obj(raw):
    drives = []
    volumes = []
    targets = []
    obj_type = ""
    obj_text = ""
    lines = raw.splitlines()
    for line in lines:
        if line.startswith('DSK:') or line.startswith('VOL:') or line.startswith('NOD:') or line.startswith('CNF:'):
            if obj_type == 'DSK':
                d = re.search(r'DSK:  (?P<disk_name>\S+),.*nodeID=(?P<node_id>\S+)+.*\n.*uuid\=(?P<uuid>\S+).*', obj_text)
                drives.append(d.groupdict()) # type: ignore
            elif obj_type == 'VOL':
                vol_drives = [] # type: ignore
                volume = {'name': re.search(r'VOL:  (?P<vol_name>\S+),', obj_text).group(1), 'segments': vol_drives, # type: ignore
                          'drives': []}
                for d in re.findall(r'SEG:.*\(SEG\)\n\s+diskUUID=(?P<disk_uuid>\S+),', obj_text):
                    vol_drives.append(d)
                volumes.append(volume)
            elif obj_type == 'NOD':
                targets.append(obj_text)
            elif obj_type == 'CNF':
                pass
            obj_type = line.split(':')[0]
            obj_text = line
        else:
            assert obj_type, "no ctx for line"
            obj_text = obj_text + '\n' + line

    for v in volumes:
        for d_uuid in v['segments']:
            for d in drives: # type: ignore
                if d['uuid'] == d_uuid: # type: ignore
                    v['drives'].append(d['disk_name']) # type: ignore
    print(volumes)
    return volumes


def _get_toma_configs(host, **pager_cmd_kwargs):
    ret = []
    needed_traces = PagerUtils.get_has_traces([TOMA_START_CONFIG_TRACE, TOMA_DUMP_CONFIG_TRACE])
    cmd = PagerUtils.construct_pager_cmd(filter_query="{}".
                                         format(needed_traces), channels=['toma.binlog'],
                                         **pager_cmd_kwargs)
    print(cmd)
    results = PagerUtils.run_pager_cmd(cmd, host)

    raw_msgs = []
    raw_msg = ""
    for t in results:
        if t.trace_name == TOMA_START_CONFIG_TRACE:
            if raw_msg:
                raw_msgs.append(raw_msg)
            raw_msg = ""
        if t.trace_name == TOMA_DUMP_CONFIG_TRACE:
            raw_msg += t.message

    for m in raw_msgs:
        c = _raw_config_to_obj(m)
        for v in c:
            # add it as md
            v['raw'] = str(m)
        ret.append(c)

    return ret


def process_offline(path_to_logs, pager_path, volume):
    hostname, _, logs_location = path_to_logs.partition(':')
    multi_inst_filter = PagerUtils.get_multi_instance_idx_filter(0)
    pager_kwargs = {'logs_path': logs_location,
                    'pager_path': pager_path or logs_location + '/pager.py'}
    host = Host.instance(name=hostname)

    configs = _get_toma_configs(host, **pager_kwargs)
    drives = [] # type: ignore
    # find last Toma configuration with the target volume
    for c in reversed(configs):
        if drives:
            break
        for v in c:
            if v['name'] == volume.name:
                drives = v['drives']
                break
    if not drives:
        assert 1, "could not find drives please check logs"

    print("Config is\n {}".format(v['raw']))
    drives = [Drive.instance(name=d) for d in drives]
    return _process(host, sys.stdout, multi_inst_filter, drives,
                    **pager_kwargs)


def main():
    parser = CLIArgumentParser()
    parser.add_argument('-c', '--client', type=EntityArg(Client),
                        help="client to run tool on", required=False)
    parser.add_argument('-v', '--volume', type=EntityArg(Volume),
                        help="volume to run tool on", required=True)
    parser.add_argument('-l', '--logs_path', type=str,
                        help="logs path, implies offline run")
    parser.add_argument('-p', '--pager_path', type=str,
                        help="pager path, implies offline run", default="")

    args = parser.parse_args()
    if args.logs_path:
        process_offline(args.logs_path, args.pager_path, args.volume)
    else:
        process_online(args.client, args.volume, sys.stdout)


if __name__ == '__main__':
    main()
