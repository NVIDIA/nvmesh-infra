# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import next
from builtins import zip
from builtins import str
from future.utils import viewkeys
from builtins import object
from datetime import datetime

from typing import Any,Deque,Dict,IO,Iterable,List,Mapping,Optional,Pattern,Sequence,Union # pylint: disable=unused-import
import os
import threading
import time
import collections
import re
import sys
import logging
import socket
import paramiko
from itertools import count, chain
from jinja2 import Template

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from xlro.core.util.general_utils import wait_for_it, wait_for_threads, IDAdapter
from xlro.core.util.ssh import Connection, RemotePopen, LocalPopen
from xlro.core.util.actions import Action
from xlro.core import is_infra_running, infra_conf

MON_LOGGER = logging.getLogger('xlro.core.util.monitor')
PY3 = sys.version_info[0] == 3

def matchdict(match):
    # Extract non-None, named matches, if any
    return {} if not match else {k:v for k,v in match.groupdict().items() if v is not None}

class MsgHandler(object): # pylint: disable=too-few-public-methods
    """
    A message handler can do process all messages going through a monitor.
    A primary usage is to optionally generate a meaningful "event" if the message is interesting.
    If a value (a dict) is returned, it's added to the monitor's event list.
    Various "special" keys can be in the map to signal things to the monitor.
    """
    FULL_MSG = '__message'
    SIG_IGNORE = '__ignore'
    SIG_EVENTID = '__eventid'
    LOG_LEVEL = '__loglevel'
    EVENT_FORMAT = '__format'
    EVENT_TEMPLATE = '__template'
    NON_ERROR = 'NON_ERROR'

    def handle_msg(self, msg: str) -> Optional[Dict[str, Any]]: # pylint: disable=unused-argument,no-self-use
        return None


class SpecialPattern(object):
    """
        Abstract base-class for a special Pattern handler which returns an event on matches.
        Right now, the only example is a RangePattern.
    """
    def match(self, msg: str) -> Optional[Dict]:
        return None

class RangePattern(SpecialPattern):
    """
        RangePattern allows matching multiple lines between a start and end pattern (inclusive)
    """
    DUMMY_MATCH = re.match('', '')

    def __init__(self, startPattern: str, endPattern: str, maxLines: int = 20, middlePattern: Optional[str] = None) -> None:
        """
        startPattern is the pattern which starts the multi-line match
        endPattern is the pattern which ends the multi-line match
        maxLines is the maximum amount of lines we'll search for endPattern before abandoning the match.
        middlePattern is an optional pattern. If set, only matching lines between start/end will be added to event.
        If endPattern == None, maxLines lines from the startPattern will be returned as an event
        """
        self.startPattern = re.compile(startPattern)
        self.endPattern = re.compile(endPattern) if endPattern else None
        self.middlePattern = re.compile(middlePattern) if middlePattern else None
        self.maxLines = maxLines
        self.message: Optional[str] = None
        self.range_lines = 0
        self.event: Optional[Dict] = None

    def match(self, msg: str) -> Optional[Dict]:
        """
            Check msgs for matches. If we get a valid start..end sequence, return an event
        """
        if not self.message:
            # Not in range already, so just check for start
            match = self.startPattern.search(msg)
            if match:
                self.message = msg
                self.event = matchdict(match)
                self.range_lines = 0
            return None

        # Otherwise, we're within a range
        self.range_lines += 1
        if self.range_lines > self.maxLines:
            # Range too long, cancel
            self.message = None
            return None

        if not self.middlePattern:
            self.message += msg
        else:
            match = self.middlePattern.search(msg)
            if match:
                self.message += msg
                # If value is None, the pattern didn't match at all (vs. matching nothing)
                # e.g., (?P<tag>\S+)? vs. (?P<tag>\S*)
                if not self.event:
                    self.event = {}
                self.event.update(matchdict(match))

        # Check for end...
        match = None
        if self.endPattern:
            match = self.endPattern.search(msg)
        elif self.range_lines == self.maxLines:
            match = self.DUMMY_MATCH
        if match:
            # We've completed a range.
            assert self.event is not None, 'Event should have been initialized on start-message'
            self.event.update(matchdict(match))
            self.event[MsgHandler.FULL_MSG] = self.message
            self.message = None
            return self.event
        return None

class PatternHandler(MsgHandler): # pylint: disable=too-few-public-methods
    """
    The pattern handler applies one or more regex patterns to the messages, and generates an event if matched.
    The patterns are assumed to have "named groups" which extract parts of the message as entries in the map.
    You can supply many patterns, but only first match will emit an event.
    """
    def __init__(self, patterns: Union[str, list, Mapping], event_defaults: Optional[Dict] = None) -> None:
        """
        Options for 'patterns' include:
        - a simple regex string, for example 'Error in (?P<module>\w+) - (?P<errmsg>.*)'
        - a dict where each key is a primary regex. If it matches, the 'patterns' which are it's values are recursively applied.
        for example: { '^PANIC:' : [ '.*verify error', '.*IO error' ] }
        - a list of the above pattern types
        """
        self.orig_strings: Dict[Any, str] = {}
        self.patterns = self._compile(patterns)
        self.event_defaults = event_defaults or {}

    def _compile(self, pattern):
        """ pre-compile the various patterns provided. """
        if isinstance(pattern, (SpecialPattern, Pattern)):
            return pattern
        elif isinstance(pattern, str):
            compiled = re.compile(str(pattern))
            self.orig_strings[compiled] = pattern
            return compiled
        elif isinstance(pattern, collections.abc.Sequence):
            return [self._compile(pitem) for pitem in pattern]
        elif isinstance(pattern, dict):
            if 'patternclass' not in pattern:
                # Default is a hierarchy of patterns
                return {self._compile(pkey): self._compile(pvalue) for pkey, pvalue in pattern.items()}
            # Otherwise, a "special pattern" - although this auto-compile option is limited to leaf nodes.
            # This is mostly a hack to support special patterns in YAML based monitors
            pdict = pattern.copy()
            pclass = pdict.pop('patternclass', None)
            try:
                compiled = globals()[pclass](**pdict)
                self.orig_strings[compiled] = str(pdict)
                return compiled
            except Exception as e:
                raise Exception(f'Failed to create patternclass: {pclass} : {repr(e)}')
        raise Exception('Invalid object in patterns: {} {}'.format(type(pattern), pattern))

    def _match(self, pattern, msg):
        event: Optional[Dict[str, Any]] = None
        if isinstance(pattern, collections.abc.Sequence):
            for pitem in pattern:
                event = self._match(pitem, msg)
                if event is not None:
                    # First match (later, we might handle continue signal...)
                    break
        elif isinstance(pattern, collections.abc.Mapping):
            for pkey, pvalue in pattern.items():
                event = self._match(pkey, msg)
                if event and not self.SIG_IGNORE in event:
                    subevent = self._match(pvalue, msg)
                    if subevent and not self.SIG_IGNORE in subevent:
                        event.update(subevent)
                        break
                    else:
                        event = None
        elif isinstance(pattern, SpecialPattern):
            s_event = pattern.match(msg)
            if s_event:
                event = self.event_defaults.copy() if self.event_defaults else {}
                event.update(s_event)
        else:
            match = pattern.search(msg)
            if match:
                event = self.event_defaults.copy() if self.event_defaults else {}
                event[MsgHandler.FULL_MSG] = msg
                event.update(matchdict(match))
        result = event if event and self.SIG_IGNORE not in event else None
        return result

    def handle_msg(self, msg):
        return self._match(self.patterns, msg) or None


class Monitor(object):
    """
    Base class for scanning messages, generating and handling events
    Abstract because it doesn't create a message pulling thread. It can manually be fed strings via on_message.
    """

    # Special event keys
    CLASS = '_class'
    TIMESTAMP = '_timestamp'
    SOURCE = '_source'
    CREATOR = '_creator'
    EVENT_ID = '_id'
    MON_NAME = '_mon_name'
    ANY = object()  # a unique value
    _e_id = count()
    ACTION_TIMEOUT = 60                 # triggered action shouldn't last this long. (timeout pre trigger?)
    ACTION_THREADS: Dict['Monitor', List[threading.Thread]] = defaultdict(list)
    AT_LOCK = threading.Lock()

    log_path_dict: Dict[str, int] = defaultdict(lambda: -1)

    class FatalMonitorException(Exception):
        pass

    class LoggingOptions(object):
        LOG_EVENT_TO_LOG = 1 << 1
        LOG_EVENT_TO_FILE = 1 << 2
        LOG_MESSAGE_TO_FILE = 1 << 3
    DEFAULT_LOGGING_OPTION = LoggingOptions.LOG_MESSAGE_TO_FILE | LoggingOptions.LOG_EVENT_TO_LOG | LoggingOptions.LOG_EVENT_TO_FILE

    def __str__(self):
        return self._str

    def __repr__(self):
        return self._str

    def __init__(self, handlers=None, event_defaults=None, logpath=None, logmode='w', log_option=DEFAULT_LOGGING_OPTION,
                 name=None, **monitor_kwargs):
        self.event_defaults = event_defaults.copy() if event_defaults else {}
        self._events: List[Dict] = []
        self.handlers = handlers or []
        self.logpath: Optional[str] = None
        if logpath:
            self.log_path_dict[logpath] += 1
            self.logpath = ''.join((logpath, '-', str(self.log_path_dict[logpath]))) if self.log_path_dict[logpath]\
                else logpath
        self.logmode = logmode
        self.logfile: Optional[IO] = None
        self.started = False
        self.ignoring = False
        self.logger: Union[IDAdapter, logging.Logger] = IDAdapter(logging.getLogger(name or self.__class__.__name__))
        self._str = (name or self.__class__.__name__) + '.' + str(self.logger.extra['id'])
        self.event_defaults[self.CLASS] = self.__class__.__name__.rpartition('.')[2]
        self.log_option = log_option
        self.name = name
        self.actions: List[Action] = []

    @classmethod
    def get_logger(cls) -> logging.Logger:
        return MON_LOGGER.getChild(cls.__name__)

    @classmethod
    def set_logger_level(cls, level: int) -> None:
        cls.get_logger().setLevel(level)

    def add_msg_handler(self, handler: MsgHandler) -> None:
        if handler not in self.handlers:
            self.handlers.append(handler)

    def remove_msg_handler(self, handler: MsgHandler) -> None:
        try:
            self.handlers.remove(handler)
        except:
            pass

    def add_action(self, action: Action) -> None:
        if action not in self.actions:
            self.actions.append(action)

    def remove_action(self, action: Action) -> None:
        try:
            self.actions.remove(action)
        except:
            pass

    @property
    def events(self):
        # This is to allow MultiMonitor to override
        return self._events if not self.ignoring else []

    @staticmethod
    def match_dict(d: Dict, m: Dict) -> bool:
        ''' Check, hierarchically, if dict d matches match-dict m. Keys are exact match.
            Leaf values of m can be ANY to match any value or sub-dict.
        '''
        try:
            if not viewkeys(m) <= viewkeys(d):
                return False        # There are m keys that aren't even in d
        except:
            return False            # Someone isn't a dict...
        for mk, mv in m.items():
            if isinstance(mv, dict):
                if not Monitor.match_dict(d[mk], m[mk]):
                    return False
            elif mv != Monitor.ANY and mv != d[mk]: # TODO: Guess we could expand to patterns...
                return False
        return True

    def match_events(self, includes: Iterable[Union[str, Dict]] = [], excludes: Iterable[Union[str, Dict]] = [], limit=0) -> List[Dict]:
        ''' match events with filtering.
            includes/excludes are lists if str or dicts.
            If str, matches against event keys.
            If dict, matches if all key/values are present in event.
            includes : at least ONE include MUST match
            if excludes : NONE of the exclude can match
        '''
        matches = []
        # any(imap()) should be fast fail, so not as bad as it looks :-)
        for e in self.events:
            if (not includes or any(map(lambda v: (v in e) if isinstance(v, str) else self.match_dict(e, v), includes))) \
                    and (not excludes or not any(map(lambda v: (v in e) if isinstance(v, str) else self.match_dict(e, v), excludes))):
                matches.append(e)
                if limit and len(matches) >= limit:
                    break
        return matches

    def get_events(self, match_props=None):
        return self.events if not match_props else self.match_events(includes=[match_props] if isinstance(match_props, str) else match_props)

    def ignore(self):
        # In ignore mode, no events will be handled or saved.
        self.reset()
        self.ignoring = True

    def unignore(self):
        self.ignoring = False
        self.start()

    def start(self):
        self.logger.debug(f'MON start({str(self)})')
        if self.logpath and not self.logfile:
            # Create directory if needed.
            logd = os.path.dirname(self.logpath) or '.'
            if not os.path.isdir(logd):
                os.makedirs(logd)
            self.logger.debug('open-log(%s)', self.logpath)
            self.logfile = open(self.logpath, self.logmode, buffering=1)
        self.started = True
        self.ignoring = False

    @classmethod
    def wait_for_all_actions(cls, timeout=ACTION_TIMEOUT):
        logger = cls.get_logger()
        with cls.AT_LOCK:
            action_threads = list(chain(*list(cls.ACTION_THREADS.values())))
            cls.ACTION_THREADS = defaultdict(list)
        wait_for_threads(action_threads, timeout=timeout, stop=False)

    def stop(self):
        self.logger.debug('pre-stop()')
        if self.logfile:
            self.logger.debug('close-log(%s)', self.logpath)
            self.logfile.close()
            self.logfile = None

        # Stop/wait for action-threads...
        with self.AT_LOCK:
            threads = self.ACTION_THREADS.pop(self, [])
        wait_for_threads(threads, self.ACTION_TIMEOUT, stop=True)
        self.logger.debug('post-stop()')

    def localize_event(self, rawevent: Dict) -> Dict:
        if not self.event_defaults:
            return rawevent
        event = self.event_defaults.copy()
        event.update(rawevent)
        return event

    def _add_event(self, event):
        if self.ignoring:
            return
        event = self.localize_event(event)
        event.setdefault(self.TIMESTAMP, "{}".format(datetime.now()))
        # Adding and removing was a bad idea, so we don't add the event unless it returns
        # to Monitor.on_event().  Any Monitor can swallow an event by not calling super()
        # To ensure only the creating monitor stores the event, we store the owning Monitor ID
        # self._events.append(event)
        event[self.SOURCE] = self.__class__.__name__
        event[self.CREATOR] = id(self)    # Keep event jsonable
        event[self.EVENT_ID] = next(self._e_id)
        event[self.MON_NAME] = self.name
        self.on_event(event)

    def _eget(self, event, key, default=None):
        return event.get(key, self.event_defaults.get(key, default))

    def format_event(self, event):
        etemplate = self._eget(event, MsgHandler.EVENT_TEMPLATE)
        if etemplate:
            try:
                return str(Template(etemplate).render(**self.localize_event(event)))
            except Exception as e:
                self.logger.info('Event formatting error: ({}) {}'.format(type(e), e))
        eformat = self._eget(event, MsgHandler.EVENT_FORMAT)
        if eformat:
            try:
                return eformat.format(**self.localize_event(event))
            except Exception as e:
                self.logger.info('Event formatting error: ({}) {}'.format(type(e), e))
        return 'EVENT: ' + str(event)

    def on_message(self, msg: str) -> None:
        # Copy the handler list, in case someone unregisters in their event handler...
        if self.logfile and (self.log_option & self.LoggingOptions.LOG_MESSAGE_TO_FILE):
            self.logfile.write(msg)
        for handler in self.handlers[:]:
            event = None
            try:
                event = handler.handle_msg(msg) # Dict
                if event:
                    if self.log_option & self.LoggingOptions.LOG_EVENT_TO_LOG:
                        # Note: This will log events when recognized, even if later swallowed or ignored.
                        loglevel = logging._checkLevel(  # type: ignore[attr-defined]
                                self._eget(event, MsgHandler.LOG_LEVEL, logging.DEBUG))
                        self.logger.log(loglevel, self.format_event(event))
                    if MsgHandler.SIG_IGNORE in event:
                        break
                    self._add_event(event)
            except Exception as e:
                self.logger.info('Handler {} failure: ({}) {}, event: {}'.format(
                        handler, type(e), e, event))

    def on_event(self, event: Dict) -> None:
        if event.get(self.CREATOR) == id(self):
            self._events.append(event)
        if self.logfile:
            if self.log_option & self.LoggingOptions.LOG_EVENT_TO_FILE:
                print(self.format_event(event), file=self.logfile, flush=True)

        # Hopefully, this can replace RelayHandlers?
        for action in self.actions:
            # Enable BG actions returning their thread.
            ret = action.handle_event(event)
            if isinstance(ret, threading.Thread):
                with self.AT_LOCK:
                    self.ACTION_THREADS[self].append(ret)

        # NOTE: This is a likely indicator the design was wrong, but we need to call on_event of parent monitors...
        for handler in [h for h in self.handlers if isinstance(h, RelayHandler)]:
            try:
                handler.monitor.on_event(event)
            except Exception as e:
                self.logger.info('Event relay error to: ({}) {} - ({}) {}.'.format(
                    type(handler.monitor), handler.monitor, type(e), e))
                pass

    def reset(self):
        self._events[:] = []

    @staticmethod
    def log_monitor(logger, monitor, indent=0, prefix=''):
        ''' Recursive log, especially for visualizing MultiMonitor '''
        logger.debug(f'{prefix}{"  " * indent}{monitor}. # Events: {len(monitor.events)}')
        if isinstance(monitor, MultiMonitor):
            for submon in monitor.monitors:
                Monitor.log_monitor(logger, submon, indent+1, prefix)



def get_process(cmd: str, host: Optional[str] = None, **kwargs: Any) -> Union[RemotePopen, LocalPopen]:
    if host: # TODO: check for localhost, 127.0.0.1, etc.
        # NOTE: get_pty serves two purposes. 1) join STDOUT/STDERR, 2) Kill remote command when we disconnect
        kwargs.setdefault('get_pty', True)
        return Connection.get_connection(host).popen(cmd, **kwargs)
    else:
        # NOTE: local joins STDOUT/STDERR to same stream (despite the strong GhostBusters warnings)
        return LocalPopen(cmd, **kwargs)


class CmdMonitor(Monitor):
    """
    Basic wrapper to run a long-running command and route each output line to a set of handlers and collect events.
    Logging is here, but needs more thought. Would be nice to support a log at Multi level, and allow event logs
    """
    EXIT = 'EXIT'
    DISCONNECT = 'DISCONNECT'
    UNSTARTED = -256 # Not a possible exit code
    TAIL = 5 # Number of tail lines to save for bad exit message
    # TODO: inherit from Popen and auto-start?
    # TODO: logging

    def __init__(self, cmd: str, host: str, inbuf: Optional[Any] = None, get_pty: Optional[bool] = True, expected: Optional[Union[List,int]] = None, resilient: bool = False, **kwargs: Any) -> None:
        self.inbuf = inbuf
        self.get_pty = get_pty
        # Default expected should be 0, but we'll roll this out gradually
        self.expected = expected
        if type(self.expected) == int:
            self.expected = [self.expected]
        super(CmdMonitor, self).__init__(**kwargs)
        if cmd[0] == '@':
            # This enables an f-string like command-line which can reference {MGR} or {NODES}.
            # TODO: What else can be templated? Is this the right place?
            try:
                from xlro.core.entities import Manager
                from xlro.core.util.general_utils import host_name
                mgr = Manager.get_manager()
                cmd = cmd[1:].format(
                        NODES = ' '.join({n.name for n in mgr.get_all_subsystems() if not n.name.startswith('scale-')}),
                        MGR = mgr.host,
                        MGRS = ' '.join({host_name(mgr.endpoint_to_host(e)) for e in mgr.mgmt_cluster}),
                )
            except Exception as e:
                raise Exception(f'Failed to format cmd for monitor. {repr(e)}. cmd={cmd}')
        self.cmd = cmd
        self.host = host
        self.event_defaults['host'] = host
        self.process: Optional[Union[LocalPopen, RemotePopen]] = None
        self.msg_thread: Optional[threading.Thread] = None
        self.return_code = self.UNSTARTED
        # Used to prevent unexpected exit event when stop()ing
        self.stopping = False
        # In case of unexpected exit code, keep some output context
        self.tail: Deque = deque(maxlen=self.TAIL)
        self.resilient = resilient
        self.warn_on_msgs = 1000

    def __str__(self):
        return '{}: @{} "{}" (Process={}, State:{})'.format(
                super(CmdMonitor, self).__str__(), self.host, self.cmd.partition('\n')[0], 
                self.process is not None, self.poll_all())

    def _read_messages(self):
        self.logger.debug("read messages thread is running")
        assert self.process and self.process.stdout, 'No process to read from!'
        e = None
        msgs_count = 0
        base_time = time.time()

        while self.process and is_infra_running():
            try:
                stdout: Union[IO[str], paramiko.channel.ChannelFile] = self.process.stdout

                line = stdout.readline()
                if PY3 and isinstance(line, bytes):
                    line = line.decode('utf-8')

                # Chose a really arbitrary numbers here - protect high CPU
                if msgs_count == self.warn_on_msgs:
                    current_time = time.time()
                    diff = (current_time - base_time)
                    if diff <= 30:
                        self.logger.warning('High Workload - handle {} messages in {} seconds'.format(msgs_count, diff))
                        self.logger.debug('Workload tail:\n' + "tail: \\n".join(list(self.tail)))
                        # Clumsy backup, since we see too many repeated
                        self.warn_on_msgs *= 2
                    msgs_count = 0
                    base_time = current_time
                else:
                    msgs_count += 1

                if not self.process or not is_infra_running():
                    self.logger.debug("closing thread")
                    break
                if line == '':
                    if not isinstance(stdout, paramiko.channel.ChannelFile):
                        break
                    raise StopIteration("_read_messages got EOF")
                self.tail.append(line.strip())
                self.on_message(line)
            except Exception as read_e:
                if not isinstance(stdout, paramiko.channel.ChannelFile):
                    self.logger.info("subprocess popen got {}".format(repr(read_e)))
                    break
                e = stdout.channel.transport.saved_exception
                if e:
                    self.logger.info("got transport exception - breaking")
                    break
                if isinstance(read_e, StopIteration):
                    e = read_e
                    self.logger.info("got EOF - breaking from readline loop")
                    break
                if isinstance(read_e, IOError):
                    if not self.process:
                        self.logger.info("got io-error and process is none -"
                                         " probably that stdout was closed by other thread - breaking")
                        break
                    if not isinstance(read_e, socket.timeout):
                        # we expect socket timeout sometimes - configured in _exec_command (ssh.py)
                        self.logger.warn("got IOError which is not timeout,"
                                         " but process is not None: {}".format(repr(read_e)))
                self.logger.debug("error {} accepted - sending ignore to check connection".format(repr(read_e)))
                # not a transport connection error and not EOF - we can assume connection is up
                try:
                    # simplest way to ensure connection - will make socket to be closed if connection is down
                    stdout.channel.transport.send_ignore()
                except Exception as send_e:
                    self.logger.info("connection cant send ignore request")
                    e = send_e
                    break

        if self.process and not self.stopping and is_infra_running():
            self.logger.debug("waiting for process to return code")
            rc = self.process.wait()
            self.logger.debug("process returned with exit code {}".format(rc))
            self.return_code = rc if not self.stopping else 0
            if not self.stopping and isinstance(self.expected, Iterable) and self.return_code not in self.expected:
                if e and self.resilient:
                    self._add_event({self.DISCONNECT: self.return_code, 'tail': '\n'.join(self.tail),
                                     'exception': e, MsgHandler.FULL_MSG: "eof"})
                else:
                    self._add_event({self.EXIT: self.return_code, 'tail': '\n'.join(self.tail),
                                     MsgHandler.FULL_MSG: "eof"})

    def start(self):
        # if process terminated itself, use stop() to reset the procs
        if self.process and self.process.poll() is not None:
            self.stop()
        if not self.process:
            self.process = get_process(self.cmd, self.host, inbuf=self.inbuf, get_pty=self.get_pty)

        # now it is safe to call super.start() as we want the monitor log ile to be open before msg-reader
        super(CmdMonitor, self).start()
        if not self.msg_thread:
            self.msg_thread = threading.Thread(target=self._read_messages, name='msg-reader-{}'.format(self._str))
            self.msg_thread.daemon = True
            self.msg_thread.start()

    def stop(self):
        self.stopping = True
        if self.process:
            self.process.terminate()
            self.process = None
            self.return_code = 0 # If we stopped the process the exit code is not relevant
        if self.msg_thread:
            self.logger.debug("joining the message thread")
            self.msg_thread.join(10)
            if self.msg_thread.is_alive():
                self.logger.warn("_message thread is still alive after join")
            self.msg_thread = None

        super(CmdMonitor, self).stop()
        self.stopping = False

    def poll_all(self):
        return [self.process.poll()] if self.process else [self.return_code]

    def wait_all(self):
        if self.process:
            # Let msg_thread wait on process and set return_code...
            if self.msg_thread:
                self.msg_thread.join()
            self.msg_thread = None
            self.process.terminate()
            self.process = None
        return [self.return_code]

    def run(self):
        self.start()
        self.wait_all()
        self.stop()

    def on_event(self, event: Dict) -> None:
        super(CmdMonitor, self).on_event(event)

        if isinstance(self.process, LocalPopen) or not self.process:
            return

        if self.DISCONNECT in event and self.resilient:
            e = event['exception']
            self.logger.warning("restart monitor due to: {}".format(repr(e)))
            # process is already finished
            self.process.terminate()
            self.process = None
            # current msg_thread will create the new message_thread and exit later
            self.msg_thread = None

            connection = Connection.get_connection(self.host)
            connection._need_to_reconnect = True
            assert wait_for_it(connection.reconnect, poll=10, timeout=60*10), "reconnect timeout reached"
            # now we can re-start the command
            self.start()


class PidMonitor(CmdMonitor):
    # There were MUCH simpler ways, but I was trying to exercise the infra
    PID_PROP = 'PID'
    PID_PATTERN = r'^PID=(?P<{}>\d+)'.format(PID_PROP)
    BOOT_PROP = 'BOOT_TIME'
    BOOT_PATTERN = r'(?P<{}>.* system boot .*\d)'.format(BOOT_PROP)

    def __init__(self, cmd, *args, **kwargs):
        for flag in ('term_sig', 'term_wait', 'kill_sig', 'kill_wait', 'stop_targets'):
            setattr(self, flag, kwargs.pop(flag, getattr(infra_conf.root.pid_stop, flag)))
        if 'PID=$$' not in cmd:
            cmd = 'who -b && echo PID=$$ && exec ' + cmd
        super(PidMonitor, self).__init__(cmd, *args, **kwargs)
        self.pid_handler = PatternHandler(self.PID_PATTERN)
        self.boot_time_handler = PatternHandler(self.BOOT_PATTERN)
        self.pid = None
        self.boot_stamp = None

    def on_event(self, event):
        if event and self.PID_PROP in event:
            self.pid = event[self.PID_PROP]
            # We only need one event, so we unregister right away and don't pass on the event
            self.remove_msg_handler(self.pid_handler)
        elif event and self.BOOT_PROP in event:
            self.boot_stamp = event[self.BOOT_PROP].strip()
            self.remove_msg_handler(self.boot_time_handler)
        else:
            super(PidMonitor, self).on_event(event)

    def start(self):
        self.add_msg_handler(self.pid_handler)
        self.add_msg_handler(self.boot_time_handler)
        super(PidMonitor, self).start()

    def stop(self):
        ''' Special stop() to kill pid and all descendants '''
        from functools import partial
        host_exec = partial(Connection.execute_on_host, self.host)

        self.stopping = True
        self.logger.debug('stopping pid-monitor. pid={}. boot={}'.format(self.pid, self.boot_stamp))
        if self.pid:
            try:
                if self.boot_stamp:
                    if host_exec('who -b')[0].strip() != self.boot_stamp:
                        self.logger.info(f"boot time differs - not killing command {self.pid}")
                    else:
                        # Get all descendants of pid.
                        # Process group seemed unreliable - especially for FIO
                        # pstree unreliable - sometimes returned empty
                        pidinfo, _, _ = host_exec(f'''
                            ps -eo pid,ppid,stat,cmd \
                                | awk '{{
                                        if ($3 == "Z") next;
                                        if ($1 == {self.pid}) kids[$1] = 0;
                                        if ($2 in kids) {{ kids[$2] += 1; kids[$1] = 0; }}
                                    }}
                                    END {{ for (p in kids) print kids[p] ":" p; }}' 2>&1
                            ''')
                        # Added self.pid, even if not found.  But don't really understand
                        pids = set(re.findall(r'^[0-9]+:([0-9]+)', pidinfo, re.M)) or {self.pid}
                        if not pids:
                            self.logger.info(f'No PIDs found for {self.pid}.  Assuming already stopped.')
                        else:
                            allpids = ' '.join(sorted(pids, key=int, reverse=True))
                            self.logger.debug(f'PIDS: {allpids} stop_targets: {self.stop_targets}')
                            if self.stop_targets == 'leaves':
                                # Use only last pid on line...
                                leaves = sorted(re.findall(r'^0:([0-9]+)', pidinfo, re.M), key=int, reverse=True)
                                pids_to_kill = ' '.join(leaves)
                                if not pids_to_kill:
                                    self.logger.warning(f'Failed to find leaf PIDs for {self.pid}!  Killing all.')
                                    pids_to_kill = allpids
                            elif self.stop_targets == 'all':
                                pids_to_kill = allpids
                            else:
                                pids_to_kill = self.pid
                            out, err, code = host_exec(f'sudo kill -{self.term_sig} {pids_to_kill}')
                            if code or out or err:
                                self.logger.debug(f'Kill (exit={code}). {out} {err}')
                            def not_running():
                                new_running = host_exec(f'ps --no-headers -o stat,pid,cmd {allpids} | grep -v "^Z"')[0].strip()
                                running = host_exec(f'ps -f {allpids} | tail -n +2')[0].strip()
                                if running:
                                    self.logger.debug(f'Running: {running}')
                                    if not new_running:
                                        self.logger.info(f'Running: found defunct procs?!')
                                return not running

                            if not wait_for_it(not_running, timeout=self.term_wait):
                                assert self.kill_sig and self.kill_sig.upper() != 'FAIL', 'Procs still running!'
                                self.logger.info(f'Procs still running after {self.term_wait} seconds.  Killing')
                                host_exec(f'sudo kill -{self.kill_sig} -- {pids_to_kill}')
                                try:
                                    wait_for_it(not_running, timeout=self.kill_wait).assert_result('Procs still running!')
                                except Exception as e:
                                    raise self.FatalMonitorException(f'Procs still running!  Failed to stop: {self}')

                self.pid = None
                self.boot_stamp = None
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(f'PidMonitor stop() failed: {repr(e)}')
        super(PidMonitor, self).stop()

    def run(self):
        self.start()
        self.wait_all()
        self.pid = None
        self.stop()


class PollingMonitor(Monitor):
    EXIT_CODE = 'EXIT-CODE'

    def __init__(self, cmd: str, host: str, interval: float, *args: Any, **kwargs: Any) -> None:
        super(PollingMonitor, self).__init__(*args, **kwargs)
        self.cmd = cmd
        self.host = host
        self.event_defaults['host'] = host
        self.interval = interval
        self.stopped = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.process: Optional[Union[RemotePopen, LocalPopen]] = None

    def run(self):
        while not self.stopped.wait(self.interval):
            self.process = get_process(self.cmd, self.host)
            # Should this be in Monitor?
            # self.event_defaults['time'] = time.strftime('%c')
            assert isinstance(self.process.stdout, Iterable), 'Invalid stdout stream for monitoring: {}'.format(type(self.process.stdout))
            for line in self.process.stdout:
                if self.stopped.is_set():
                    break
                if PY3 and isinstance(line, bytes):
                    line = line.decode('utf-8')
                self.on_message(line)
            if not self.stopped.is_set():
                self.on_message('{}={}\n'.format(self.EXIT_CODE, self.process.wait()))
            if self.process:
                self.process.terminate()
            self.process = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='poller')
        self.thread.daemon = True
        self.thread.start()
        super(PollingMonitor, self).start()

    def stop(self):
        self.stopped.set()
        if self.process:
            self.process.terminate()
            self.process = None
        if self.thread:
            self.thread.join()
        self.thread = None
        super(PollingMonitor, self).stop()

    def poll_all(self):
        return [None if self.thread else 0]

    def wait_all(self):
        self.stop()
        return [0]


class RelayHandler(MsgHandler): # pylint: disable=too-few-public-methods
    """
    To allow a multi-monitor to act as a monitor itself, we "handle" child monitors messages.
    """
    def __init__(self, monitor: Monitor, msg_prefix: Optional[str]) -> None:
        self.monitor = monitor
        self.msg_prefix = msg_prefix

    def handle_msg(self, msg):
        self.monitor.on_message(msg if not self.msg_prefix else (self.msg_prefix + msg))


class MultiMonitor(Monitor):
    """
    MultiMonitor acts as both a monitor itself, and a hierarchical container of monitors.
    It allows extracting the events of itself and it's children or logging all messages from the hierarchy.
    """
    # Max size of ThreadPool for this monitor. Overridden in constructor. Pass 1 for serial or 0 for unlimited.
    MAX_PARALLEL = 40

    def __init__(self, monitors: Optional[Sequence[Monitor]] = None, max_parallel: Optional[int] = None, **kwargs: Any) -> None:
        super(MultiMonitor, self).__init__(**kwargs)
        self.monitors: List[Monitor] = []
        self.relays: Dict[Monitor, MsgHandler] = {}
        self.max_parallel = max_parallel if max_parallel is not None else self.MAX_PARALLEL
        for mon in monitors or []:
            self.add_monitor(mon)

    def add_monitor(self, mon: Optional[Monitor], msg_prefix: str = None) -> None:
        if mon is None:
            return
        self.monitors.append(mon)
        relay = RelayHandler(self, msg_prefix=msg_prefix)
        self.relays[mon] = relay
        mon.add_msg_handler(relay)

    def remove_monitor(self, mon: Monitor) -> None:
        try:
            mon.remove_msg_handler(self.relays[mon])
            del self.relays[mon]
            self.monitors.remove(mon)
        except Exception as e:
            # Not found.  Search descendants
            for m in self.monitors:
                if not isinstance(m, MultiMonitor):
                    continue
                try:
                    m.remove_monitor(mon)
                    # Should we remove empty parents? Shouldn't really make a difference
                    return
                except:
                    pass
            raise Exception(f'remove-monitor({mon}) from {self} failed: {repr(e)}.')

    def start(self):
        n_threads = min(self.max_parallel or 5000, len(self.monitors))
        self.logger.debug('multi-start(): threads: {}, sub-monitors: {}'.format(n_threads, [str(m) for m in self.monitors]))

        super(MultiMonitor, self).start()

        if n_threads:
            try:
                with ThreadPoolExecutor(n_threads) as executor:
                    list(executor.map(lambda mon: mon.start(), self.monitors))
            except Exception as e:
                self.logger.info('multi-start() done. Failed: {} - {}'.format(type(e), e))
                raise

    def stop(self):
        n_threads = min(self.max_parallel or 5000, len(self.monitors))
        self.logger.debug('multi-stop(): threads: {}, sub-monitors: {}'.format(n_threads, [str(m) for m in self.monitors]))

        fatal_failures = []
        if n_threads:
            with ThreadPoolExecutor(n_threads) as executor:
                # Don't use .map() here because we want to get all the exceptions separately
                futures = [executor.submit(mon.stop) for mon in self.monitors]
            for mon, e in zip(self.monitors, [f.exception() for f in futures]):
                # We swallow non-fatal exceptions
                if isinstance(e, self.FatalMonitorException):
                    self.logger.warning('stopping failed for {} -> {} - {}'.format(mon, type(e), e))
                    fatal_failures.append(e)
                elif isinstance(e, Exception):
                    self.logger.info('stopping failed for {} -> {} - {}'.format(mon, type(e), e))

        super(MultiMonitor, self).stop()
        self.logger.debug('multi-stop(): done. #Failures: {}'.format(len(fatal_failures)))

        if fatal_failures:
            raise self.FatalMonitorException(str(fatal_failures))

    def reset(self):
        for mon in self.monitors:
            mon.reset()
        super(MultiMonitor, self).reset()

    def ignore(self):
        for mon in self.monitors:
            mon.ignore()
        super(MultiMonitor, self).ignore()

    def unignore(self):
        for mon in self.monitors:
            mon.unignore()
        super(MultiMonitor, self).unignore()

    @property
    def events(self):
        return self._events + [self.localize_event(event) for mon in self.monitors for event in mon.events]

    def poll_all(self):
        return [item for mon in self.monitors if hasattr(mon, 'poll_all') for item in mon.poll_all()] # type: ignore[attr-defined]

    def wait_all(self):
        return [item for mon in self.monitors if hasattr(mon, 'wait_all') for item in mon.wait_all()] # type: ignore[attr-defined]

    def all_monitors(self):
        return self.monitors + [submon for mon in self.monitors if hasattr(mon, 'monitors') for submon in mon.all_monitors()] # type: ignore[attr-defined]

    def find_monitors(self, attrs):
        ''' find sub monitors whose event_defaults contain the specified attributes '''
        return [mon for mon in self.all_monitors() if attrs.items() <= mon.event_defaults.items()]

    def running_monitors(self):
        return [mon for mon in self.all_monitors() if hasattr(mon, 'process') and mon.process and mon.process.poll() is None]

    def stopped_monitors(self):
        return [mon for mon in self.all_monitors() if not mon.ignoring and hasattr(mon, 'process') and (mon.process is None or mon.process.poll() is not None)]

    def failed_stopped_monitors(self):
        return [mon for mon in self.all_monitors() if not mon.ignoring and hasattr(mon, 'process') and (mon.process is None or mon.process.poll())]


class ExitCodeHandler(MsgHandler):
    """
    Handler useful for PollingMonitor. Records an event for non-zero exit codes.
    If save_output is True, add the command output to the event.
    If no_repeats is True, only one event will be emitted for each repeated sequence of an error code.
    """
    def __init__(self, save_output=True, no_repeats=False):
        self.save_output = save_output
        self.no_repeats = no_repeats
        self.output = ''
        self.code = None

    def handle_msg(self, msg):
        if msg.startswith(PollingMonitor.EXIT_CODE):
            code = msg.partition('=')[2].strip()
            output = self.output
            self.output = ''
            if code == '0' or (self.no_repeats and code == self.code):
                self.code = code
                return None
            self.code = code
            return { 'code': code, 'output': output }
        if self.save_output:
            self.output += msg


class HostReachableMonitor(PollingMonitor):
    """
    Monitor to test reachability to a host.
    """
    def __init__(self, host):
        super(HostReachableMonitor, self).__init__('nc -vz {} 22'.format(host), 'localhost', 1)
        self.add_msg_handler(ExitCodeHandler(save_output=True, no_repeats=True))
