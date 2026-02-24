#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
from builtins import map
from builtins import zip
import _thread
from typing import Any,Dict,List,Tuple,Union # pylint: disable=unused-import
import collections
import copy
from os import path
import time
import logging
import re
from shlex import quote
from xlro.core import infra_conf
from xlro.core.util.monitor import MsgHandler, CmdMonitor, MultiMonitor, Monitor
from xlro.core.entities import Attachment, Client, Volume, SourceTypes, Host, Manager
from xlro.core.util.general_utils import wait_for_it


class WatchMsgHandler(MsgHandler):
    """
    Handler useful for WatchMonitor. Records an event for each series of non-empty lines.
    Lines are of the format: '>'|'<' <data>
    '<' indicates old data, '>' indicates new data
    Without sep, an event is:
    { _time: <time-stamp>, 'data': (old, new) }
    If a sep is provided, the data is assumed a <sep> separated list, with the first column a key.
    Then, an event is:
    { _time: <time-stamp>, Key1: (old, new), Key2: (old, new) ...  }
    old and new will be lists of field values (split on sep and stripped)
    old or new can be an empty list
    """
    def __init__(self, sep=':'):
        self.sep = sep
        self.data: Dict[str, Dict[str, List[str]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        self.initialized = False        # The first message can be empty, indicating empty initial content.
        self.watcher_id = ""
        self.watcher_start = True       # Not same as initialize event.  Watcher could be restarted
        self.logger = logging.getLogger('.'.join([self.__module__, self.__class__.__name__]))

    def handle_msg(self, msg):
        bare_msg = msg.strip()
        # self.logger.info('MSG: "{}"'.format(msg))
        if not bare_msg:
            # An empty line is a message separator, so process what we have, if anything
            if not self.data and self.initialized:
                return None

            self.initialized = True
            # Emit event
            event: Dict[str, Any] = {}
            event['watcher_id'] = self.watcher_id
            event['watcher_start'] = self.watcher_start
            event['data'] = { key: (vdict['old'], vdict['new']) for key, vdict in self.data.items() }
            if not event['data'] and not self.sep:
                # There can be an "empty" message during initialization - fake the data
                event['data']['data'] = ([],[])
            # event['data'] = self.data.copy()
            self.data.clear()
            return event

        flag, _, bare_msg = bare_msg.partition(' ')
        if flag == '#':
            # Watcher ID
            self.watcher_start = not self.watcher_id == bare_msg
            self.watcher_id = bare_msg
            return None

        if self.sep:
            fields = [f.strip() for f in bare_msg.split(self.sep)]
            key = fields.pop(0)
            self.data[key]['new' if flag == '>' else 'old'] = fields
        else:
            self.data['data']['new' if flag == '>' else 'old'].append(bare_msg)
        return None


class PyWatchMonitor(CmdMonitor):
    """
    Run watch.py on a remote to monitor command output and/or file content for changes.
    See watch.py arg parsing for help with options.
    """
    # maybe this should be in CmdMonitor if input is sent...
    OPTIONS = ['file', 'cmd', 'pattern', 'format', 'json', 'jsonobjects', 'delay', 'debug', 'once']
    TERMINATOR = '__END_OF_INPUT__'
    _watch_script = None

    # Unlike other monitors, the first event initializes the results' status. We don't count it as an event
    _is_initialized = False

    def __init__(self, host, sep=None, **kwargs):
        self.logger = logging.getLogger('.'.join([self.__module__, self.__class__.__name__]))
        self.sep = sep
        self.results: Union[Dict, List] = {} if sep else []
        self.base_results = self.results
        self.results_changed = False
        if not self.__class__._watch_script:
            with open(path.join(path.dirname(__file__), 'watch.py'), 'r') as fp:
                self.__class__._watch_script = fp.read() + '\n' + self.TERMINATOR + '\n\n'
        watch_args = kwargs.pop('watch_args', '')
        for opt in self.OPTIONS:
            value = kwargs.pop(opt, None)
            if value is True:
                watch_args += ' --' + opt
            elif value:
                watch_args += " --{}='{}'".format(opt, value)
        cmd = 'sudo /usr/bin/python3 -u - {} <<\{}\n{}\n'.format(watch_args, self.TERMINATOR, self._watch_script)
        super(PyWatchMonitor, self).__init__(cmd, host, **kwargs)
        self.add_msg_handler(WatchMsgHandler(sep))

    def on_message(self, msg):
        # keep initialization messages at default level
        prev_level = self.event_defaults.get(MsgHandler.LOG_LEVEL, None)
        if prev_level and not self._is_initialized:
            del self.event_defaults[MsgHandler.LOG_LEVEL]
        event = super(PyWatchMonitor, self).on_message(msg)
        if prev_level:
            self.event_defaults[MsgHandler.LOG_LEVEL] = prev_level
        return event

    def start(self, wait_for_init=10):
        super(PyWatchMonitor, self).start()
        if wait_for_init > 0 and not wait_for_it(self.is_initialized, timeout=wait_for_init):
            self.logger.warning('Initialize event not received after {} seconds.'.format(wait_for_init))
            self.logger.info('Execution tail:\n' + "tail: \\n".join(list(self.tail)))
            raise Exception('Initialize event not received after {} seconds.'.format(wait_for_init))

    def on_event(self, event):
        # Maintain a results map
        if 'data' not in event:
            # It isn't one of ours, so just pass it on.
            return super(PyWatchMonitor, self).on_event(event)

        prev_results = copy.deepcopy(self.results)
        if self.sep:
            # keyed values...
            assert isinstance(self.results, dict), 'Results type for "sep" watcher must be dict.'
            for key, update in event['data'].items():
                if update[1]:
                    self.results[key] = update[1]
                else:
                    self.results.pop(key, None)
        else:
            # just a list...
            # (Since we're assuming uniqueness now, probably should just be a set, but don't want to break anything
            assert isinstance(self.results, list), 'Results type for "non-sep" watcher must be list.'

            if self.is_initialized and event.get('watcher_start', False):
                # We were already initialized, but the watcher restarted, so we recalculate del/add lists
                self.results = event['data']['data'][1]
                new_set = set(self.results)
                old_set = set(prev_results)
                event['data']['data'] = (list(old_set - new_set), list(new_set - old_set))
            else:
                for value in event['data']['data'][0]:
                    self.results.remove(value)
                for value in event['data']['data'][1]:
                    if value not in self.results:
                        self.results.append(value)
            self.results.sort()

        if not self._is_initialized or prev_results == self.results:
            # Swallow initializing event or no-op data event
            self.logger.debug('Swallowing {} event: {}'.format('No-Op' if self._is_initialized else 'Init', event))
            self._is_initialized = True
            return None
        else:
            self.results_changed = True

        return super(PyWatchMonitor, self).on_event(event)

    def is_initialized(self):
        return self._is_initialized

    def update_base_results(self):
        self.base_results = copy.deepcopy(self.results)
        self.results_changed = False

    def assert_changes(self):
        assert not self.results_changed, "{}: {} != {}".format(self.__class__.__name__, self.base_results, self.results)


class AttachmentWatchMonitor(PyWatchMonitor):
    def __init__(self, host, **kwargs):
        self.logger = logging.getLogger('.'.join([self.__module__, self.__class__.__name__]))
        self.client = Client.instance(name=host)
        self.client.get_property('attachments', SourceTypes.PROC)
        self.logger.debug('Watching Attachments for: {}'.format(self.client))
        if self.client.isUmClient:
            cmd="sudo nvmeshum_spdk_rpc nvmesh_list_class_objects --class volume | jq -r '.\"class objects\"[] | .name' | xargs -rl sudo /opt/nvmesh/nvmeshum/bin/nvmeshum_rpc2proc.py --method volume_status --name | jq -r '[.name,\"visible\",.state,.status,(.io_enabled == 1 | tostring)] | join(\":\")' "
            # This is a VERY heavy command.  For now, I'm setting delay to 3s
            super(AttachmentWatchMonitor, self).__init__(host, sep=':', delay=3, cmd=quote(cmd)[1:-1], **kwargs)
        else:
            super(AttachmentWatchMonitor, self).__init__(host, sep=':',
                            file='{}/volumes/*/status'.format(self.client.proc),
                            format='{vname}:{is_hidden}:{status}:{status_str}:{is_io_enabled}',
                            delay=0.3, pattern=Attachment.PATTERN, **kwargs)

    def on_event(self, event):
        # Maintain Client.attachments...
        super(AttachmentWatchMonitor, self).on_event(event)
        self.logger.debug('Event-data: {}'.format(event['data']))
        attachments = self.client.attachments
        current = self.results
        assert isinstance(current, dict), '{} expected results to be dict'.format(self.__class__.__name__)
        # Check for deletions
        for vname in attachments:
            if vname not in current:
                self.logger.debug('Removing: {}'.format(attachments[vname]))
                attachments.pop(vname).remove()
        # Check for add/update
        for vname, values in current.items():
            if not vname in attachments:
                attachments[vname] = Attachment.instance(client=self.client, volume=Volume.instance(name=vname))
            props = dict(list(zip(['is_hidden','status','status_str', 'is_io_enabled'], values)))
            self.logger.debug('Add/Update: {}: {}'.format(attachments[vname], props))
            # TODO - add source to the set_properties
            attachments[vname].set_properties(props)


class ServiceWatchMonitor(PyWatchMonitor):
    def __init__(self, host, **kwargs):
        super(ServiceWatchMonitor, self).__init__(host, sep=' ',
                                                  cmd='systemctl --plain --all --no-legend list-units nvmesh\*',
                                                  pattern='\s*(\w+).service\s+(\w+)\s+(\w+)\s+(\w+)',
                                                  format='{1} {2} {3} {4}', delay=0.5, resilient=True, expected=0, **kwargs)


class ModuleWarningWatcher(PyWatchMonitor):
    def __init__(self, host, module_name, **kwargs):
        self.module_name = module_name
        self.warnings = 0
        super(ModuleWarningWatcher, self).__init__(host, file='/sys/module/{}/parameters/num_warnings'.format(module_name),
                                                   delay=0.3, pattern=r'(\d+)', resilient=True, expected=0,
                                                   event_defaults={'module': module_name, 'host': host}, **kwargs)

    def on_event(self, event):
        if event and 'data' in event and 'data' in event['data']:
            old, new = event['data']['data']
            if not new or not old:
                # module is turning off/on
                event[MsgHandler.NON_ERROR] = ""

        super(ModuleWarningWatcher, self).on_event(event)
        if len(self.results) == 0:
            # in case sys not exists(old versions or service is down)
            self.warnings = 0
            return
        self.warnings = int(self.results[0])
        if self.warnings:
            self.logger.warning(f'Found {self.warnings} warnings for {self.module_name} on {self.host}'
                    f' ({"" if self.results_changed else "un"}changed)')


class DiskCountersWatcher(PyWatchMonitor):
    NO_WARN_CONF_PATH = 'monitors.counters-watcher.no_warn'
    no_warn = None
    def __init__(self, host, **kwargs):
        # procd is PURELY for testing
        procd = '/proc' if host != 'localhost' else path.abspath('./sim-proc')

        super(DiskCountersWatcher, self).__init__(host, sep=' ',
                    cmd='grep -s "^n_.*[1-9][0-9]*$" {}/*/disks/*/counters | sort'.format(procd),
                    pattern = r'{}/(?P<client>[^/]*)/disks/(?P<disk>[^/]*)/counters:(?P<cname>\S+) *: *(?P<cvalue>\d+)'.format(procd),
                    format='{client}:{disk}:{cname} {cvalue}', delay=0.5,
                    event_defaults = {
                        MsgHandler.EVENT_FORMAT: 'Disk-counter increase. {host} {data}',
                        MsgHandler.LOG_LEVEL: logging.DEBUG,
                        # Temporary.  We should figure out a more generic way to handle, but not today.
                        MsgHandler.NON_ERROR: "true",
                    }, **kwargs)

        if self.no_warn is None:
            try:
                self.no_warn = infra_conf.get_path(self.NO_WARN_CONF_PATH)
                assert isinstance(self.no_warn, list), f'Invalid configuration.  {self.NO_WARN_CONF_PATH} expected a list'
                self.logger.info(f'Disk-Watchers no_warn: {self.no_warn}')
            except Exception as e:
                self.no_warn = []
                self.logger.debug(f'No valid config for {self.NO_WARN_CONF_PATH}.  {repr(e)}')

    def on_event(self, event):
        try:
            # Data (when monitor has a sep) is a dict of keys and values, where values are list of fields
            # Missing, or '0' new values are not errors, but add NON_ERROR only if ALL changes are non-error
            # Also, if keys are configured as no-warn, that's also a non-error
            for k, (old,new) in event['data'].items():
                if (not self.no_warn or k.rpartition(':')[2] not in self.no_warn) and new and int(new[0]) > 0:
                    break
            else:
                event[MsgHandler.NON_ERROR] = ""
            if event[MsgHandler.NON_ERROR]:
                self.logger.warning(self.format_event(event))
        except Exception as e:
            self.logger.info('Invalid event: {} - {}'.format(repr(e), event))
        super(DiskCountersWatcher, self).on_event(event)

class OSDiskUsageWatcher(PyWatchMonitor):
    def __init__(self, host, disk_use_threshold=90, **kwargs):
        super(OSDiskUsageWatcher, self).__init__(
            host,
            cmd='df -h --output=pcent,avail /',
            pattern='(?P<pcent>\d{1,3}%)\s*(?P<avail>\d{1,5}\w|\d{1,5}\.\d{1,3}\w|0)',
            format='Used%: {pcent}, Available space: {avail}',
            delay=60,
            event_defaults={
                MsgHandler.EVENT_FORMAT: "Root partition disk usage on {host}: {data[data][1][0]}",
                MsgHandler.LOG_LEVEL: logging.DEBUG,
                MsgHandler.NON_ERROR: "true",
            }, **kwargs)

        self.warning_issued = False
        self.disk_use_threshold = disk_use_threshold
        self.logger.debug(f'Initializing OSDiskUsageWatcher for {host} with threshold: {self.disk_use_threshold}%')

    def on_event(self, event):
        try:
            for k, (old, new) in event['data'].items():
                if len(new) > 1:  # if got junk from stdout, only keep last print (which is 'df' command output)
                    new = new[-1:]
                old_pcent, old_avail = [int(match) for match in
                                        re.findall(r'\d{1,5}\.\d{1,3}|\d{1,5}|0', old[0])] if old else [0, 0]
                new_pcent, new_avail = [int(match) for match in
                                        re.findall(r'\d{1,5}\.\d{1,3}|\d{1,5}|0', new[0])]
                if new_pcent >= self.disk_use_threshold and not self.warning_issued:
                    self.logger.warning(f'Root partition disk usage on {event["host"]} is '
                                        f'above {str(self.disk_use_threshold)}%! Unexpected issues may occur!')
                    self.warning_issued = True
                elif new_pcent >= self.disk_use_threshold and self.warning_issued:
                    self.logger.debug(f'Root partition disk usage on {event["host"]} is still high '
                                      f'but I already issued warning')
                elif old and old_pcent >= self.disk_use_threshold > new_pcent:
                    self.logger.debug(f'Root partition disk usage on {event["host"]} is back '
                                      f'below {str(self.disk_use_threshold)}%, resetting warning status')
                    self.warning_issued = False
        except Exception as e:
            self.logger.info('Invalid event: {} - {}'.format(repr(e), event))
        super(OSDiskUsageWatcher, self).on_event(event)

class NodeJSMemoryWatcher(PyWatchMonitor):
    def __init__(self, host, **kwargs):
        super(NodeJSMemoryWatcher, self).__init__(
            host,
            # this is a bloody mess but awk runs like this, so please don't touch
            cmd="pidof node | xargs sudo pmap -qx 2>&1 | grep -E \"rw---.*anon|argument\s*missing\" | awk \"{ if (\$2 == \\\"argument\\\") { print 0 } else { sum += \$2 } } END { if (sum) {print sum} }\"",
            delay=60,
            event_defaults={
                MsgHandler.EVENT_FORMAT: 'Total NodeJS allocated heap on {host}: {data[data][1][0]} KB',
                MsgHandler.LOG_LEVEL: logging.DEBUG,
                MsgHandler.NON_ERROR: 'true',
            }, **kwargs)

        self.node_allocated_memory = self.get_node_allocated_memory(host)
        self.logger.info(f'Configured NodeJS memory allocation limit on {host}: {self.node_allocated_memory} MB')

    @staticmethod
    def get_node_allocated_memory(host):
        out, err, code = Host.instance(name=host).execute(
            cmd="perl -nle 'print $+{value} if /^(?!\/\/).*config\.nodeAllocatedMemory\s*=\s*(?<value>\d*);/' "
                "/etc/nvmesh/management.js.conf")
        return int(out.strip('\n')) if out else 4096

    def on_event(self, event):
        try:
            for k, (old, new) in event['data'].items():
                if new == ['0']:
                    self.logger.info(f'NodeJS process not running on {event["host"]}')
                elif (int(new[0]) // 10**3) >= self.node_allocated_memory:
                    self.logger.warning(f'NodeJS memory usage on {event["host"]} ({int(new[0]) // 10**3} MB) '
                                        f'is over the allocated {self.node_allocated_memory} MB!')
        except Exception as e:
            self.logger.info('Invalid event: {} - {}'.format(repr(e), event))
        super(NodeJSMemoryWatcher, self).on_event(event)

class MultiWatcher(MultiMonitor):
    def __init__(self, results_keys=('host', ), **kwargs):
        super(MultiWatcher, self).__init__(**kwargs)
        self.results_keys = results_keys
        self.base_results = {}

    def watchers(self):
        # type () -> List[PyWatchMonitor]
        return [mon for mon in self.all_monitors() if isinstance(mon, PyWatchMonitor)]

    @property
    def results(self) -> Dict[Tuple, Any]:
        return {tuple(mon.event_defaults[k] for k in self.results_keys): mon.results for mon in self.watchers()}

    def is_initialized(self):
        return all([mon.is_initialized() for mon in self.watchers()])

    def update_base_results(self):
        list(map(lambda w: w.update_base_results(), self.watchers()))

    def assert_changes(self):
        list(map(lambda w: w.assert_changes(), self.watchers()))


class LsWatcher(PyWatchMonitor):
    def __init__(self, host, patterns, **kwargs):
        cmd = "ls -1 -d {} 2> /dev/null".format(patterns)
        super(LsWatcher, self).__init__(host, cmd=cmd, delay=0.5, pattern=r'^([\/S].*)', format='{1}',
                                        resilient=True, expected=[0], **kwargs)

    def start(self, wait_for_init=5):
        super().start()
        if wait_for_init > 0 and not wait_for_it(self.is_initialized, timeout=wait_for_init):
            self.logger.debug('Initialize event not received after {} seconds.'.format(wait_for_init))


class CoreFileWatcher(LsWatcher):
    def __init__(self, host, save_cores_md_dir="", **kwargs):
        super(CoreFileWatcher, self).__init__(host, " ".join(Host.CORE_PATTERNS), **kwargs)
        self.save_cores_md_dir = save_cores_md_dir
        self.artifacts = set()

    def on_event(self, event: Dict) -> None:
        from xlro.core.entities import Host

        cores_before = copy.copy(self.results)
        init_event = not self.is_initialized()
        super(CoreFileWatcher, self).on_event(event)

        new_cores = set(self.results).difference(cores_before)
        if new_cores and not init_event:
            self.logger.warning("found cores on {}: {}".format(self.host, new_cores))

            if self.save_cores_md_dir:
                for core in new_cores:
                    if core.startswith('/var/crash'):
                        host = Host.instance(name=self.host)
                        if 'ubuntu' in host.platform.lower() and 'dump' in core:
                            path_to_core = core.replace('dump', 'dmesg')
                        else:
                            path_to_core = core + '-dmesg.txt'
                        dpath = path.join(self.save_cores_md_dir, path.basename(path_to_core) + '.' + self.host)
                        try:
                            _, err, code = host.connection.execute(f'sudo chmod a+r {path_to_core}')
                            assert not code, f"Unable to grant read permissions for copying {path_to_core} - {err}"
                            host.connection.get_file(path_to_core, dpath)
                        except Exception as e:
                            self.logger.warning(f"failed to fetch {dpath} from {self.host} - {repr(e)}")
                        self.artifacts.add(dpath)
        else:
            # Cores could be deleted.  Not an error
            event[MsgHandler.NON_ERROR] = 'No new cores'


class LustreResourceWatcher(PyWatchMonitor):
    '''
    Watch the results of "pcs status resources" and maintain a map of server: [lustre-volumes]
    No multi-watcher because all nodes reported by 1
    '''
    def __init__(self, host: str = None, **kwargs):
        cmd = '''
sudo pcs status resources | awk '
/Started:/ 		{ split(gensub(/.*\[ (.*) ]/, "\\1", 1), nodes); for (n in nodes) fs[nodes[n]] = "" }
/:[\t ]*Started /	{ fs[$NF] = fs[$NF] " " $2 }
END			{ for (n in fs) print(n fs[n]) }
'
        '''
        cmd = quote(cmd)[1:-1]
        if not host:
            host = Manager.get_manager().host
        super().__init__(host, sep=' ', cmd=cmd, **kwargs)

class LustreNodeWatcher(PyWatchMonitor):
    '''
    Watch the results of "pcs status nodes" and maintain a map of status: server-list
    No multi-watcher because all nodes reported by 1
    '''
    def __init__(self, host: str = None, **kwargs):
        cmd = '''
        sudo pcs status nodes | awk '
/.*Remote/  { exit; }
/^Pace/     { next; }
            { split($0, fields, ":"); state = gensub(/ +/, "-", "g", fields[1]); sub(/^-*/, "", state); print(state fields[2]); }
'
        '''
        cmd = quote(cmd.strip())[1:-1]
        # cmd = quote(cmd)[1:-1]
        if not host:
            host = Manager.get_manager().host
        super().__init__(host, sep=' ', cmd=cmd, **kwargs)


class CoreFileMultiWatcher(MultiWatcher):
    def __init__(self, hosts, *args, **kwargs):
        super(CoreFileMultiWatcher, self).__init__(*args, **kwargs)
        list(map(self.add_monitor, [CoreFileWatcher(host) for host in hosts]))


class MultiModuleWarningWatcher(MultiWatcher):
    def __init__(self, hosts, modules, *args, **kwargs):
        kwargs['results_keys'] = ('host', 'module')
        super(MultiModuleWarningWatcher, self).__init__(*args, **kwargs)
        list(map(self.add_monitor, [ModuleWarningWatcher(host, module) for host in hosts for module in modules]))


class HostsMultiWatcher(MultiWatcher):
    def __init__(self, watch_cls, hosts, *args, **kwargs):
        super(HostsMultiWatcher, self).__init__(*args, **kwargs)
        list(map(self.add_monitor, [watch_cls(host) for host in hosts]))


if __name__ == '__main__':
    import sys, pprint
    from xlro.core.util.cli_util import CLIArgumentParser
    from xlro.core.util.caser import CaserWatcherMonitor
    pp = pprint.PrettyPrinter(indent=4)
    logger = logging.getLogger(sys.argv[0])

    watch_classes = {
            'attachments': AttachmentWatchMonitor,
            'services': ServiceWatchMonitor,
            'counters': DiskCountersWatcher,
            'osdiskusage': OSDiskUsageWatcher,
            'nodejsmemory': NodeJSMemoryWatcher,
            'watch': PyWatchMonitor,
            'caser': CaserWatcherMonitor,
            'cores': CoreFileWatcher,
            'lustre-resource': LustreResourceWatcher,
            'lustre-node': LustreNodeWatcher,
        }

    parser = CLIArgumentParser()
    parser.add_argument('-d', '--duration', type=int, default=60, help='Duration to wait for events')
    parser.add_argument('hosts')
    parser.add_argument('watchtype', choices=watch_classes.keys())
    parser.add_argument('watchargs', nargs='*')
    args = parser.parse_args()

    hosts = args.hosts.split(',')
    cmd = sys.argv[2]
    watch_args = ' '.join(["'{}'".format(arg) for arg in args.watchargs])

    class PrintWatcher(MultiWatcher):
        def on_event(self, event):
            super(PrintWatcher, self).on_event(event)
            print(_thread.get_ident(), '** [{host}] Updates:'.format(**event))
            for key, value in event['data'].items():
                print(_thread.get_ident(), key, value[1])
            print(_thread.get_ident(), 'CURRENT:\n', pp.pformat(multi_mon.results))
            print(_thread.get_ident(), 'TIME:', time.asctime())

    multi_mon = PrintWatcher()

    for host in hosts:
        mon = watch_classes[args.watchtype](host, watch_args=watch_args, logpath='./{}.log'.format(host))
        multi_mon.add_monitor(mon)

    print(_thread.get_ident(), 'STARTING:', time.asctime())
    multi_mon.start()
    print(_thread.get_ident(), 'STARTED:', time.asctime())
    print(_thread.get_ident(), '** INITIAL-STATE:\n', multi_mon.results)
    print(_thread.get_ident(), '** INITIAL-STATE:\n', pp.pformat(multi_mon.results))
    print(_thread.get_ident(), 'TIME:', time.asctime())

    try:
        wait_for_it(lambda: not None in multi_mon.poll_all(), timeout=args.duration, poll=5)
    except KeyboardInterrupt:
        print('INTERRUPTED')
    except Exception as e:
        print('EXCEPTION:', type(e), e)

    print('MULTI-EVENTS:', len(multi_mon.events))
    for event in multi_mon.events:
        try:
            print(pp.pformat(event))
        except:
            print('NON-JSONable:', event)

    for m in multi_mon.all_monitors(): # type: Monitor
        print('MON:', m, 'EVENTS:', len(m.events))
        for event in m.events:
            try:
                print(pp.pformat(event))
            except:
                print('NON-JSONable:', event)
    print('RESULTS:', pp.pformat(multi_mon.results))
    multi_mon.stop()
