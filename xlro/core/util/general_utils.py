#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import absolute_import
from builtins import map
from builtins import next
from builtins import str
from builtins import range
from builtins import object
import datetime
import traceback
from itertools import count
from functools import partial
import subprocess
from math import ceil

from typing import Any, Dict, Callable,Iterable,List,Optional,Sequence,Tuple,TYPE_CHECKING
import time
import logging
from socket import gethostbyaddr, gethostbyname, gethostname
from threading import Lock, RLock
from functools import wraps
import random
from typing import Hashable, Dict, Union, ClassVar
from collections import defaultdict
import re
from xlro.core import infra_conf
settings = infra_conf.root.general
if TYPE_CHECKING:
    from xlro.core.entities import BaseEntity, Block, Attachment, Client, Volume
    from xlro.core.util.block_objects import PageData

logger = logging.getLogger(__name__)

_host_cache: Dict[str, str] = {}        # Maps any name to its primary hostname
_alias_cache: Dict[str, List[str]] = {} # Maps any hostname to its aliases
_host_by_name_lock = RLock()
IP_PATTERN = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
AZURE_NODE_SUFFIX = '.internal.cloudapp.net'

def log_cache(level = logging.DEBUG):
    import json
    logger.log(level, f'_host_cache: {json.dumps(_host_cache, indent=2)}')
    logger.log(level, f'_alias_cache: {json.dumps(_alias_cache, indent=2)}')

def run_local(cmd):
    # Don't use local_execute() because it causes loop to get_hostname()
    completed = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    logger.debug(f'run_local({cmd}) - completed: {(completed.stdout, completed.stderr, completed.returncode)}')
    return (completed.stdout, completed.stderr, completed.returncode)

def add_names(hostname: str, aliases: List[str]):
    global _host_cache, _alias_cache, _host_by_name_lock
    logger.debug(f'add_names({hostname}) => {sorted(aliases)}')
    with _host_by_name_lock:
        # Check if we've encountered these names
        name_set = set([hostname] + aliases)
        for name in name_set:
            if name in _host_cache:
                # We've already seen name, so take existing primary
                hostname = _host_cache[name]
                logger.debug(f'found existing primary {hostname} for {name}')
                break
        _alias_cache.setdefault(hostname, list(name_set))
        new_aliases = name_set - set(_alias_cache[hostname])
        if new_aliases:
            _alias_cache[hostname].extend(new_aliases)
            _alias_cache[hostname].sort()

def get_hostnames(name: str, skip_cache=False, allow_local=False) -> Tuple[str, List[str]]:
    ''' map "name" to primary hostname and aliases.  skip_cache and allow_local for testing only. '''
    global _host_cache, _alias_cache, _host_by_name_lock
    if not name:
        raise Exception('get_hostname() called without name')
    name = name.lower()
    if not skip_cache:
        try:
            return _host_cache[name], _alias_cache[_host_cache[name]]
        except Exception as e:
            pass

    aliases: List[str] = []
    hostName: str = ''
    with _host_by_name_lock:
        try:
            hostName, aliases, ipAddrs = gethostbyaddr(name)
            logger.debug(f'get_hostnames({name}) via socket: {hostName}, {aliases}')
            assert allow_local or name.startswith('localhost') or not hostName.startswith('localhost'), f'{hostName} is not valid for {name}.'
        except Exception as e:
            logger.debug(f'get_hostnames({name}) via socket failed: {repr(e)}')
            try:
                out, err, code = run_local(f'host {name}')
                assert code == 0 and out, f'code={code} err={err}'
                hostName = out.strip().partition(' ')[0]
                aliases = [name]
                logger.debug(f'get_hostnames({name}) via "host": {hostName}, {aliases}')
            except Exception as e2:
                logger.info(f'get_hostnames({name}) via "host" failed: {repr(e2)}')
                try:
                    ssh = f'ssh -o BatchMode=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o LogLevel=ERROR {name} hostname 2>/dev/null'
                    # Don't use Connection - it will come right back here :-(
                    out, err, code = run_local(ssh)
                    assert code == 0 and out, f'ssh hostname: code={code} err={err}'
                    hostName = out.strip()
                    aliases = [name]
                    logger.debug(f'get_hostnames({name}) via "ssh": {hostName}, {aliases}')
                except Exception as e3:
                    logger.debug(f'get_hostnames({name}) via "ssh" failed. {repr(e3)}')

        if name == 'localhost':
            try:
                # Artificially add local hostnames as aliases for 'localhost' - in case we didn't already
                lnames = run_local('hostname -s; hostname -f')[0].split()
            except FileNotFoundError:
                # most likely distroless container without /bin/sh
                lnames = []
            if not hostName:
                hostName = 'localhost'
                aliases = []
            aliases += lnames

        if not hostName:
            logger.info(f'get_hostnames({name}) failed.  Defaulting to {name}, [{name}]')
            hostName = name
            aliases = [name]

        if hostName.endswith(AZURE_NODE_SUFFIX):
            aliases.append(hostName)
            hostName = hostName[:-len(AZURE_NODE_SUFFIX)]

        if '.' in hostName:
            # Append the shortname to aliases by default.  But don't change hostName
            short = hostName.partition('.')[0]
            if not short.isnumeric():
                aliases.append(short)

        # Try to make sure the IP is in the aliases (mostly for Cert validation)
        ipset = set()
        for n in set([hostName] + aliases):
            try:
                ipset.add(gethostbyname(n))
            except Exception as e:
                pass
        aliases.extend(ipset)

        aliases.append(hostName)
        hostName = hostName.lower()
        aliases.append(hostName)
        aliases.append(name)
        aliases.append(name.lower())
        allnames = sorted(list(set(aliases + [name.lower() for name in aliases])))
        with _host_by_name_lock:
            # Need to check if someone already set a primary for any of these names
            primary_set = {_host_cache[n] for n in allnames if n in _host_cache}
            if primary_set:
                first_primary = sorted(primary_set)[0]
                if len(primary_set) > 1:
                    logger.info(f'CACHE WARNING: {hostName} has multiple primaries: {primary_set}! Using {first_primary}.')
                hostName = first_primary
            logger.debug(f'update cache: {name} -> {hostName} -> {allnames}')
            for n in allnames:
                _host_cache.setdefault(n, hostName)
                if _host_cache[n] != hostName:
                    logger.info(f'CACHE WARNING: {n} is {_host_cache[n]} but should be {hostName}')
            add_names(hostName, allnames)
        allnames = _alias_cache[hostName]
        logger.debug(f'get_hostnames({name}) => {hostName}, {allnames}')
        return hostName, allnames

def host_aliases(name: str) -> List[str]:
    return get_hostnames(name)[1]

def host_name(name: str) -> str:
    if name == 'localhost.localdomain': # This is a kludge for testing only
        return name
    return get_hostnames(name)[0]

# past package no longer supported
def old_div(a, b):
    """ provide 2.7 / behavior """
    import numbers
    if isinstance(a, numbers.Integral) and isinstance(b, numbers.Integral):
        return a // b
    else:
        return a / b

class WaitResult(object):

    class WaitResultException(Exception):
        pass

    def __init__(self, result, metadata="", *args):
        self.result = result
        self.metadata = metadata
        self.args = args

    def __bool__(self):
        # That means that results like [], "", None, False will consider bad.
        return bool(self.result)

    def __str__(self):
        return "{}({}, {})".format(self.__class__.__name__, self.result, self.metadata % self.args)

    def assert_result(self, prefix=None):
        if not self:
            reason = "{}: {}".format(prefix, self.metadata % self.args) if prefix else self.metadata % self.args
            raise WaitResult.WaitResultException(reason)


class StopWait(Exception):
    ''' "Exception" to fast-fail a wait-for-it before the timeout, if, for example, the condition will never come true.  '''
    pass

def wait_for_it(testfunc: Callable, poll: Union[int,float] = 1, timeout: Union[int,float] = 10, quiet: bool = True, measure_testfunc: bool = False) -> WaitResult:
    '''Delay current thread, polling testfunc() every `poll` seconds until it returns True, or raises StopWait, or `timeout` occurs

    Returns:
        True - as soon as testfunc() returns True
        False - if testfunc() raises StopWait
        False - if timeout occurs
    '''
    timeout += settings.wait_for_it_offset
    time_waited = 0.0
    res = WaitResult(False)
    while timeout == 0 or time_waited < timeout:
        try:
            if measure_testfunc:
                start_t = datetime.datetime.now()
            res = testfunc()
            if res:
                return res if isinstance(res, WaitResult) else WaitResult(True)
        except StopWait as s:
            logger.exception(s)
            logger.info(f'StopWait: failing early. {s}')
            return WaitResult(False, str(s))
        except Exception as e:
            logger.info(f'Exception while waiting: {repr(e)}.  Ignoring? {quiet}')
            if not quiet:
                raise e
        if measure_testfunc:
            time_waited += (datetime.datetime.now() - start_t).total_seconds()
        time.sleep(poll)
        time_waited += poll
    return res if isinstance(res, WaitResult) else WaitResult(False)

def wait_for_all(testfunc: Callable, items: List, **wait_args) -> WaitResult:
    '''Wait for testfunc to be true for ALL items.

    More efficient than most naive implementations, because this is NOT a loop on :func:`wait_for_it` which can
    wait up to timeout * len(items) (which is incorrect) and because we don't retest items once they become True.
    '''
    items = items[:] # Copy items, to leave original list alone
    def test_for_all():
        while items and testfunc(items[0]):
            items.pop(0)
        return not items
    # TODO: Consider passing failed item up. testfunc could do so, but WaitResult probably needs enhancement
    return wait_for_it(test_for_all, **wait_args)

def quiet_map(func, *iterables):
    ''' map() but swallow exceptions '''
    def wrap_func(*args):
        try:
            return func(*args)
        except Exception as e:
            return e
    return list(map(wrap_func, *iterables))

def wait_for_first(func: Callable, iterable: Iterable,
                    exception_ok: bool=False, max_wait: int=0, **kwargs) -> Tuple[Any, Any]:
    ''' Apply func to a set of items. Return the first response as (item, result) '''
    from xlro.core.util.terminable_thread import Thread as TThread
    from queue import Queue
    TIMER = '__TIMER__'

    def queue_result(q: Queue, func: Callable, item, **kwargs):
        try:
            result = func(item, **kwargs)
        except SystemExit:
            raise
        except Exception as e:
            logger.debug(f'Exception on item: {item} - {repr(e)}')
            result = e
        q.put((item, result))

    q = Queue()
    items = list(iterable)
    logger.debug(f'WaitForFirst: {func.__name__}({items})')
    threads = [TThread(target=queue_result, args=(q, func, i), kwargs=kwargs, daemon=True) for i in items]
    if max_wait:
        threads.append(TThread(target=queue_result, args=(q, lambda item: time.sleep(max_wait), TIMER), daemon=True))
    for t in threads:
        t.start()
    count = len(threads)
    while count:
        (item, result) = q.get()
        if item == TIMER:
            raise Exception('TIMEOUT during wait-for-first after {max_wait}s.')
        logger.debug(f'Got result: {result} via {item}')
        if exception_ok or not isinstance(result, Exception):
            # VERY strange behavior when terminating.  Some lock in the logging.  No clue.
            # quiet_map(TThread.terminate, threads)
            return (item, result)
        count -= 1
    raise Exception('All threads failed!')

def wait_for_threads(threads=None, timeout=10, stop=False):
    from xlro.core.util.terminable_thread import Thread as TThread
    from .operations import AsyncCaller

    if not threads:
        return True

    # Try to force threads to stop, if requested and they support that
    if stop:
        quiet_map(AsyncCaller.stop, [t for t in threads if isinstance(t, AsyncCaller)])
        quiet_map(TThread.terminate, [t for t in threads if isinstance(t, TThread)])

    # Don't use wait() so it's parallel, not serial
    result = wait_for_it(testfunc=lambda: not any((t.is_alive() for t in threads),), timeout=timeout, quiet=False)
    if not result:
        logger.warning('wait-for-threads() Threads still alive after {}s timeout: ({})'.format(timeout,
            ','.join([t.name for t in threads if t.is_alive()])))

    return result

def wait_for_property_values(
        objects: Sequence["BaseEntity"],
        prop_name: str,
        values: List[Any],
        source: Optional[str] = None,
        is_matching: bool = True,
        refresh_func: Optional[Callable] = None,
        non_values: Optional[List[Any]] = None,
        refresh_arg: Optional[str] = None,      # refresh_func can take a set of objects as an arg named this
        **wait_kwargs: Any) -> WaitResult:
    value_set = set(values)
    non_values = non_values or []

    # If an object is not found after a refresh, it's logical value is considered one of the "non-values".
    # For example, ['Deleted'] for Volume.status, which would mean a disappeared volume matches 'Deleted' value.
    is_true_for_nonexistent = not is_matching ^ any(non_value in value_set for non_value in non_values)
    bulk_size: int = infra_conf.root.general.max_wait_group or 50
    obj_list = list(objects)
    if not obj_list:
        logger.info('prop-wait: No objects given - returning True')
        return WaitResult(True)

    obj_offset = 0
    retry = []

    if not refresh_func:
        # It is possible this obviates the whole need for refresh_func...
        def default_refresh(entities):
            # Convention is refresh_func returns map of key to value
            results = entities[0].bulk_get_property(entities=entities, prop=prop_name, no_cache=True, source=source)
            return {e.key(): e for e in results}
        refresh_func = default_refresh
        refresh_arg = 'entities'
    elif not refresh_arg:
        orig_refresh_func = refresh_func
        refresh_func = lambda ignore='ignored-arg': orig_refresh_func()
        refresh_arg = 'ignore'

    logger.debug(f"prop-wait: waiting for #{len(obj_list)} {obj_list[0].__class__.__name__}(s) to"
                        f" {'' if is_matching else 'not '}match '{prop_name}'[{source or 'ANY'}] in {values}"
                        f" (refresh: {refresh_func}({refresh_arg}), missing-matches? {is_true_for_nonexistent})")

    def check_property_values() -> WaitResult:
        nonlocal obj_list, obj_offset, retry
        active = set()
        while True:
            # Get "bulk" by adding from new to retries
            active = retry + obj_list[obj_offset:obj_offset+(bulk_size - len(retry))]
            if not active:
                logger.debug('prop-wait: returning True (no active left)')
                return WaitResult(True)
            logger.debug(f'prop-wait: bulk {len(active)} ({len(retry)} retries).  New offset: {obj_offset}')
            wait_result = None
            try:
                results = refresh_func(**{refresh_arg: active})
            except Exception as refresh_e:
                logger.warning(f'prop-wait: returning False - refresh-failed.  {repr(refresh_e)}.')
                return WaitResult(False, f'Refresh() failed. {repr(refresh_e)}')
            obj_offset += len(active) - len(retry)
            retry.clear()
            logger.debug(f'prop-wait: got results {len(results)}/{len(active)}: {list(results)}')

            for e in active:
                try:
                    if e.key() not in results:
                        raise KeyError(f'{e} not found in refresh results.')
                    updated_value = e.get_property(prop_name, source=source)
                    if is_matching ^ (updated_value in value_set):
                        retry.append(e)
                        logger.debug(f'prop-wait: key: {e}, prop: {prop_name}, bad value: {updated_value}')
                        if wait_result is None:
                            wait_result = WaitResult(False, f'{e}: {prop_name} is "{updated_value}"')
                except (KeyError, AttributeError) as ex:
                    # logger.exception(ex)
                    logger.debug(f'prop-wait: key: {e}, missing-value. {repr(ex)}')
                    if not is_true_for_nonexistent:
                        retry.append(e)
                        if wait_result is None:
                            wait_result = WaitResult(False, f'{e}: {prop_name} missing')
            if wait_result is not None:
                # We don't fast-fail per item - we already did the fetch, so better to handle all fetched matches
                logger.debug(f'prop-wait: returning {wait_result}')
                return wait_result
            # if not failure, continue with next "bulk"

    return wait_for_it(check_property_values, **wait_kwargs)

def list_index_insert(l, i, v):
    """
    adjust the gien list and returns it
    insert the value at the index to the list,
    add null values in between if needed
    """
    l_len = len(l)
    if i < l_len:
        l[i] = v
    else:
        l += [None]*(i-l_len) + [v]

    return l


namespace2name_cache: Dict[str, Dict] = defaultdict(dict)

def gen_ent_name(cls: type, namespace: str = 'default') -> str:
    """
    generating entities names of format "test-<cls>-<random>-<counter>"
    handle a counters dict ('names_counter') which holds a counter for every (<path>&<cls>)
    every classes created with same path, will reference to a same random name prefix
    """
    cls2name_info = namespace2name_cache.setdefault(namespace, {'prefix': random.randint(10000, 99999)})
    # TODO ....
    logger.info("namespace - {} relates to random -{}".format(namespace, namespace2name_cache[namespace]['prefix']))

    name_info = cls2name_info.setdefault(cls,
                                            {'count': 0,
                                             'prefix': 'test-{}-{}'.format(cls.__name__.lower(),
                                                                           cls2name_info['prefix'])})
    name_info['count'] = int(name_info['count']) + 1
    return "{}-{}".format(name_info['prefix'], name_info['count'])


def gen_short_ent_name(*args, **kwargs):
    """
    Using the regular gen_ent_name may generate too long name, for example in target class when counter reaches 10.
    Therefore in some cases we can cut the "test-" prefix.
    """
    test_prefix = 'test-'

    name = gen_ent_name(*args, **kwargs)
    return name[name.startswith(test_prefix) and len(test_prefix):]

def convert_size_to_bytes(size_str):
    """
        By given size str with or without units return number of bytes.
        example : convert_size_to_bytes('1k') -> 1024
    """
    multipliers = {'b': 1, 'k': 1024, 'm': 1024**2, 'g': 1024**3, 't': 1024**4, 'p': 1024**5}
    size_str = size_str.lower().strip()
    if len(size_str):
        suffix = size_str[-1]
        if suffix.isdigit():
            return int(size_str)
        if suffix in multipliers:
            return int(float(size_str[0:-1]) * multipliers[size_str[-1]])
        raise Exception("{} is not valid suffix.".format(suffix))
    raise Exception("can't convert empty string")

def tolerant_func(retries=3, delay=1, log_level='info'):
    def tolerant_decorator(func):
        def inner_func(*args, **kwargs):
            msg = ""
            for retry in range(retries):
                try:
                    if retry:
                        time.sleep(delay)
                    return func(*args, **kwargs)
                except Exception as e:
                    msg = str(e)
                    getattr(logger, log_level)(msg + ", retry={}".format(retry))

            raise Exception(msg + " after {} retries".format(retries))
        return inner_func
    return tolerant_decorator


class IDAdapter(logging.LoggerAdapter):
    """
    logger Adapter which assist to defer between important objects
    """

    _id = count()

    def __init__(self, logger, extra=None):
        self.extra: dict = {}
        extra = extra or {}
        extra.setdefault('id', next(self._id))
        # small hack for ELK to make logger_name be actually the adapter name
        extra['logger_name'] = ".".join((logger.name, str(extra['id'])))
        super(IDAdapter, self).__init__(logger, extra)

    def process(self, msg, kwargs):
        new_extra = {}
        if kwargs:
            new_extra = kwargs.pop('extra', {})
        ret_extra = self.extra.copy()
        ret_extra.update(new_extra)
        ret = {'extra': ret_extra}
        ret.update(kwargs)
        return msg, ret

    def warn(self, msg, *args, **kwargs):
        return super(IDAdapter, self).warning(msg, *args, **kwargs)

    def notice(self, msg, *args, **kwargs):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.notice(msg, *args, **kwargs)  # type: ignore


def copy_blocks(blocks: Iterable[Union['Block', 'PageData']], local_data_path: str = 'copy.data', local_metadata_path: str = 'copy.meta') -> Tuple[str, str]:
    with open(local_data_path, 'wb') as df:
        with open(local_metadata_path, 'wb') as mf:
            for blk in blocks:
                df.write(blk.data)
                mf.write(blk.metadata)
    return local_data_path, local_metadata_path


def get_cookie():
    return int(time.time() * 10 ** 9) & (2 ** 64 - 1)


def dmsg_slice_dict(volume: 'Volume', v_addr: int, *args, **kwargs) -> Dict:
    ''' This USED to call proc on attached client, but now uses internal Infra calculations.
        Interface left alone, for backwards compatibility
    '''
    from xlro.core.entities import Volume
    from xlro.tools.nvmesh_edit.util import role2rolename
    vlba = Volume.LBA(v_addr)
    chunk, clba = volume.get_clba(vlba)
    praid, plba = chunk.get_plba(clba)
    pslice = praid.get_pslice(plba, vlba)
    index = v_addr % praid.dataDisks
    return [
        {
            'role': f'{role2rolename(n, praid)}{"[*]" if n == index else ""}',
            'node_name': p.drive.target.name.partition('.')[0],
            'drive_name': p.drive.name,
            'd_addr': hex(p.dlba_range.lbs.addr)[2:]
        } for n, p in enumerate(pslice.slice_content)
    ]


def dmsg_mtv_info(volume, v_addr, client, max_tries=10):
    raise Exception('MTV/ELECT no longer supported')


def get_debug_di(client, vol):
    try:
        return Attachment.instance(client=client, volume=vol).debug_di
    except Exception as e:
        logger.info("can't get debug_di status in volume {} in client {}".format(vol.name, client.name))
    return False

def rand_alnum(len: int =6) -> str:
    """ Lowest collision, random, alphanumeric string """
    import string, secrets

    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for i in range(len))

_gn_lock = Lock()
_gn_curr_test: Optional[str] = None
_gn_curr_id = rand_alnum()
_gn_test_count = count()
_gn_counters : Dict = defaultdict(count)

# New version of gen_ent_name() and gen_short_ent_name().
# 1. Thread-safety
# 2. base ID per test vs per prefix
# 3. mixing gen_ent and gen_short would give inconsistent naming
def gen_name(prefix: str = 'tst') -> str:
    # NOTE: Keep prefix short, since some objects have name len limits
    global _gn_lock, _gn_curr_test, _gn_curr_id, _gn_counters, _gn_test_count

    try:
        import slash
        if slash and slash.session and slash.session.results.current.test_metadata.address != _gn_curr_test:
            with _gn_lock:
                testid = slash.session.results.current.test_metadata.address
                if testid != _gn_curr_test:
                    _gn_counters = defaultdict(count)
                    _gn_curr_id = f'{_gn_curr_id[:3]}{next(_gn_test_count):03}'
                    slash.logger.debug(f'Basename for {testid} is {_gn_curr_id}')
                    _gn_curr_test = testid
    except Exception as e:
        # Not in a slash test context
        pass

    return f'{prefix}-{_gn_curr_id}-{next(_gn_counters[prefix])}'


def get_attached_volumes(clients: List['Client']) -> Dict['Client', List]:
    """
    returns dictionary of client:its attached volumes.
    the keys will be client instances
    values are list of its attached volumes
    """
    attached_vols_per_client = defaultdict(list)
    for client in clients:
        attached_vols_per_client[client] = [a.volume for a in list(client.attachments.values()) if not a.is_hidden]
    return attached_vols_per_client


def get_attached_volumes_specified(clients: List['Client'], statuses: List[str] = None, specific_vols: List['Volume'] = None) -> Dict['Client', List]:
    """
    returns client:attached_volumes dictionary with volumes attached to it that are in specified statuses (within specific_vols, if not None)
    statuses is a list of statuses like [Volume.ONLINE,Volume.DEGRADED,Volume.OFFLINE, Volume.UNAVAILABLE]
    specific_vols is a list of volumes that wanted to be in that dictionary
    """
    client_all_vol_dic = get_attached_volumes(clients)
    client_vol_with_status_dic = {}

    for client in client_all_vol_dic:
        statuses = statuses or [Volume.ONLINE, Volume.DEGRADED, Volume.UNAVAILABLE, Volume.OFFLINE]
        specific_vols = specific_vols or client_all_vol_dic[client]

        def _vol_filter(vol, statuses):
            return vol.get_property('status', no_cache=True) in statuses

        client_vol_with_status_dic[client] = list(filter(lambda vol: _vol_filter(vol, statuses), specific_vols))

    return client_vol_with_status_dic

class parentlookupdict(dict):
    def __init__(self, default, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default = default

    def __getitem__(self, key):
        from xlro.core.entities import BaseEntity

        for parent_class in BaseEntity.ENTITY_REGISTRY[key].__mro__:
            try:
                return super().__getitem__(parent_class.__name__)
            except KeyError:
                pass

        return self.default()

# Pattern to match sensitive values in CLI args and dict strings
# Handles: -p secret, --password=value, 'token': 'value', etc.
_SENSITIVE_PATTERN = re.compile(
    r"(['\"]?-{0,2}(?:-p|password|passphrase|token|secret|api-key)['\"]?\s*[:=\s]+['\"]?)([^'\"\s,}]+)",
    re.IGNORECASE
)

def mask_sensitive_value(value: str) -> str:
    """
    Mask a sensitive value, preserving some characters at margins for recognition.
    
    Args:
        value: The sensitive string to mask
    
    Returns:
        Masked string with margin characters preserved
    """
    if not value:
        return value
    margin_idx = min(len(value) // 5, 2)
    if margin_idx == 0:
        return '*' * len(value)
    return value[:margin_idx] + '*' * (len(value) - (margin_idx * 2)) + value[-margin_idx:]

def mask_hidden_fields(d: Any) -> str:
    """
    Mask sensitive values in data structures for logging. Always returns a string.
    
    Handles CLI flags (-p, --password, --token, etc.) and dict keys (password, token, etc.)
    
    Args:
        d: Data to mask - can be dict, list, tuple, str, or None
    
    Returns:
        Masked string suitable for logging
    """
    if isinstance(d, (list, tuple)):
        d = ' '.join(str(a) for a in d)
    else:
        d = str(d)
    
    return _SENSITIVE_PATTERN.sub(lambda m: m.group(1) + mask_sensitive_value(m.group(2)), d)

def _parse_js_conf(d, res):
    try:
        if d['type'] == 'Literal':
            return d['value'] if isinstance(d['value'], bool) else eval(d['raw'])
        if d['type'] == 'Identifier':
            return d['name']
        elif d['type'] == 'MemberExpression':
            return res.get(d['property']['name'], d['property']['name'])
        elif d['type'] == 'UnaryExpression':
            return eval(d['operator'] + d['argument']['raw'])
        elif d['type'] == 'BinaryExpression':
            left = _parse_js_conf(d['left'], res)
            right = _parse_js_conf(d['right'], res)
            if type(left) != type(right):
                raise TypeError(f'Unable to perform operation on different types: {type(left)} and {type(right)}')
            return left+right if d['operator'] == '+' else eval(f'{left}{d["operator"]}{right}')
        elif d['type'] == 'ExpressionStatement' and d['expression']['operator'] == '=':
            value = _parse_js_conf(d['expression']['right'], res)
            key = _parse_js_conf(d['expression']['left'], res)
            res[key] = value
        elif d['type'] == 'ArrayExpression':
            _res = []
            for e in d['elements']:
                try:
                    _res.append(_parse_js_conf(e, res))
                except TypeError:
                    continue
            return _res
        elif d['type'] == 'ObjectExpression':
            _res = {}
            for p in d['properties']:
                try:
                    value = _parse_js_conf(p['value'], res)
                    key = _parse_js_conf(p['key'], res)
                    _res[key] = value
                except TypeError:
                    continue
            return _res
        else:
            # add log here maybe, not supported
            raise TypeError(f'Unsupprted type {d["type"]}')
    except Exception as e:
        raise e

def parse_js_conf(raw: str) -> Dict[str, Any]:
    from pyjsparser import parse
    # pyjsparser is ECMA 5.1 only; template literals crash the parser.
    # Drop those lines since _parse_js_conf cannot use complex expressions like new RegExp(`...`).
    stripped = '\n'.join('' if '`' in line else line for line in raw.splitlines())
    res = {}
    for e in parse(stripped)['body']:
        try:
            _parse_js_conf(e, res)
        except TypeError:
            continue
    return res


def client_name_to_obj(cname):
    from xlro.core.entities import UmClient, Client, Node
    ret_client = Client.instance(name=cname)
    try:
        if ret_client.is_umclient:
            ret_client = UmClient.instance(name=cname, initiator=Node.instance(name=cname))
        else:
            pass # Kernel client
    except:
        logger.debug("check failed, will use kc")
    finally:
        return ret_client


def batched_operation(
        batch_arg: str,
        func: Callable,
        batch_size: int = infra_conf.root.general.max_rest_batch_size,
        fast_fail: bool = infra_conf.root.general.fast_fail_rest_operations,
        **kwargs
) -> List[Any]:
    objects = kwargs.pop(batch_arg)
    num_objs = len(objects)
    is_multi_batch = num_objs > batch_size

    result = []
    for i in range(0, num_objs, batch_size):
        if is_multi_batch:
            logger.debug(f'Performing batch #{i // batch_size}/{ceil(num_objs / batch_size)} of {func.__self__.__name__}:{func.__name__}')

        kwargs[batch_arg] = objects[i:i + batch_size]
        res = func(**kwargs)
        # JW: Removing from here.
        # * It's specific to REST functions so doesn't belong in general utility
        # * It changed the exceptions found by various operations - causing CLI and other tests to fail.
        # if fast_fail:
            # fail_ent = next((r for r in res if not r.get('success')), None)
            # assert not fail_ent, f'Stopping a fast-fail enabled operation due to - {fail_ent["error"]}'
        result.extend(res)

    return result

trace_logger = logging.getLogger('traceback')
def print_current_traceback(header: str, logger: logging.Logger = trace_logger, level: int = logging.DEBUG):
    stack = traceback.extract_stack()
    logger.log(level, header + "\n" + "".join(traceback.format_list(stack[:-1])) + "\n")

def main():
    import sys
    import argparse
    from concurrent.futures import ThreadPoolExecutor, wait
    from collections import defaultdict
    from xlro.core.util.cli_util import CLIArgumentParser, init_logging

    parser = CLIArgumentParser()
    parser.add_argument('--log-cache', action='store_true')
    parser.add_argument('hostname', nargs='+')
    args = parser.parse_args()
    init_logging(level='DEBUG')

    for h in args.hostname:
        print('***', h.upper())
        print(h.upper(), '->', get_hostnames(h))
        if args.log_cache:
            log_cache(logging.WARNING)
        print

if __name__ == '__main__':
    main()
