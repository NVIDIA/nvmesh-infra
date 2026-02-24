# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import division
from builtins import chr
from builtins import str

from xlro.core.util.general_utils import old_div
from os import path
import re
import os
import time

from typing import Dict, Any

from xlro.core.util import monitor, ssh
from xlro.core.entities import Client, Attachment, Volume, K8sClient, Host, Node
from xlro.core.util.general_utils import convert_size_to_bytes, wait_for_it, StopWait, WaitResult, dmsg_mtv_info
from collections.abc import Sequence
from threading import Lock
from collections import defaultdict
from xlro.core.util.monitor import PidMonitor, Action
from xlro.core.util.ssh import Connection
from xlro.core import infra_conf


class IOCmdMonitor(PidMonitor):
    def __init__(self, name, cmd, debug_di, mgmt=None, **monitor_kwargs):
        from xlro.core.entities import Manager

        monitor_kwargs.setdefault('stop_targets', 'leaves')
        super(IOCmdMonitor, self).__init__(cmd, **monitor_kwargs)

        self.name = name
        self.mgmt = mgmt or Manager.get_manager()

        # We no longer support dynamic changes to debug_di.  The volume must be created in the proper mode.
        volume = Volume.instance(name=self.event_defaults['volume'], mgmt=self.mgmt)
        assert debug_di is None or bool(debug_di) == bool(volume.use_debug_di), \
            f'IOCmdMonitor: volume: {volume.name} use_debug_di={volume.use_debug_di} conflicts with parameter debug_di={debug_di}'


class IOMonitor(monitor.MultiMonitor):
    PANIC_TYPE = 'PANIC'
    IO_TYPE = 'IO'
    DI_TYPE = 'DI'
    DUMP_BLOCK_TYPE = 'block_data'
    CACHE_LOCK = Lock()

    def __init__(self, clients, volume_paths, io_tool_args, io_tool_path, logdir, duration, mgmt=None):
        from xlro.core.entities import Manager

        super(IOMonitor, self).__init__()
        self.clients = clients
        self.volume_paths = volume_paths
        self.io_tool_args = io_tool_args
        self.io_tool_path = io_tool_path
        self.logdir = logdir
        self.duration = duration
        self.mgmt = mgmt or Manager.get_manager()

        self._io_started_dict = defaultdict(set)
        self._cached_di_events = set()
        self.is_io_started = False
        self.io_performance = defaultdict(str)

    def pending_io(self) -> Dict[str, Any]:
        # TODO: Move the individual implementations here.
        return {}

    def panics(self):
        return [event for event in self.events if self.PANIC_TYPE in event]

    @staticmethod
    def job_name(client_name, vname):
        return '{}-{}'.format(client_name, vname)

    @staticmethod
    def log(job_name='IOMonitor'):
        return '{}.log'.format(job_name)

    def on_event(self, event: Dict) -> None:
        if self.DI_TYPE in event:
            di_volume_tuple = (event['nvmesh_volume_name'], '')
            di_event_tuple = (event['nvmesh_volume_name'], event.get('vlba', ''))
            with self.CACHE_LOCK:
                run_di = di_volume_tuple not in self._cached_di_events and di_event_tuple not in self._cached_di_events
                if run_di:
                    self._cached_di_events.add(di_event_tuple)
            if run_di:
                self.di_event_handle(event)

        super(IOMonitor, self).on_event(event)

    def di_event_handle(self, event):
        client = Client.instance(name=event['host'], mgmt=self.mgmt)
        volume = Volume.instance(name=event['nvmesh_volume_name'])
        vlba_str = ''
        if event.get('vlba', False):
            vlba_str = "{}".format(event['vlba'])
        for mon in [mon for mon in self.all_monitors() if hasattr(mon, 'prev_status') and
                                                          mon.event_defaults['volume'] == event['volume'] and
                                                          mon.event_defaults['client'] == event['client']]:
            mon.prev_status = None
            data_collect_events = [e for e in mon.events if self.DUMP_BLOCK_TYPE in e]
            if data_collect_events:
                event[self.DUMP_BLOCK_TYPE] = str(data_collect_events[0][self.DUMP_BLOCK_TYPE].strip())

        client.di_stop(volume, vlba_str)
        if volume.sub_volumes:
            event['mtv_info'] = defaultdict(dict)
            for client in volume.get_current_attached_clients():
               event['mtv_info'][client.name] = dmsg_mtv_info(volume, event['vlba'], client)

    def reset(self):
        super(IOMonitor, self).reset()
        self._io_started_dict.clear()
        self.is_io_started = False

    def reset_is_io_started(self):
        self._io_started_dict.clear()
        self.is_io_started = False

    def check_for_io_started(self) -> bool:
        # TODO: Need to refactor this together with pending_io, and pull the combined/redundant code out of io_test.py
        if self.failed_stopped_monitors():
            for m in self.failed_stopped_monitors():
                if isinstance(m, monitor.CmdMonitor):
                    if m.process is None:
                        self.logger.info(f'{m}: stopped?')
                    else:
                        code = m.process.poll()
                        self.logger.info(f'{m}: Exit code: {m.process.poll()}, Log tail:{os.linesep}{os.linesep.join(m.tail)}')
                else:
                    self.logger.info(f'Non-CMD Monitor: {m}')
            raise StopWait(f'I/O command(s) failed in monitor: {self}')
        return self.is_io_started

    def wait_for_io(self, **wait_kwargs) -> WaitResult:
        return wait_for_it(self.check_for_io_started, **wait_kwargs)


class BaseBtestMonitor(IOMonitor):
    BTEST_CMD = '/opt/nvmesh/perfTest/io_stress/btestEX'
    TRACE_CMD = 'sudo journalctl _TRANSPORT=kernel | grep -E "(TR_|RT_).* {volume} "'
    PANIC_PATTERNS = [
                        # This parsed # is not OUR VLBA, but offset/stampsize which is ALMOST always 4k...
                        r'^PANIC:(?P<DI>).*verify_error: *file *(?P<drive>[^\[]+?nvmesh/(?P<volume>[^\[]+))?\[(?P<btest_vlba>\d+)\] offset (?P<offset>0x[0123456789abcdef]+)',
                        "^PANIC:.*IO error on '(?P<drive>[^']*)': (?P<error>.*)",
                        r'^PANIC: \[.*\] do_timeout_check: IO \(worker \d+\) on \'\/dev\/nvmesh\/(?P<volume>\S+)\'(?P<TIMEOUT>) '
                        r'offset (?P<offset>\d+) block size (?P<bs>\d+) didn\'t complete after (?P<timeout_ms>\d+)\[usec]',
                        r'^PANIC:(?P<DI>).*verify_error: *file *(?P<binding>[^\[]+)?\[(?P<btest_vlba>\d+)\] offset (?P<offset>0x[0123456789abcdef]+)',
                        '^PANIC:.*'
                    ]
    IO_PATTERNS = [r'Subtotal \(diff\): (\d+) workers, (?P<seconds>\S+) seconds \S+, (?P<iops>\S+) iops,'
                   r' avg latency (?P<latency>\d+) usec, bandwidth (?P<bandwidth>\d+) KB\/s, errors (?P<errors>\d+),'
                   r'( verification errors (?P<verification_errors>\d+),)? ops (?P<ops>\d+)',
                   r'Total: (?P<seconds>\S+) seconds, (?P<iops>\S+) iops, avg latency (?P<latency>\d+) usec, bandwidth (?P<bandwidth>\d+) KB\/s,.*',
                   r'Total: (?P<seconds>\S+) seconds, (?P<max_latency>\d+) max_latency,.*']
    DATA_DUMP_PATT = [r'^writing block information to (?P<block_data>[^\n]+)',
                      r'^<==> FILE: (?P<block_data>[^\n]+)']
    DEFAULT_ARGS = "-c -t 0 -T 4 -w 8 -D -B 30000 R 50"
    BTEST_DUMP_FILE_TO = '/var/log/btest'

    def _create_btest_cmd(self, btest_args, duration):
        return '-y {} -t {} {}'.format(self.BTEST_DUMP_FILE_TO, duration, re.sub('(^| )-(t|-duration)( *|=)\d*', '',
                                           btest_args)) if duration is not None else btest_args
    def __init__(self, clients, volumes, btest_args, btest_path=BTEST_CMD, logdir=None, duration=None, debug_di=False, **monitor_kwargs):
        from xlro.core.entities import Manager

        _clients = clients if isinstance(clients, Sequence) and not isinstance(clients, str) else [clients]
        _volumes = volumes if isinstance(volumes, Sequence) and not isinstance(volumes, str) else [volumes]
        mgmt = getattr(_volumes[0], 'mgmt', None) or getattr(_clients[0], 'mgmt', None) or Manager.get_manager()

        for c in _clients:
            for v in _volumes:
                assert getattr(c, 'mgmt', mgmt) == getattr(v, 'mgmt', mgmt), "{} and {} are not pointing to same Manager".format(c, v)

        self.clients_to_vol_paths = defaultdict(list)
        self.clients_path_to_vol = defaultdict(dict)
        self.clients_to_vols = defaultdict(list)
        for c in _clients:
            for v in _volumes:
                self.clients_to_vol_paths[c].append(c.get_volume_dev_path(v))
                self.clients_path_to_vol[c][c.get_volume_dev_path(v)] = v
                self.clients_to_vols[c].append(v)


        super(BaseBtestMonitor, self).__init__(
            clients=_clients,
            volume_paths=[v_path for paths in list(self.clients_to_vol_paths.values()) for v_path in paths],
            io_tool_args=self._create_btest_cmd(btest_args, duration),
            io_tool_path=btest_path,
            logdir=logdir,
            duration=duration,
            mgmt=mgmt or Manager.get_manager())

        self.io_tool = 'btest'
        self.btest_args = self.io_tool_args
        self.btest_path = self.io_tool_path
        self.n_clients = len(self.clients)
        self.debug_di = debug_di
        self.attachment_to_debug_di_status = {}  # attachment:status
        self.create_monitors(**monitor_kwargs)

    def create_monitors(self, **monitor_kwargs):
        for i, client in enumerate(self.clients):
            for vol_path in self.clients_to_vol_paths[client]:
                vol_name = path.basename(vol_path)
                ph_data = [(self.PANIC_PATTERNS, {self.PANIC_TYPE: True}),
                           (self.IO_PATTERNS, {self.IO_TYPE: True, 'volume': vol_path, monitor.PatternHandler.LOG_LEVEL: 1}),
                           (self.DATA_DUMP_PATT,)]

                volume_object = self.clients_path_to_vol[client][vol_path]
                self.add_monitor(client.get_mon_cls(self.io_tool)(
                    name=f'{path.basename(self.io_tool_path)}:{client.name}:{vol_name}',
                    cmd=self.cmd_str(client, i, vol_path),
                    client=client,
                    host=client.get_hostname(),
                    logpath=path.join(self.logdir, self.log("BtestMonitor-" + self.job_name(client.name, vol_name))) if self.logdir else None,
                    debug_di=self.debug_di,
                    handlers=[monitor.PatternHandler(*p) for p in ph_data],
                    event_defaults={'client': client.name, 'volume': vol_name, 'volume_bs': volume_object.get_block_size(),
                                    'nvmesh_volume_name': volume_object.name},
                    mgmt=self.mgmt,
                    io_tool=self.io_tool,
                    **monitor_kwargs))

    def cmd_str(self, client, index, action_path):
        j_arg = f' -j {index},{self.n_clients} ' if self.n_clients > 1 else ''
        btest_cmd = '{} {} {} {}'.format(self.btest_path, j_arg, self.btest_args, action_path)
        cmd = "ulimit -c unlimited && mkdir -p {} && {}; rc=$?; echo finish_run; exit $rc".format(self.BTEST_DUMP_FILE_TO, btest_cmd)
        if not isinstance(client, K8sClient):
            cmd = f"sudo bash -c '{cmd}'"

        return cmd

    def io_events(self):
        return [event for event in self.events if self.IO_TYPE in event]


class MRSPBtestMonitor(BaseBtestMonitor):
    def __init__(self, client, nvme_devices, btest_args, btest_path=BaseBtestMonitor.BTEST_CMD, logdir=None, duration=None):
        super(MRSPBtestMonitor, self).__init__([client.host.name],
                                           [dev.name for dev in nvme_devices],
                                           btest_args,
                                           btest_path,
                                           logdir,
                                           duration)


class BtestTimeoutAction(Action):
    def __init__(self, mgmt, logdir, *args, **kwargs):
        self.mgmt = mgmt
        self.logdir = logdir
        self.transport_errors = []
        super(BtestTimeoutAction, self).__init__(*args, **kwargs)

    def doit(self, event):
        from xlro.core.util.disks_transport import process_online

        if not self.logdir:
            return

        # wait 2 seconds for flushing traces
        time.sleep(2)

        client = Client.instance(name=event['client'], mgmt=self.mgmt)
        volume = Volume.instance(name=event['volume'], mgmt=self.mgmt)
        vlba = old_div(int(event['offset']), int(event['volume_bs']))

        with open(self.logdir + '/timeout_{}_{}_{}'.format(client.name, volume.name, self.mgmt.host), 'w+') as o:
            self.logger.notice("drive connection traces: {}".format(o.name))
            process_online(client, volume, o)
        # just to make test work, later one can add here analyzing of process_online results
        self.transport_errors.append("DUMMY_DONE")


class BtestMonitor(BaseBtestMonitor):

    def __init__(self, clients, volumes, btest_args=BaseBtestMonitor.DEFAULT_ARGS,
                 btest_path=BaseBtestMonitor.BTEST_CMD, logdir=None, duration=None, debug_di=False, **monitor_kwargs):

        super(BtestMonitor, self).__init__(clients, volumes,
                                           btest_args,
                                           btest_path,
                                           logdir,
                                           duration, debug_di,
                                           **monitor_kwargs)
        self._traced = set()
        self.add_action(BtestTimeoutAction(mgmt=self.mgmt,
                                           logdir=logdir,
                                           condition=lambda e: 'TIMEOUT' in e,
                                           is_async=False))
        self.min_ops = monitor_kwargs.pop("min_ops", 0)

    def topo_trace(self, event):
        self.logger.debug('TOPO-Trace - logdir={}, event={}'.format(self.logdir, event))
        if not self.logdir:
            return
        try:
            tracefile = 'topo-trace-{volume}-{host}.txt'.format(**event)
        except AttributeError as e:
            self.logger.info('{} - bad event: {}'.format(e, event))
            return

        # only save once per host/volume combo
        with self.CACHE_LOCK:
            if tracefile in self._traced:
                return
            self._traced.add(tracefile)

        with open(path.join(self.logdir, tracefile), 'w') as fp:
            cmd = self.TRACE_CMD.format(**event)
            process = ssh.RemotePopen(ssh.Connection.get_connection(event['host']), cmd)
            while fp.write(process.stdout.read(4096)):
                pass
            process.wait()

    def on_event(self, event: Dict) -> None:
        if self.is_io_started:
            if int(event['ops']) < self.min_ops:
                self.logger.info(f"io is below accepted min ({self.min_ops}): {event['ops']}")
                event['PANIC'] = f"ops too small"

        if 'btest_vlba' in event:
            # The btest printed VLBA is actually the offset / "stampsize". For IO-bs < 4K, stampsize == IO-bs which is wrong
            event['vlba'] = int(event['offset'], 16) // event['volume_bs']
            del event['btest_vlba']

        if self.IO_TYPE in event and 'Total' in event['__message']:
            if 'max_latency' in event['__message']:
                self.io_performance['max_latency (usec)'] = event['max_latency']
                if infra_conf.root.general.print_btest_results:
                    # This event is always AFTER the event with the client/volume info, so it is already filled
                    self.logger.notice(f"client: {self.io_performance['client']}, dev: { self.io_performance['volume']}, vol: {self.io_performance['nvmesh_volume_name']}, IOPS: {(self.io_performance['IOPS'])}, avg latency: {(self.io_performance['avg latency (usec)'])} usec, bandwidth: {( self.io_performance['bandwidth (K/s)'])} KB/s, max latency: {(self.io_performance['max_latency (usec)'])} usec")
            else:
                self.io_performance['client'] = event['client']
                self.io_performance['volume'] = event['volume']
                self.io_performance['nvmesh_volume_name'] = event['nvmesh_volume_name']
                self.io_performance['IOPS'] = event['iops']
                self.io_performance['avg latency (usec)'] = event['latency']
                self.io_performance['bandwidth (K/s)'] = event['bandwidth']

        if not self.is_io_started and (self.PANIC_TYPE in event or (self.IO_TYPE in event and float(event['iops']))):
            client = event['client']
            volume = event['volume']
            client_io_set = self._io_started_dict[client]
            if volume not in client_io_set:
                self.logger.info("got first IO event: {}".format(event))
                client_io_set.add(volume)
            if set(self._io_started_dict.keys()) == set(c.name for c in self.clients) and \
                    all([set(self.clients_to_vol_paths[c]) == set(self._io_started_dict[c.name]) for c in self.clients]):
                self.logger.info("Setting is_io_started=True on {}".format(client))
                self.is_io_started = True
        super(BtestMonitor, self).on_event(event)

    @property
    def pending_io(self):
        ret = {}
        for c in self.clients:
            diff = set(self.clients_to_vol_paths[c]).difference(set(self._io_started_dict[c.name]))
            if diff:
                ret[c] = diff
        return ret


# TODO: Once stop() is tested more, we can drop this class.  Should simplify other stuff.
class FioPidMonitor(PidMonitor):
    def __init__(self, name, cmd, kill_timeout=None, **monitor_kwargs):
        self.name = name
        self.kill_timeout = kill_timeout or 60
        super(FioPidMonitor, self).__init__(cmd, **monitor_kwargs)

    def stop(self):
        self.stopping = True
        fio_vol_pattern = f'fio.*{self.event_defaults["volume"]}'
        fio_pids = Connection.execute_on_host(self.host, f'sudo pgrep -af "{fio_vol_pattern}"')[0]
        self.logger.debug(f'FIO PS BEFORE:\n{fio_pids}')
        result = super().stop()
        fio_pids = Connection.execute_on_host(self.host, f'sudo pgrep -af "{fio_vol_pattern}"')[0]
        self.logger.debug(f'FIO PS AFTER:\n{fio_pids}')
        return result

class FioMonitor(IOMonitor):
    PANIC_PATTERNS = [r'(?P<verify_type>\S+):(?P<DI>) verify failed at file (?P<filename>\S+) offset (?P<offset>\d+)',
                      r'verify:(?P<DI>) bad magic header \S+.* file \S+ offset (?P<offset>\d+)',
                      r"verify:(?P<DI>) bad header rand_seed \S+.* file \S+.* offset (?P<offset>\d+), length (?P<length>\d+)",
                      r'fio: \S+ is not a directory']
    FIO_CMD = 'fio'
    FIO_IO_PATTERN = r'.*\[r=(?P<reads_rate>.*),w=(?P<writes_rate>.*)\]\[r=(?P<reads_count>.*),w=(?P<writes_count>.*) IOPS\]\[eta.*\]'
    FIO_IO_W_ONLY_PATTERN = r'.*\[w=(?P<writes_rate>.*)\]\[w=(?P<writes_count>.*) IOPS\]\[eta.*\]'
    FIO_IO_R_ONLY_PATTERN = r'.*\[r=(?P<reads_rate>.*)\]\[r=(?P<reads_count>.*) IOPS\]\[eta.*\]'
    FIO_IO_PATTERNS = [FIO_IO_R_ONLY_PATTERN, FIO_IO_W_ONLY_PATTERN, FIO_IO_PATTERN]
    BS_PATTERN = r'--(bs|blocksize)=(?P<bs>(\d+[kKMm]{,1})*,*\d+[kKMm]{,1},*(\d+[kKMm]{,1})*)'
    TIME_RE = r'(^| )--(runtime|time_based)( *|=)\d*'
    DEFAULT_ARGS = "--group_reporting --rw=rw --numjobs=1 --iodepth=4 --bs=4k --ioengine=libaio --direct=1 --rwmixread=80 --size=2g"
    DEFAULT_BS = 4096

    def __init__(self, clients, volumes, fio_args, fio_path, mount_point=None, logdir=None, duration=0, **monitor_kwargs):
        from xlro.core.entities import Manager

        _clients = clients if isinstance(clients, Sequence) and not isinstance(clients, str) else [clients]
        _volumes = volumes if isinstance(volumes, Sequence) and not isinstance(volumes, str) else [volumes]
        # JW: We should never have been so accomodating...
        mgmt = getattr(_volumes[0], 'mgmt', None) or getattr(_clients[0], 'mgmt', None) or Manager.get_manager()
        _clients = [c if isinstance(c, Client) else Client.instance(name=c, mgmt=mgmt) for c in _clients]
        _volumes = [v if isinstance(v, Volume) else Volume.instance(name=v, mgmt=mgmt) for v in _volumes]
        self.volume_names = [getattr(volume, 'name', volume) for volume in _volumes]
        self.client_names = [getattr(client, 'name', client) for client in _clients]

        for c in _clients:
            for v in _volumes:
                assert c.mgmt == v.mgmt, "{} and {} are not pointing to same Manager".format(c, v)

        if duration is not None:
            fio_args = f"--time_based --runtime={duration or '1d'} --eta-newline=1 {re.sub(self.TIME_RE, '', fio_args)}"

        super(FioMonitor, self).__init__(clients=_clients, volume_paths=self.volume_names, io_tool_args=fio_args,
                                         io_tool_path=fio_path, logdir=logdir, duration=duration, mgmt=mgmt)

        self.fio_args = self.io_tool_args
        self.fio_path = self.io_tool_path
        self.io_tool = 'fio'

        res = re.search(FioMonitor.BS_PATTERN, fio_args)
        if res:
            bs_dict = res.groupdict()
            # TODO:need to support all types of bs patterns
            self.read_bs, self.write_bs, self.trim_bs = 3 * ([convert_size_to_bytes(bs_dict['bs'])] if bs_dict.get('bs') else [FioMonitor.DEFAULT_BS])
        else:
            self.read_bs, self.write_bs, self.trim_bs = 3 * [FioMonitor.DEFAULT_BS]
        self.mount_point = mount_point
        assert not (self.mount_point and len(_volumes) > 1), \
            'cannot spwan single FioMonitor to multiple volumes, please construct multiple FioMonitors'
        self.create_monitors(**monitor_kwargs)
        # check only for io_started if ramp time wasn't provided to avoid complexity
        self.is_io_started = "ramp_time" in fio_args

    def create_monitors(self, **monitor_kwargs):
        for client in self.clients:
            for vname in self.volume_paths:
                att = Attachment.instance(client=client, volume=Volume.instance(name=vname, mgmt=self.mgmt))
                job_name = self.job_name(client.name, vname)
                ph_data = [(self.PANIC_PATTERNS, {self.PANIC_TYPE: True}),
                           (self.FIO_IO_PATTERNS, {self.IO_TYPE: True, monitor.PatternHandler.LOG_LEVEL: 1})]

                self.add_monitor(client.get_mon_cls(self.io_tool)(
                    name=f'{path.basename(self.io_tool_path)}:{client.name}:{vname}',
                    cmd=self.cmd_str(client, job_name, self.mount_point or path.join(client.vol_dir_prefix, vname)),
                    client=client,
                    host=client.get_hostname(),
                    logpath=path.join(self.logdir, self.log('FioMonitor-' + job_name)) if self.logdir else None,
                    handlers=[monitor.PatternHandler(*p) for p in ph_data],
                    event_defaults={'client': client.name, 'volume': vname, 'volume_bs': att.volume.get_block_size(),
                                    'read_bs': self.read_bs, 'write_bs': self.write_bs, 'trim_bs': self.trim_bs},
                    io_tool=self.io_tool,
                    **monitor_kwargs))

    def cmd_str(self, client, job_name='fio_job', action_path='filename'):
        return f'{"" if isinstance(client, K8sClient) else "sudo"} {self.fio_path or client.host.cache_cmd_path(self.io_tool)} {self.fio_args} --{"directory" if self.mount_point else "filename"} {action_path} --name {job_name}'

    def on_event(self, event: Dict) -> None:
        if self.DI_TYPE in event:
            event['vlba'] = old_div(int(event['offset']), event['volume_bs']) if event.get('offset', '') else ''

        if not self.is_io_started and self.IO_TYPE in event and \
                (event.get('writes_count', '0') != '0' or event.get('reads_count', '0') != '0'):
            client = event['client']
            volume = event['volume']
            client_io_set = self._io_started_dict[client]
            if volume not in client_io_set:
                client_io_set.add(volume)
                self.logger.info("got first IO event: {}".format(event))

            if set(self._io_started_dict.keys()) == set(self.client_names) and \
                    all([set(self.volume_names) == set(self._io_started_dict[c]) for c in self.client_names]):
                self.is_io_started = True

        super(FioMonitor, self).on_event(event)

    @property
    def pending_io(self):
        ret: Dict[str, set] = {}
        if self.is_io_started:
            return ret
        for c in self.client_names:
            diff = set(self.volume_names).difference(set(self._io_started_dict[c]))
            if diff:
                ret[c] = diff
        return ret

class ElbenchoPidMonitor(FioPidMonitor):
    # Dict of phase: { server: {  nbytes: totalbytes, msecs: totalmsecs } }
    allstats = defaultdict(lambda: defaultdict(lambda: {'msecs': 0, 'nbytes': 0}))

    def on_event(self, event: Dict) -> None:
        try:
            stats = self.allstats[event['mixtype'] if event['phase'].startswith('RWMIX') else event['phase']][event['server']]
            # Switch event stats to be per-sample, not total, and calc latest rate
            event['nbytes'] = int(event['nbytes']) - stats['nbytes']
            stats['nbytes'] += event['nbytes']
            event['msecs'] = int(event['msecs']) - stats['msecs']
            stats['msecs'] += event['msecs']
            event['mbps'] = int((event['nbytes']/(1024*1024))/(event['msecs']/1000))
            super().on_event(event)
        except Exception as e:
            self.logger.info(f'Failed to handle event. {repr(e)}')

class ElbenchoMonitor(IOMonitor):
    ELBENCHO_CMD = 'elbencho'
    DEFAULT_ARGS = '--threads 8 --size 1M --block 4k --write --iteration 1 --infloop --direct'
    # These are the output control args needed for monitor to work. Not in user control
    MON_ARGS = '--livecsv /dev/fd/1 --livecsvex --nolive'
    # CSV Fields: Label,Phase,RuntimeMS,Rank,MixType,Done%,DoneBytes,MiB/s,IOPS,Entries,Entries/s,Lat Ent us,Lat IO us,Active,CPU,Service,
    # TODO: csv handler
    ELBENCHO_IO_PATTERNS = '[^,]*,(?P<phase>[^,]*),(?P<msecs>\d+),[^,]+,(?P<mixtype>[^,]*),[^,]+,(?P<nbytes>[^:,]*),([^,]*,){8}(?P<server>[^:,]+)'

    def __init__(self, clients, paths, elbencho_args=DEFAULT_ARGS, elbencho_path=ELBENCHO_CMD, logdir=None, duration=0, **monitor_kwargs):
        from xlro.core.entities import Manager

        elbencho_listeners = clients if isinstance(clients, Sequence) and not isinstance(clients, str) else [clients]
        self.paths = paths.split(',') if isinstance(paths, str) else paths
        mgmt = getattr(elbencho_listeners[0], 'mgmt', None) or Manager.get_manager()
        super().__init__(clients=elbencho_listeners, volume_paths=self.paths, io_tool_args=elbencho_args,
                         io_tool_path=elbencho_path, logdir=logdir, duration=duration, mgmt=mgmt)
        self.io_tool = 'elbencho'
        primary = elbencho_listeners[0].name
        self.client_names = [c.name for c in self.clients]
        hosts_str = ','.join(self.client_names)
        if duration:
            self.MON_ARGS += f' --timelimit {duration}'

        # Is this supposed to be create_monitors()?
        for p in self.paths:
            job_name = p.replace('/', '-')
            self.add_monitor(ElbenchoPidMonitor(
                name=f'{self.io_tool_path}:{p}',
                cmd=f'{self.io_tool_path} --hosts {hosts_str} {self.MON_ARGS} {self.io_tool_args} {p}',
                host=primary,
                logpath=os.path.join(self.logdir, self.log('ElbenchoMonitor-' + job_name)) if self.logdir else None,
                handlers=[monitor.PatternHandler(self.ELBENCHO_IO_PATTERNS,
                    {'client': primary, self.IO_TYPE: True, 'path': p, monitor.PatternHandler.LOG_LEVEL: 1})],
                io_tool=self.io_tool,
                **monitor_kwargs))

    def on_event(self, event: Dict) -> None:
        if not self.is_io_started and self.IO_TYPE in event and event['nbytes']:
            client = event['server']
            path = event['path']
            client_io_set = self._io_started_dict[client]
            if path not in client_io_set:
                client_io_set.add(path)
                self.logger.info(f'got first IO event for {client}:{path}')

            path_set = set(self.volume_paths)
            if set(self._io_started_dict.keys()) == set(self.client_names) and \
                    all([path_set == set(self._io_started_dict[c]) for c in self.client_names]):
                self.logger.info(f'IO STARTED!')
                self.is_io_started = True

        super().on_event(event)

    def start(self):
        for c in self.clients:
            out, err, code = Host.instance(name=c.name).execute(f'{self.io_tool_path} --service')
            assert not code or 'Address already in use' or 'Address in use' in err, f'Failed to start {self.io_tool} on {c.name} - {err}'
        super().start()

    def stop(self):
        super().stop()
        out, err, code = Host.instance(name=self.clients[0].name).execute(f'{self.io_tool_path} --hosts {",".join(c.name for c in self.clients)} --quit')
        assert not code, f'Failed to stop {self.io_tool} - out: {out} ; err: {err}'

    @property
    def pending_io(self):
        ret: Dict[str, set] = {}
        if self.is_io_started:
            return ret
        for c in self.client_names:
            diff = set(self.paths).difference(set(self._io_started_dict[c]))
            if diff:
                ret[c] = diff
        return ret


class VDBEMCHMonitor(monitor.CmdMonitor):
    TRACE_CMD = "C:\\vdbench50405\\vdbench.bat -f C:\\vdbench50405\\configs\\vdbench_monitor_conf.txt"
    TRACE_PATTERN = r'I/O error'

    def __init__(self, windows_client, nvme_devices, **kwargs):
        self.windows_client = windows_client
        self.nvme_devices = nvme_devices
        super(VDBEMCHMonitor, self).__init__(self.TRACE_CMD, windows_client.host.name, get_pty=False, **kwargs)
        self.add_msg_handler(monitor.PatternHandler(self.TRACE_PATTERN, {'host': windows_client.host.name}))
        self._create_vdbench_config_file()

    def _create_vdbench_config_file(self):
        config_list = []
        for i, dev in enumerate(self.nvme_devices):
            root_anchor = '{0}:\dir1'.format(chr(int(dev.id) + ord('C')))
            config_list.append('fsd=fsd{0},anchor={1},depth=2,width=2,files=2,size=128k'.format(i, root_anchor))

        config_list.append('fwd=fwd1,fsd=fsd*,operation=write,xfersize=4k,fileio=sequential,fileselect=random,threads=2')
        config_list.append('rd=rd1,fwd=fwd1,fwdrate=max,format=yes,elapsed=25,interval=1')

        sftp = self.windows_client.host.connection.client.open_sftp()
        file = sftp.file('C:\\vdbench50405\\configs\\vdbench_monitor_conf.txt', 'w+')
        file.write('\n'.join(config_list))
        file.close()
