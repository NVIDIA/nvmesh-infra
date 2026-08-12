# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
import re
import logging
import datetime
import json
import threading
from functools import partial

from typing import Any,Callable,Dict,Iterable,List,Optional,Tuple,Type,TYPE_CHECKING,Union

from xlro.core.util.thread_manager import ThreadPoolManager

if TYPE_CHECKING:
    from xlro.core.entities import Host
from collections import namedtuple
from xlro.core.util.monitor import CmdMonitor, PatternHandler, MsgHandler

logger = logging.getLogger(__name__)
TraceType = namedtuple('TraceType', ['trace_name', 'rgx_pattern'])

# rgx pattern is not needed anymore but saving it for legacy
TRACE_METHOD2TYPE = {'nvmeibc_raid1_calc_io_perm':
                             TraceType("trace_1_topology_raid1_is_ioable",
                                       re.compile(r'(?P<name>\S+) <(?P<topo_debug_id>\d+)>: seg=\((?P<chunk_idx>\d+),'
                                                  r'(?P<praid_idx>\d+),(?P<segment_idx>\d+)\) '
                                                  r'disk=(?P<disk_name>\S+) acm=(?P<acm>.+) act=(?P<active>\d+) '
                                                  r'p=(?P<disk_state>\d+), lid=(?P<lid>\S+) ver=(?P<version>\d+)')),
                         '__set_active_topology':
                             TraceType("trace_topology_set_active_topology",
                                       re.compile(r'(?P<name>\S+) <(?P<topo_debug_id>\d+)>: old <(\d+)>')),
                         '__set_error_state':
                             TraceType("trace_topology_set_error_state",
                                       re.compile(r'(?P<name>\S+) <(?P<topo_debug_id>\d+)>: io_perm=(?P<io_perm>\S+)')),
                         '__block_toma_msg_handler':
                         TraceType("trace_4_topology_block_toma_msg_handler",
                                   re.compile(r'(?P<msg_type>\S+)\((?P<reason>\S+)\) received for volume (?P<volume>\S+),'
                                              r' seg=\((?P<chunk>\d+),(?P<praid>\d+),(?P<segment>\d+)\),'
                                              r' cfg=(?P<config_version>\d+), conv=(?P<conversation_id>\S+)')),
                         '__send_combined_msg':
                         TraceType("trace_b_cp_send_toma_msg_send_combined_msg",
                                   re.compile(r'.* Sending (?P<msg_type>\S+)\((?P<reason>\S+)\), (?P<volume>\S+)'
                                              r' \((?P<chunk>\d+),(?P<praid>\d+),(?P<segment>\d+)\)'
                                              r' uuid=(?P<segment_uuid>\S+)-(?P<disk_host>\S+),'
                                              r' r1_ver=(?P<praid_version>\d+) cookie=\d+'))
                     }


def c_prv(x: str) -> str:
    match = re.match(r"c_prv=0x(\S+)", x)
    assert match, 'Cannot parse c_prv from: {}'.format(x)
    return match.group(1)  # pager mistake I guess


def topo_debug_id(x):
    if isinstance(x, int):
        return x
    match = re.search(r'(\d+)', x)
    assert match, 'Cannot parse topo_debug_id from: {}'.format(x)
    return int(match.group(1))


class PagerToken(object):
    int16 = partial(int, base=16)
    # a mapping between pager token name to -> infra token name, conversion function/constructor
    P_TO_I: Dict[str, Tuple[str, Union[Type, Callable]]] = {"severity": ("severity", int),
              "K_PID": ("kernel_pid", str),
              "K_TASK_NAME": ("kernel_task_name", str),
              "nanoseconds": ("time_stamp", int),
              "PROTOCOL_CLIENT_MSG_STR": ("msg_type", str),
              "PROTOCOL_CLIENT_MSG_REASON_STR": ("reason", str),
              "DEV_NAME": ("volume", str), # maybe make it Volume entity one day?
              "CHUNK_IDX": ("chunk", int),
              "PRAID_IDX": ("praid", int),
              "SI": ("segment", int),
              "CFG": ("config_version", int),
              "CLNT_TOMA_PR_CONVER_IND": ("conversation_id", int16),
              "DEBUG_UNIQUE_INDEX": ("topo_debug_id", int),
              "TOPO_DBG_ID": ("topo_debug_id", topo_debug_id),
              "C_PRV": ("version", c_prv), # pager mistake I guess
              "RV": ("disk_state", int),
              "VOL_ID": ("short_id", int)
              }

    def __init__(self, pager_token_name, value):
        self.known_token = False
        try:
            # try to convert
            self.name = self.P_TO_I[pager_token_name][0]
            self.value = self.P_TO_I[pager_token_name][1](value)
            self.known_token = True
        except KeyError:
            # just use strings and lower the key
            self.name = pager_token_name.lower()
            self.value = value


class TraceEntry(object): # ignore
    TRACE_ENTRY_RGX_TEXT = r"(?P<module>\S+):(?P<line>\d+) \[(?P<method>\S+?)\] \[(?P<process_info>\S+)\]: (?P<trace_msg>.*)"
    TRACE_ENTRY_RGX_JSON_LINES = "(.*)\n"

    def __init__(self, pager_tokens, hostname=None):
        self._token_entries = {}

        for token in pager_tokens:
            self._token_entries[token.name] = token.value

        # pop common tokens
        self.channel = self._token_entries.pop("channel", None)
        self.message = self._token_entries.pop("message", None)
        self.severity = self._token_entries.pop("severity", None)
        self.k_pid = self._token_entries.pop("kernel_pid", None)
        self.kernel_task_name = self._token_entries.pop("kernel_task_name", None)
        self.time_stamp = self._token_entries.pop("time_stamp", None)
        self.datetime = datetime.datetime.fromtimestamp(self.time_stamp // 1000000000)
        self.trace_name = self._token_entries.pop("trace_name", None)
        self.topo_debug_id = self._token_entries.pop("topo_debug_id", None)
        self.hostname = hostname
        # method is not yet visible with json

        try:
            match = re.match(self.TRACE_ENTRY_RGX_TEXT, self.message)
            assert match, 'Trace entry parsing failed: {}'.format(repr(self.message))
            self.method = match.groupdict()['method']
        except:
            self.method = None

    def __getitem__(self, token_key):
        try:
            return self._token_entries[token_key]
        except KeyError:
            return getattr(self, token_key)


class PagerCmdMonitor(CmdMonitor):

    def __init__(self, cmd, host):
        super(PagerCmdMonitor, self).__init__(cmd, host, log_option=0)
        self.set_logger_level(logging.INFO)
        self.add_msg_handler(PatternHandler([TraceEntry.TRACE_ENTRY_RGX_JSON_LINES]))
        self.traces = []

    def on_event(self, event):
        json_event = json.loads(event[MsgHandler.FULL_MSG])

        pager_tokens = []
        for k, v in json_event.items():
            # we are convert to str due to unicode issue
            pager_tokens.append(PagerToken(str(k), str(v)))
        self.traces.append(TraceEntry(pager_tokens, self.host))


class PagerUtils(object):
    LOGS_PATH = "/var/log/nvmesh/trace_daemon/"
    PAGER_PATH = LOGS_PATH + "pager.py"
    NFS_CLIENT_CONF = LOGS_PATH + "tracedaemon.conf"
    CLIENT_ECHO = "/proc/nvmeibc/echo"
    TIME_FORMAT = "%d/%m/%Y %H:%M:%S"

    @classmethod
    def construct_pager_cmd(cls, logs_path: str = LOGS_PATH, pager_path: str = PAGER_PATH, filter_query: Optional[str] = None, time_query: Optional[str] = None,
                            channels: Optional[Iterable[str]] = None) -> str:
        cmd_list = ["cd {} ".format(logs_path), "&& ", "python3 ", pager_path, "--mode msg-stream-json",
                    "--silent"]
        # calculating the additional args and queries. default values request for all LONG channel traces as txt
        channel_arg = "-l {}".format(" ".join(channels)) if channels else ""

        query_args = ""
        if time_query:
            query_args += " -t {}".format(time_query)
        if filter_query:
            query_args += " -f '{}'".format(filter_query)
        # add the pager call with all its arguments to cmd
        cmd_list.append('{} {}'.format(channel_arg, query_args))

        return " ".join(cmd_list)

    @classmethod
    def get_pager_results(cls, host: 'Host', logs_path: str = LOGS_PATH, pager_path: str = PAGER_PATH, filter_query: Optional[str] = None, time_query: Optional[str] = None) -> List[TraceEntry]:
        from xlro.core.entities.client import ClientNode, Host
        traces_conf = ClientNode.instance(name=host.name).traces_conf
        if traces_conf:
            traces_conf = traces_conf.split(':')
            logs_path = traces_conf[1]
            host = Host.instance(name=traces_conf[0])

        cmd = cls.construct_pager_cmd(logs_path, pager_path, filter_query, time_query)
        return cls.run_pager_cmd(cmd, host)

    @classmethod
    def run_pager_cmd(cls, cmd: str, host: 'Host') -> List[TraceEntry]:
        m = PagerCmdMonitor(cmd, host.name)
        m.run()
        logger.info('TRACER: found %d traces.', len(m.traces))
        return m.traces

    # convenience methods
    @classmethod
    def get_field_eq(cls, field_name: str, value: Any) -> str:
        return '@{} = {}'.format(field_name, value if isinstance(value, int) else '\"{}\"'.format(value))

    @classmethod
    def get_field_in(cls, field_name: str, value: Any) -> str:
        return '@{} in [{}]'.format(field_name, value)

    @classmethod
    def get_field_like(cls, field_name: str, value: Any) -> str:
        return '@{} like \"{}\"'.format(field_name, value)

    @classmethod
    def get_multi_instance_idx_filter(cls, idx: int) -> str:
        return cls.get_field_like('K_TASK_NAME', '*{}'.format(idx))

    @classmethod
    def get_func_filer(cls, func_name: str) -> str:
        return 'func={}'.format(func_name)

    @classmethod
    def get_time_query(cls, start_time: Optional[datetime.datetime], end_time: Optional[datetime.datetime], host_dt: Optional['Host'] = None) -> Optional[str]:
        if start_time:
            if end_time:
                return cls.get_between_time(start_time, end_time, host_dt=host_dt)
            else:
                return cls.get_from_time(start_time, host_dt)
        elif end_time:
            return cls.get_to_time(end_time)

        else:
            return None

    @classmethod
    def get_between_time(cls, from_time: datetime.datetime, to_time: datetime.datetime, include_from: Optional[bool] = False, include_to: Optional[bool] = False, host_dt: Optional['Host'] = None) -> str:

        # TODO - address to 'include_from' and 'include_to'
        if host_dt:
            from_time += host_dt.time_delta
            to_time += host_dt.time_delta
        from_time_str = cls.convert_time_struct_2_pager_str(from_time)
        to_time_str = cls.convert_time_struct_2_pager_str(to_time)

        return "'{}' '{}'".format(from_time_str, to_time_str)

    @classmethod
    def get_to_time(cls, to_time: datetime.datetime) -> str:
        raise NotImplementedError

    @classmethod
    def get_from_time(cls, from_time: datetime.datetime, host_dt: Optional['Host'] = None) -> str:
        time_delta = (datetime.datetime.now() - from_time) + host_dt.time_delta if host_dt else datetime.datetime.now() - from_time
        return cls.get_tail(time_delta)

    @classmethod
    def get_tail(cls, tail_time: datetime.timedelta) -> str:
        return "-tail {}".format(int(tail_time.total_seconds() * 10**3))

    @classmethod
    def get_has_field(cls, field_name: str) -> str:
        return "HAS @{}".format(field_name)

    @classmethod
    def get_has_traces(cls, traces: Iterable[Union[str, TraceType]]) -> str:
        return "||".join(['(trace = "{}")'.format(getattr(trace, 'trace_name', trace)) for trace in traces])

    @classmethod
    def convert_time_struct_2_pager_str(cls, datetime_obj: datetime.datetime) -> str:
        return datetime_obj.strftime(cls.TIME_FORMAT)

    @classmethod
    def echo_pager(cls, hosts, msg):
        try:
            from shlex import quote as cmd_quote  # type: ignore
        except ImportError:
            from pipes import quote as cmd_quote  # type: ignore

        msg = "echo {} | sudo tee {}".format(cmd_quote(msg), cls.CLIENT_ECHO)
        with ThreadPoolManager() as executor:
            for h in hosts:
                executor.add_task(str(h), h.execute, cmd=msg, reconnect_timeout=0)
            executor.start()

        return [(task, res.result() if not res.exception() else res.exception())
                for task, res in executor.results.items()]


class PagerHandler(logging.Handler):
    _instance = None
    _lock = threading.Lock()

    class PagerFilter(logging.Filter):
        def filter(self, record):
            return hasattr(record, "hosts")

    def __init__(self):
        super(PagerHandler, self).__init__(logging.INFO)
        self.formatter = logging.Formatter('[%(logger_name)-12s] %(message)s')
        self.addFilter(self.PagerFilter())

    @classmethod
    def get_instance(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = cls() # type: ignore
        return cls._instance

    def emit(self, record):
        PagerUtils.echo_pager(record.hosts, self.format(record))
