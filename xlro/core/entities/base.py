#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import map
from builtins import next
from builtins import str
from builtins import object
import tempfile
from weakref import WeakValueDictionary
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED, FIRST_EXCEPTION

from xlro.core.util.general_utils import IDAdapter
from xlro.core.util.dict_util import expand_dots, merge_dicts
from xlro.core.util.frozen import FrozenList, FrozenDict

__all__ = ['BaseEntity', 'NamedEntity', 'UUIDEntity', 'SourceTypes', 'PropertySpec', 'PropertySpecT', 'prop_loader',
        'entity', 'entity_method', 'set_entity_logging_ctx', 'E', 'LoaderStatus', 'Property']
from threading import RLock
import collections
from uuid import UUID
import logging
import json
import typing
from typing import Optional, List, Callable, Union, Iterable, Any, Tuple, Dict, Mapping, TypeVar, Type, Sequence, Set
from functools import partial
from enum import EnumMeta, Enum
import copy
import threading
import traceback
import json
import os
import pickle
from itertools import count

from xlro.core import infra_conf

from collections.abc import MutableMapping, MutableSequence
from inspect import getfullargspec as getargspec # type: ignore[attr-defined]

# pylint: disable=protected-access

###
# OPEN ISSUES:
#  - property/descriptor? advantages to seeing entity.prop before it's fetched.
#  - default fetch? should entity.prop throw Exception until fetched or auto-fetch by default_source?
#  - what about introspection?
#  - single loader? _get_<prop>_<source> requires typing string value of source
###

# I actually don't like Enum because there are ever-increasing possibilities. We should have some
# standards, but enable Entity-specific, e.g., FIRST/LAST for GPT
class SourceTypes(object): # pylint: disable=too-few-public-methods
    LOCAL = 'LOCAL' # We could use this to save local changes to an entity and then save?
    SNAPSHOT = 'SNAPSHOT'
    OS = 'OS'
    MANAGEMENT = 'MANAGEMENT'
    PROC = 'PROC'
    NETLINK = 'NETLINK'
    MRSP_CONFIG_FILE = 'MRSP_CONFIG_FILE'
    MRSP_RPC = 'MRSP_RPC'
    MRSP_RPC_CONTROLLER1 = 'MRSP_RPC1'
    MRSP_RPC_CONTROLLER2 = 'MRSP_RPC2'
    BINARY_TRACE = 'BINARY_TRACE'
    DEFAULT = "DEFAULT"



class LoaderStatus(Enum):
    CALLED = 'CALLED'
    PASSED = 'PASSED'
    FAILED = 'FAILED'


def prop_loader(stypes_or_s: Optional[Union[str, Iterable[str]]] = None, prop_names: Optional[Iterable[str]] = None) -> Callable[..., Callable[..., Dict[str, Any]]]:
    """ method decorator to declare method as providing values for a given source for a set of properties.
        Example: @prop_loader(SourceTypes.PROC, ['id', 'foo', 'bar'])"""
    def annotate(func):
        if not stypes_or_s or isinstance(stypes_or_s, Sequence) and not isinstance(stypes_or_s, str):
            assert 'source' in getargspec(func).args, "non-src & multi-src loaders must have 'source' argument"
        func._loads = (stypes_or_s, prop_names)
        return func
    return annotate

# TODO: enable type-hinting for editors/linters
PropertySpecT = typing.NamedTuple('PropertySpecT',
        [('propid', int), ('ptype', Union[Type, str, Dict, List]), ('required', bool), ('default', Any), ('key', bool), ('transient', bool), ('readonly', bool)])
PropertySpecT.__new__.__defaults__ = (None,) * len(PropertySpecT._fields) # type: ignore # https://github.com/python/mypy/issues/1923


P = TypeVar('P')
def typePropertySpec(ptype: Type[P], required: bool = False, default: Any = None, key: bool = False, transient: bool = False, readonly: bool = False, propid: int = None) -> P:
    return PropertySpecT(propid, ptype, required, default, key, transient, readonly) # type: ignore

def seqPropertySpec(ptype: Sequence[Type[P]], required: bool = False, default: Any = None, key: bool = False, transient: bool = False, readonly: bool = False, propid: int = None) -> P:
    return PropertySpecT(propid, ptype, required, default, key, transient, readonly) # type: ignore

def mapPropertySpec(ptype: Mapping[str, Type[P]], required: bool = False, default: Any = None, key: bool = False, transient: bool = False, readonly: bool = False, propid: int = None) -> Mapping[str, P]:
    return PropertySpecT(propid, ptype, required, default, key, transient, readonly) # type: ignore

_prop_counter = count()
def PropertySpec(ptype=None, required=False, default=None, key=False, transient=False, readonly=False):
    propid = next(_prop_counter)
    if not ptype or isinstance(ptype, type):
        return typePropertySpec(ptype, required, default, key, transient, readonly, propid)
    elif isinstance(ptype, Mapping):
        return mapPropertySpec(ptype, required, default, key, transient, readonly, propid)
    elif isinstance(ptype, Sequence):
        return seqPropertySpec(ptype, required, default, key, transient, readonly, propid)
    raise Exception('Unrecognized PropertyType ptype: {}'.format(ptype))


class Property(object):
    # Variation on Python's "property" descriptor to support our PropertySpec
    def __init__(self, name, spec):
        self.name = name
        self.spec = spec

    def __get__(self, instance, owner):
        if not instance:
            return self
        return instance.get_property(self.name)

    def __set__(self, instance, value):
        return instance.set_property(self.name, value)


def remove_duplicates(seq):
    seen: set = set()
    seen_add = seen.add
    return [o for o in seq if not (o in seen or seen_add(o))]


E = TypeVar('E', bound='BaseEntity')

def entity(sourcetypes: List[str]) -> Callable[..., Type[E]]:
    """ Class decorator for Entity classes. Builds loaders map and registers entity by name. """
    T = TypeVar('T', bound='BaseEntity')

    def entity_wrapper(cls: Type[T]) -> Type[T]:

        # Swap PropertySpec for Property and add to _xlro_props
        all_props = dict(cls._xlro_props) # Prevent clobbering inherited _xlro_props.
        # for prop, spec in filter(lambda p: isinstance(p[1], PropertySpec), cls.__dict__.iteritems()):
        for prop, spec in [item for item in cls.__dict__.items() if isinstance(item[1], PropertySpecT)]:
            all_props[prop] = spec
            setattr(cls, prop, Property(prop, spec))
        cls._xlro_props = all_props

        # Build defaults
        cls._xlro_defaults = {key: spec.default for key, spec in cls._xlro_props.items() if spec.default is not None}

        # Build keyprops from _xlro_props, unless explitly there. _xlro_keyprops does not inherit, so can be used to override
        if '_xlro_keyprops' not in cls.__dict__:
            cls._xlro_keyprops = [prop for prop, spec in sorted(cls._xlro_props.items(), key=lambda kv: kv[1]) if spec.key]

        stypes = list(sourcetypes)
        # iterate mro to take sourcetypes of all parents except BaseEntity and Object
        for c in cls.__mro__[:-2]:
            stypes.extend(getattr(c, '_xlro_sourcetypes', []))
        stypes = remove_duplicates(stypes)

        # assuring first sourcetype is always SourceTypes.LOCAL
        try:
            stypes.remove(SourceTypes.LOCAL)
        except ValueError:
            pass

        try:
            stypes.remove(SourceTypes.DEFAULT)
        except ValueError:
            pass

        cls._xlro_sourcetypes = [SourceTypes.LOCAL] + stypes + [SourceTypes.DEFAULT]

        # Loaders are stored as nested dict: loaders[<source>][<prop>] = list(loader_methods)
        loaders: Dict[str, Dict[str, List[Callable]]] = collections.defaultdict(dict)
        # TODO: handle or warn on conflicting loaders
        for loader in [loader for loader in list(cls.__dict__.values()) if callable(loader) and hasattr(loader, '_loads')]:
            stypes_or_s, props = loader._loads
            if stypes_or_s and isinstance(stypes_or_s, str):
                # Old-style loaders give one source
                sources = [str(stypes_or_s)]
                is_src_loader = False
            else:
                # New-style loaders take source as loader argument
                sources = stypes_or_s or cls._xlro_sourcetypes
                is_src_loader = True

            for source in sources:
                src_loaders = loaders[source]
                for p in props or ['*']:
                    src_loaders.setdefault(p, []).append(loader if not is_src_loader else partial(loader, source=source))

        # Deepcopy() doesn't work with the functions... :-(
        # cls._xlro_local_loaders = {s: {p: loaders[s][p][:] for p in loaders[s]} for s in loaders}
        cls._xlro_local_loaders = loaders

        # For ease later, merge parents loaders at the end of child's loaders
        # super(cls, cls) was wrong, and we now support Multiple Inheritance (at least for loaders)
        # We should make better use of python inheritance, but we try to control loader order...
        all_loaders: Dict[str, Dict[str, List[Callable]]] = collections.defaultdict(dict)
        for entity in cls.mro():
            try:
                entity_loaders = getattr(entity, '_xlro_local_loaders')
            except:
                continue
            for source, e_prop_loaders in entity_loaders.items():
                src_loaders = all_loaders.setdefault(source, collections.defaultdict(list))
                for p, p_loaders in e_prop_loaders.items():
                    for loadfunc in p_loaders:
                        if loadfunc not in src_loaders[p]:
                            src_loaders[p].append(loadfunc)

        cls._xlro_loaders = all_loaders

        cls.ENTITY_REGISTRY[cls.__name__] = cls
        return cls
    return entity_wrapper


class EntityCacheDict(MutableMapping):
    def __init__(self, lru_max=1000):
        # First layer is a sized limit lru cache implemented using OrderedDict
        self.lru_dict = collections.OrderedDict()
        # Second layer is as weakref dict that is using to hold all scoped entities
        self.weak_dict: WeakValueDictionary[Any, Any] = WeakValueDictionary()
        self.lru_max = lru_max
        self.lock = threading.RLock()

    def __getitem__(self, item):
        with self.lock:
            try:
                ret = self.lru_dict[item]
            except KeyError:
                ret = self.weak_dict[item]
            self._set_lru_dict(item, ret)
            return ret

    def _set_lru_dict(self, key, value):
        self.lru_dict.pop(key, None)
        self.lru_dict[key] = value

    def __setitem__(self, key, value):
        with self.lock:
            self.weak_dict[key] = value
            self._set_lru_dict(key, value)

            while len(self.lru_dict) > self.lru_max:
                self.lru_dict.popitem(last=False)

    def __delitem__(self, key):
        with self.lock:
            self.lru_dict.pop(key, None)
            self.weak_dict.pop(key)

    def __len__(self):
        return len(self.weak_dict)

    def __iter__(self):
        return iter(self.weak_dict)


@entity(sourcetypes=[SourceTypes.LOCAL])
class BaseEntity(object):
    # BaseEntity Class properties
    logger: Union[logging.Logger, IDAdapter] = logging.getLogger(__name__)
    TYPE_PROP = '_entity_type'
    REF_PROP = '_ref_id'
    EMPTY_VALUE = ''
    ENFORCE_CACHE = True
    ENTITY_REGISTRY: Dict[str, Type['BaseEntity']] = {}
    ENTITY_CACHE: EntityCacheDict = EntityCacheDict(100000)
    ENTITY_FOLDER = None
    logging_ctx = None

    # All entity class variables
    # A list of property names that make up the Entity key
    _xlro_keyprops: List[str] = []

    # A dictionary mapping property name to the PropertySpec object, which defines its type, and some flags
    _xlro_props: Dict[str, PropertySpecT] = {}

    # The value defaults
    _xlro_defaults: Dict[str, Any] = {}

    # A dictionary mapping sourcetype to a dictionary mapping property names to loader functions.
    # For example: { 'PROC' : { 'drives': [<load_drives_from_procfile method>] , ...}, ... }
    _xlro_loaders: Dict[str, Dict[str, List[Callable[..., Dict[str, Any]]]]] = {}
    _xlro_local_loaders: Dict[str, Dict[str, List[Callable[..., Dict[str, Any]]]]] = {}

    # The default set of Sources, in priority order, for loading new property values
    _xlro_sourcetypes: List[str] = []
    # filter out sourcestypes that are not in this list unless explicitly requested a sourcetype
    _xlro_limit_sourcetypes: List[str] = infra_conf.root.general.limit_sourcetypes or []

    _xlro_entities_locks: Dict[str,threading.RLock] = {}
    _base_lock: threading.Lock = threading.Lock()

    # Instance Properties

    # A per-instance cache storing which loaders have already been called.
    _loaders_called: Dict[str, Dict[Callable, LoaderStatus]] = {}

    # Entity Factory
    # TODO: we really shouldn't accept anything but key properties, and those should be immutable...
    @classmethod
    def instance(cls: Type[E], source: str = None, **kwargs: Any) -> E:
        """ handle access to ENTITY_CACHE, or constructor if needed.
            NOTE: Only key properties are used.  To ensure ALL properties from dict are applied, use from_dict()!
        """
        target = kwargs.pop(BaseEntity.TYPE_PROP, None)
        if not target:
            target = cls
        else:
            if isinstance(target, str):
                target = BaseEntity.ENTITY_REGISTRY[target]
            if not issubclass(target, cls):
                raise Exception('Target class {} is not a subclass of {}.'.format(target, cls))

        key = kwargs.pop('key', None)
        if not key:
            key = target._genkey(source, kwargs)

        try:
            obj = cls.instance_from_key(key)
        except:
            with BaseEntity._get_entity_instance_lock(key):
                try:
                    obj = cls.instance_from_key(key)
                except:
                    obj = target(default_source=source, **kwargs)

        assert isinstance(obj, cls), 'instance() must be subclass of {}'.format(cls.__name__)
        return obj

    @classmethod
    def instance_from_key(cls, key):
        try:
            return BaseEntity.ENTITY_CACHE[key]
        except:
            with BaseEntity._get_entity_instance_lock(key):
                try:
                    return BaseEntity.ENTITY_CACHE[key]
                except:
                    return cls.load_from_disk(key)

    @staticmethod
    def _get_entity_instance_lock(key: str) -> threading.RLock:
        try:
            return BaseEntity._xlro_entities_locks[key]
        except:
            with BaseEntity._base_lock:
                return BaseEntity._xlro_entities_locks.setdefault(key, threading.RLock())

    def __del__(self):
        if not hasattr(self, '_property_maps'):
            # we only save to disk objects that created successfully
            return
        try:
            self.save_to_disk()
        except Exception as e:
            self.logger.exception("fail to destruct {} - {}".format(self, repr(e)))

    def save_to_disk(self):
        if not BaseEntity.ENTITY_FOLDER:
            BaseEntity.ENTITY_FOLDER = infra_conf.root.base.entities_dump_dir or tempfile.mkdtemp(prefix="infra")
        save_dir = os.path.join(BaseEntity.ENTITY_FOLDER, self.__class__.__name__)
        if not os.path.exists(save_dir):
            os.mkdir(save_dir)
        dump_path = os.path.join(save_dir, self.key())
        with open(dump_path, 'w+') as fp:
            json.dump({'version': 1, 'data': self.to_dict(shallow=True)}, fp)
            self.logger.info("saved to disk({})".format(dump_path))

    @classmethod
    def load_from_disk(cls, key):
        if not BaseEntity.ENTITY_FOLDER:
            raise Exception("Entity disk folder still wasn't created")
        load_dir = os.path.join(BaseEntity.ENTITY_FOLDER, cls.__name__)
        with open(os.path.join(load_dir, key), 'rb') as fp:
            content = json.load(fp)
            return cls(**content['data'])

    # Entity Factory
    @classmethod
    def from_dict(cls: Type[E], properties: Dict, source: str = SourceTypes.LOCAL, clear: bool = False) -> E:
        # TODO - check why source defaulted to LOCAL rather then _default_source
        properties = expand_dots(properties)
        obj = cls.instance(source=source, **properties)
        # Clear any non-key values from the existing object.
        # Particulary important for create, where we may be re-creating something that once existed (e.g., reusing name)
        # TODO: Should we clear_entity() in post-create? I'm resistant to changing objects under someone's nose.
        if clear:
            cls.clear_entity(obj)
        obj.set_properties(properties, source)
        return obj

    @classmethod
    def remove_instance(cls, obj: 'BaseEntity') -> Optional['BaseEntity']:
        cls.logger.debug('Removing: ' + obj.key())
        return BaseEntity.ENTITY_CACHE.pop(obj.key(), None)

    def remove(self) -> Optional['BaseEntity']:
        return BaseEntity.remove_instance(self)

    @classmethod
    def _genkey(cls, source: Optional[str] = None, kwargs: Optional[dict] = None) -> str:
        k = None
        try:
            kwargs = cls.map_props(kwargs or {}, source)  # in case map_props deals with key props as well
            keyvals = [cls.__name__]
            for k in cls._xlro_keyprops:
                kvalue = kwargs.get(k, cls._xlro_defaults.get(k))  # supports defaults for key props as well
                # kvalue could be a dict or reference if circular reference in key :-( (e.g., Service->Host->Service)
                if isinstance(kvalue, Mapping):
                    kvalue = BaseEntity.instance(source, **kvalue)
                    # kvalue = BaseEntity.from_dict(kvalue, source)
                    kwargs[k] = kvalue
                elif isinstance(kvalue, str):
                    pspec = cls._property_spec(k)
                    if pspec and isinstance(pspec.ptype, type) and issubclass(pspec.ptype, BaseEntity):
                        kvalue = BaseEntity.ENTITY_CACHE[str(kvalue)[1:]]
                        kwargs[k] = kvalue
                # keyvals.append('{}={}'.format(k, kvalue)) #kvalue.key() if isinstance(kvalue, BaseEntity) else str(kvalue))
                if kvalue is None or (kvalue == '' and cls._property_spec(k).default != ''):
                    raise Exception('Value required for key property "{}" of {}.'.format(k, cls.__name__))

                # Simplify keys, since a) key-names are now sorted, and b) backwards compatibility problematic anyway
                keyvals.append(kvalue.key() if isinstance(kvalue, BaseEntity) else str(kvalue))

            return ':'.join(keyvals)
        except (KeyError, AttributeError) as e:
            # raise Exception('Class: {}, Missing: {}, Exc: {}, Props: {}'.format(cls.__name__, k, e, kwargs))
            if k:
                raise Exception('{} requires key attribute: {}'.format(cls.__name__, k))
            raise

    def hkey(self) -> str:
        ''' shorter, more human readable key '''
        if self._hkey is None:
            kvals = [self.get_property(k) for k in self._xlro_keyprops if k != "mgmt"]
            kstrs = [str(v) if not isinstance(v, BaseEntity) else (str(v).partition(':')[2]) for v in kvals]
            self._hkey = f'{self.__class__.__name__}:{";".join(kstrs)}'
        return self._hkey

    def key(self) -> str:
        """returns the cached (instance) key str"""
        if not self._key:
            raise Exception('Key was not initialized?!')  # set in __init__
        return self._key

    def __init__(self, default_source: Optional[str] = None, default_properties: Optional[dict] = None, **kwargs: Any) -> None:
        # cache status of loadfunc's executed: called, passed or failed
        self._loaders_called: Dict[str, Dict[Callable, LoaderStatus]] = collections.defaultdict(dict)
        properties = default_properties if default_properties is not None else {}
        properties.update(kwargs)
        for kprop in self._xlro_keyprops:
            if kprop not in properties:
                try:
                    properties[kprop] = self.get_default_value(kprop)
                except KeyError:
                    pass
        self.logger = IDAdapter(logging.getLogger('.'.join([self.__module__, self.__class__.__name__])))
        # strict mode determines the option to get/set not declared (PropertySpec) attrs
        self._strict = infra_conf.root.base.strict  # strict = False means all attrs are allowed
        # TODO: default_source? profile? initial values?
        ## self._default_source = default_source if default_source else self._xlro_sourcetypes[0]
        ## Not sure why we ever need to override _xlro_sourcetypes
        self._default_source = self._xlro_sourcetypes[0] if self._xlro_sourcetypes else (default_source or SourceTypes.LOCAL)

        # First, check key in cache...
        # Also, when reading nested objects, self must be in ENTITY_CACHE _before_ set_properties()
        self._key = self._genkey(default_source, properties)
        self._hkey = None
        self.rlock = self._get_entity_instance_lock(self._key)
        if self._key not in BaseEntity.ENTITY_CACHE:
            self.logger.debug('New Entity: ' + self._key)
            BaseEntity.ENTITY_CACHE[self._key] = self
        elif BaseEntity.ENFORCE_CACHE:
            raise Exception('Duplicate object {} use instance() factory method, not constructor.'.format(self._key))

        # TODO: maybe inversion is better - ST map per property?
        self._property_maps: Dict[str, Dict] = collections.OrderedDict([(st, {}) for st in self._xlro_sourcetypes])
        self._users_objects: WeakValueDictionary[Tuple[str, str], Union[FrozenDict, FrozenList]] = WeakValueDictionary()
        self.set_properties(properties, default_source)

    def load_properties(self, source: Optional[str] = None, *args: Any, **kwargs: Any) -> 'BaseEntity':
        """try loading all values from requested src (or default src)"""
        # TODO: allow a spec param to limit properties loaded
        self.logger.debug('load_properties({} [{}], {}, {})'.format(source, self._default_source, args, kwargs))
        if source is None:
            source = self._default_source
        if source not in self._property_maps:
            self._property_maps[source] = {}
        # TODO: only using defined 'loaders' will miss _get_X 'getters'...
        # Those could be implicitly added to loaders. Would also make get_property simpler.
        pm: Dict[str, Any] = {}
        for loadfunc in set([loadfunc for prop_loads in list(self._xlro_loaders[source].values()) for loadfunc in prop_loads]):
            # first updating a pm to decrease the identical calls to set_property (in case two loaders supllied the same prop)
            pm.update(loadfunc(self, *args, **kwargs))
        self.set_properties(pm, source)
        return self

    @classmethod
    def _property_spec(cls, prop: str) -> Optional[PropertySpecT]:
        """searched for the prop's PropertySpec and updates its ptype (str->BaseEntity) if exists"""
        # TODO - consider caching (constant per cls*prop) the results
        for c in cls.__mro__:
            if not issubclass(c, BaseEntity) or prop not in c._xlro_props:
                continue
            # if hasattr(c, '_xlro_props') and prop in c._xlro_props:
            # TODO: Copy to local for performance?
            spec = c._xlro_props[prop]
            # Map names to types, if needed (Happens when type references self before Class exists)
            if isinstance(spec.ptype, str):
                spec = spec._replace(ptype=BaseEntity.ENTITY_REGISTRY[spec.ptype])
                c._xlro_props[prop] = spec
            elif isinstance(spec.ptype, MutableSequence):
                if isinstance(spec.ptype[0], str):
                    spec.ptype[0] = BaseEntity.ENTITY_REGISTRY[spec.ptype[0]]
            elif isinstance(spec.ptype, Mapping):
                k, v = next(iter(spec.ptype.items()))
                if isinstance(v, str):
                    spec.ptype[k] = BaseEntity.ENTITY_REGISTRY[v]
            return spec
        return None

    @classmethod
    def has_prop(cls, prop: str) -> bool:
        return cls._property_spec(prop) != None

    def _load_from_cache(self, source, prop):
        return self._type_wrapper(source, prop, self._property_maps[source][prop])

    def _type_wrapper(self, source, prop, value):
        if source in (SourceTypes.DEFAULT, SourceTypes.LOCAL):
            # for DEFAULT and LOCAL we want to return the object itself and not a frozen data structure
            return value

        try:
            return self._users_objects[(source, prop)]
        except KeyError:
            pass

        ret_type: Optional[Callable] = None
        if isinstance(value, set):
            ret_type = frozenset
        elif isinstance(value, Sequence) and not isinstance(value, str):
            ret_type = FrozenList
        elif isinstance(value, Mapping):
            ret_type = FrozenDict

        value = self._refs_to_objs(value)
        if not ret_type:
            return value

        ret = ret_type(value)
        self._users_objects[(source, prop)] = ret
        return ret

    @classmethod
    def bulk_get_property(cls, prop: str, source: str, entities: Optional[List['BaseEntity']],
                          no_cache: Optional[bool] = False, fastfail: bool = False) -> Set['BaseEntity']:
        ''' Get property in parallel to allow scaling vs. serial get_property() on large set of entities '''
        assert entities, f'For non-SDK entities, a list of entities is required for bulk_get_property.'

        # Just use multi-threading.  SDKEntity has override to do bulk REST fetch
        get_prop = partial(cls.get_property, source=source, prop=prop, no_cache=no_cache)
        failed = set()
        with ThreadPoolExecutor(100) as executor:
            futures = {executor.submit(get_prop, e): e for e in entities}
            done, not_done = wait(futures, return_when=FIRST_EXCEPTION if fastfail else ALL_COMPLETED)
            for future in not_done:
                future.cancel()
            for future in done:
                try:
                    future.result()  # Trigger exception if it occurred
                except Exception as e:
                    if fastfail:
                        raise
                    entity = futures[future]
                    cls.logger(f'Failed to get-property {prop} for {entity}')
                    failed.add(entity)

        return set(entities) - failed

    def get_property(self, prop: str, source: Optional[str] = None, no_cache: Optional[bool] = False) -> Any:
        """ get (and cache) a property value from a given source. """
        spec = self._property_spec(prop)
        if self._strict and not spec:
            raise AttributeError(prop)
        no_cache = no_cache or (spec and spec.transient)
        # adjusting source as list
        if source:
            sources = [source]
        else:
            sources = list(self._property_maps.keys()) if not self._strict else self._xlro_sourcetypes

        if self._xlro_limit_sourcetypes and not source:
            for source in sources:
                if source not in self._xlro_limit_sourcetypes:
                    sources.remove(source)

            if not sources:
                self.logger.warning(f'No sources left for loading {prop} of {repr(self)}')

        if not no_cache:
            for source in sources:
                try:
                    return self._load_from_cache(source, prop)
                except KeyError:
                    pass

        loaders_tracebacks = []
        # We can make this a finer lock, but try this for now...
        with self.rlock:
            # Re-check cache within the lock. Someone may have loaded it in the meantime.
            if not no_cache:
                for source in sources:
                    try:
                        return self._load_from_cache(source, prop)
                    except KeyError:
                        pass

            for source in sources:
                try:
                    # pmap = self._property_maps.setdefault(source, {})
                    loaders = self._xlro_loaders[source]
                    new_pmap = {}
                    for loadfunc in loaders.get(prop, loaders.get('*', [])):
                        if not no_cache and self._loaders_called[source].get(loadfunc) in \
                                [LoaderStatus.CALLED, LoaderStatus.PASSED]:
                            continue
                        try:
                            self._loaders_called[source][loadfunc] = LoaderStatus.CALLED
                            loaded_map = loadfunc(self)
                            if not isinstance(loaded_map, Mapping):
                                raise TypeError('returned value of loader must be of type Mapping - {}'.format(loaded_map))
                            new_pmap.update(self.map_props(loaded_map, source))
                            self._loaders_called[source][loadfunc] = LoaderStatus.PASSED

                        except Exception as e:
                            self._loaders_called[source][loadfunc] = LoaderStatus.FAILED
                            self.logger.debug("loader {} failed for prop '{}' in {} with Error {}: {}".format(loadfunc.__name__, prop, self, type(e), e))
                            loaders_tracebacks.append(traceback.format_exc())
                            continue

                        if prop in new_pmap:
                            break

                    self.set_properties(new_pmap, source)
                    if prop not in new_pmap:
                        raise AttributeError(f'property {prop} of entity {repr(self)} not found in sources {sources}, maybe it has a default value')

                    return self._type_wrapper(source, prop, self._property_maps[source][prop])

                except (KeyError, AttributeError):
                    continue

        if prop in self._xlro_defaults:
            def_val = self.get_default_value(prop)
            self.set_property(prop, def_val, SourceTypes.DEFAULT)
            if isinstance(def_val, (str, int, bool)):
                # For primitives, just return without calling get_property again - transients caused recursion
                return def_val
            return self.get_property(prop, source=SourceTypes.DEFAULT)

        if loaders_tracebacks:
            self.logger.debug('\n'.join(loaders_tracebacks))
        raise AttributeError(f'property {prop} of entity {repr(self)} not found in sources {sources} and it does not have a default value')

    @classmethod
    def map_props(cls: Type[E], propmap: Dict[str, Any], source_type: Optional[str] = None) -> Dict:
        """ Overridable method to manipulate property names or derive properties from an incoming property map.
            This will be called on kwargs of __init__() and on propmap of set_properties().
            from_dict() is more internal, but maybe this could help with versioning even of self-generated map?
            Sub-classes should call super().map_props() to extend vs. replace.
        """
        return propmap

    def set_properties(self, propmap: dict, source_type: Optional[str] = None) -> 'BaseEntity':
        """ Set values from a dict for the specified source-type """
        propmap = self.map_props(propmap, source_type)
        for k, v in propmap.items():
            self.set_property(k, v, source_type)
        return self

    def _handle_refs(self, value: Any, source: str, vtype: type) -> Any:
        """properties values types manipulations - also relevant for from/to dict"""
        if isinstance(value, dict) and issubclass(vtype, BaseEntity):  # dict -> BaseEntity conversion
            if BaseEntity.REF_PROP in value:
                # prevents 'REF_PROP' from being set
                refid = value.pop(BaseEntity.REF_PROP)
                vobj = vtype.from_dict(value, source)
                value[BaseEntity.REF_PROP] = refid
            else:
                vobj = vtype.from_dict(value, source)
            return vobj
        elif isinstance(value, str):  # fetching the entity according to the value(=key)
            value = str(value)
            if value[0] == REF_PREFIX:
                self.logger.info('REF VALUE: ' + value)
                try:
                    return BaseEntity.ENTITY_CACHE[value[1:]]
                except Exception as e:
                    self.logger.info(f'REF "{value}" lookup failed: {repr(e)}')
            try:
                from xlro.core.util.cli_util import str2entity
                return str2entity(value, vtype)
            except Exception as e:
                self.logger.info(f'REF str2entity({value}, {type.__name__}) failed: {repr(e)}')
        return value

    @staticmethod
    def _gen_ref(val, src=SourceTypes.PROC):
        if isinstance(val, BaseEntity) and src not in [SourceTypes.LOCAL, SourceTypes.DEFAULT]:
            return EntityRef(val.key())
        return val

    def set_property(self, prop: str, value: Any, source: Optional[str] = None) -> 'BaseEntity': # pylint: disable=too-many-branches
        from xlro.core.entities.etypes import KeyValue
        if ':' in prop:
            prop, source = prop.split(':', 1)
        if isinstance(prop, str):
            # don't trust loaders to send strings
            prop = str(prop)
        spec = self._property_spec(prop)
        # TODO: strict?
        if self._strict and spec is None:
            raise AttributeError(prop)
        if not spec:
            setattr(self.__class__, prop, Property(prop, PropertySpec(ptype=type(value)) )) # type: ignore[call-arg] ## mypy not supporting __defaults__
        if source is None:
            source = self._default_source
        if spec is not None and source == SourceTypes.LOCAL and spec.readonly:
            raise Exception(f'Prop: {self.__class__.__name__}.{prop} is read-only')

        # TODO: should we fail without existing type?
        if source not in self._property_maps:
            self._property_maps[source] = {}
        pmap = self._property_maps[source]
        # TODO: Should the last condition require spec.nullable==True?
        if spec is None or spec.ptype is None or value is None:
            nvalue = value
        elif spec.ptype == KeyValue:
            # dict/Mapping replaces; tuples (from CLI --key=value) merge into existing
            if type(value) == KeyValue:
                nvalue = value
            elif isinstance(value, Mapping):
                nvalue = KeyValue(value)
            else:
                nvalue = pmap[prop] if prop in pmap else self.get_property(prop).copy()
                if isinstance(value, Iterable) and all([isinstance(i, (tuple, list)) and len(i) == 2 for i in value]):
                    update_map = dict(value)
                elif isinstance(value, tuple) and len(value) == 2:
                    update_map = {value[0]: value[1]}
                else:
                    raise Exception(f'Invalid type for property: {prop}. Cannot convert {value} to KeyValue.')
                expanded_map = expand_dots(update_map)
                nvalue = merge_dicts(nvalue, expanded_map)
                # Remove keys explicitly set to None (e.g. from --delete-<param>).
                # None means "delete the key" vs. "" which sets the key to empty string.
                def recursive_delete_none(obj):
                    """Recursively delete keys with value None in nested mappings."""
                    if isinstance(obj, Mapping):
                        to_delete = [k for k, v in obj.items() if v is None]
                        for k in to_delete:
                            obj.pop(k, None)
                        for v in obj.values():
                            recursive_delete_none(v)
                    elif isinstance(obj, Iterable) and not isinstance(obj, (str, bytes)):
                        for item in obj:
                            recursive_delete_none(item)
                recursive_delete_none(nvalue)
                for k in [k for k, v in expanded_map.items() if v is None]:
                    nvalue.pop(k, None)
        elif isinstance(spec.ptype, Mapping):
            # The mapping is a "prototype", so we look at the type of the first dict value
            membertype = next(iter(list(spec.ptype.values())))
            nvalue = spec.ptype.__class__()
            assert isinstance(value, Mapping), f'Invalid type for property: {prop}. Cannot convert {value} to Mapping.'
            for k, v in value.items():
                if not issubclass(membertype, str):
                    v = self._handle_refs(v, source, membertype)
                tmp_map_value = v if isinstance(v, membertype) else membertype.from_dict(v, source)
                nvalue[k] = self._gen_ref(tmp_map_value, source)
        elif isinstance(spec.ptype, Iterable) and not isinstance(spec.ptype, (str, EnumMeta)):
            membertype = next(iter(spec.ptype))
            tmp_list_value = []
            for v in value:
                if not issubclass(membertype, str):
                    v = self._handle_refs(v, source, membertype)
                if isinstance(v, membertype):
                    tmp_list_value.append(self._gen_ref(v, source))
                elif isinstance(membertype, BaseEntity):
                    v = membertype.from_dict(v, source)
                    tmp_list_value.append(self._gen_ref(v, source))
                else:
                    # exactly same pattern as 'outer' set_property - might be recursive
                    try:
                        v = membertype(v)
                        tmp_list_value.append(self._gen_ref(v, source))
                    except Exception as e:
                        raise Exception('Invalid type for property: {}. Expected {} but got {} ({}). {}'.format(
                            prop, spec.ptype, type(value), value, e))

            nvalue = spec.ptype.__class__(tmp_list_value)
        elif isinstance(spec.ptype, type) and isinstance(value, spec.ptype):
            nvalue = self._gen_ref(value, source)
        elif isinstance(spec.ptype, type) and issubclass(spec.ptype, BaseEntity):
            nvalue = self._handle_refs(value, source, spec.ptype)
        else:
            try:
                assert isinstance(spec.ptype, type), '{} is not a type'.format(spec.ptype)
                if issubclass(spec.ptype, bool):
                    # Some strings we still consider false, even if python considers them true
                    nvalue = False if str(value).lower() in ['0', 'false'] else bool(value)
                else:
                    nvalue = spec.ptype(value)
            except Exception as e:
                raise Exception('Invalid type for property: {}. Expected {} but got {} ({}). {}'.format(
                    prop, spec.ptype, type(value), value, e))
        # Make key fields immutable
        if spec and spec.key:
            for srctype, srcmap in self._property_maps.items():
                if prop in srcmap:
                    if nvalue != srcmap[prop]:
                        raise Exception('Key property {}.{} is immutable. (Attempt to change "{}" to "{}")'.format(
                            self.__class__.__name__, prop, srcmap[prop], nvalue))
        pmap[prop] = nvalue
        self._users_objects.pop((source, prop), None)
        return self

    def _refs_to_objs(self, value: Any) -> Any:
        from xlro.core.entities.etypes import KeyValue
        """ Takes a data structure of Entities references and returns the same structure with BaseEntities """
        if isinstance(value, EntityRef):
            return BaseEntity.instance_from_key(str(value))
        elif isinstance(value, Mapping):
            return value if isinstance(value, KeyValue) else {k: self._refs_to_objs(v) for k, v in value.items()}
        elif isinstance(value, tuple):
            return tuple(self._refs_to_objs(x) for x in value)
        elif isinstance(value, Iterable) and not isinstance(value, (str, EnumMeta)):
            return [self._refs_to_objs(x) for x in value]
        return value

    def get_property_values(self, prop: str, source_types: Optional[Iterable[str]] = None) -> List[Tuple[str, Any]]:
        """ Get a map of cached values for a property. This will NOT trigger fetching unset values. """
        # TODO: Is that the intended behavior?
        maps = self._property_maps
        types = source_types or list(maps.keys())
        return [(st, self._refs_to_objs(maps[st][prop])) for st in types if prop in maps[st]]

    def get_properties(self, source_type: Optional[str] = None, props: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        maps = self._property_maps
        if not props:
            sources = [source_type] if source_type else list(maps.keys())
            props = {prop for st in sources for prop in maps[st]}
        return {prop: self.get_property(prop, source_type) for prop in props}

    def __contains__(self, attr):
        # return any(lambda pm: attr in pm, self._property_maps.values())
        for pm in list(self._property_maps.values()):
            if attr in pm:
                return True
        return False

    def _prop_to_value(self, value: Any, tracker: Dict['BaseEntity', Dict[str, Any]], skip: List[str], shallow: bool = False) -> Any:
        """'walk' along value, and adjust its subvalues types. for every 'Entity' value found, calls its 'to_dict'"""
        if isinstance(value, BaseEntity):
            if shallow:
                skip += [prop for prop in value._xlro_props if prop not in value._xlro_keyprops]
                for propmap in list(self._property_maps.values()):
                    skip += [prop for prop in list(propmap.keys()) if prop not in value._xlro_keyprops]
            return value.to_dict(tracker, skip)
        # TODO - similar to 'set_property' - need to consider different types (Sequence VS Iterable, etc.)
        elif (isinstance(value, Sequence) or isinstance(value, (set, tuple))) and not isinstance(value, str):
            return [self._prop_to_value(x, tracker, skip, shallow) for x in value]
        elif isinstance(value, Mapping):
            return {k: self._prop_to_value(v, tracker, skip, shallow) for k, v in value.items()}
        elif hasattr(value, '__dict__'):
            # JW: The results are meant to be JSON-able, so get a string if it's some non-primitive object like UUID
            # Not a great test, but seems to work.
            return str(value)
        return value

    def to_dict(self, tracker: Dict['BaseEntity', Dict[str, Any]] = None, skip: List[str] = None, shallow: bool = False) -> Dict[str, Any]:
        if skip is None:
            skip = []
        result = {self.TYPE_PROP: self.__class__.__name__} if self.TYPE_PROP not in skip else {}
        if tracker is None:
            tracker = {}
        elif self in tracker:
            if BaseEntity.REF_PROP not in tracker[self]:  # to prevent infinite/unnecessary loops
                tracker[self][BaseEntity.REF_PROP] = REF_PREFIX+self.key()
            return tracker[self][BaseEntity.REF_PROP]
        tracker[self] = result
        for prop in set().union(*list(self._property_maps.values())).difference(skip):  # type: str
            vlist = self.get_property_values(prop)
            result[prop] = self._prop_to_value(vlist[0][1], tracker, skip, shallow)  # add its default value
            if len(vlist) > 1:  # means more multiple sources for this prop
                for source, value in vlist:
                    # keeps different prop's values per src (<prop>:<src> is known format in 'xlro', see set_property)
                    result['{}:{}'.format(prop, source)] = self._prop_to_value(value, tracker, skip, shallow)
        return result

    @classmethod
    def all_to_dict(cls) -> List[dict]:
        results = []
        tracker: Dict['BaseEntity', Dict[str, Any]] = {}
        for obj in BaseEntity.ENTITY_CACHE.values():
            if isinstance(obj, cls):
                results.append(obj.to_dict(tracker))
        return results

    @classmethod
    def dump_entities(cls, path: str) -> type:
        """ write all entities from cache to a file """
        with open(path, 'w') as fp:
            json.dump(BaseEntity.all_to_dict(), fp, indent=2)
        # This is really just for CLI usage.
        return cls

    @classmethod
    def load_entities(cls, path: str) -> Optional['BaseEntity']:
        """ read all entities from file to cache """
        with open(path, 'r') as fp:
            # We wipe out cache. Arguably, we could overlay, and leave existing entities, but might cause referential chaos.
            cache = BaseEntity.ENTITY_CACHE
            cache.clear()
            obj = None
            for e_dict in json.load(fp):
                if isinstance(e_dict, str):
                    continue    # Issue with all_to_dict dumping simple references
                e = BaseEntity.from_dict(properties=e_dict)
                if obj is None:
                    obj = e
        # This is really just for CLI usage.
        return obj

    def _to_string(self):
        try:
            mclass = self.ENTITY_REGISTRY['Manager']
            assert not isinstance(self, mclass)
            mgrs = list(getattr(mclass, 'uuid_to_manager').values())
            if len(mgrs) == 1:
                return self._key.replace(':' + mgrs[0]._key, '')
        except Exception as e:
            pass

        return self.key()

    def __str__(self):
        return self._to_string()

    def __repr__(self):
        return self._to_string()

    def reset_loaders_called_cache(self, sources: Optional[Iterable[str]] = None) -> None:
        if not sources:
            self._loaders_called.clear()
        else:
            list(map(lambda src: self._loaders_called.pop(src, None), sources))

    @classmethod
    def get_default_value(cls, attr: str) -> Any:
        return copy.deepcopy(cls._xlro_defaults[attr])

    def reset_property(self, prop, sources=()):
        list(map(lambda src: self._property_maps[src].pop(prop, None), sources or self._property_maps.keys()))
        self.reset_loaders_called_cache(sources)

    @classmethod
    def clear_entity(cls, e, sources: Optional[List[str]] = None):
        ''' Wipe the property cache.  Particularly important after SDK object deleted '''
        for source in sources or e._property_maps.keys():
            if source in e._property_maps:
                e._property_maps[source] = {k: v for k, v in e._property_maps[source].items() if k in e._xlro_keyprops}
        e.reset_loaders_called_cache(sources)


REF_PREFIX = '#'
class EntityRef(object):
    def __init__(self, key):
        self.key = key

    def __repr__(self):
        return self.key

    def __eq__(self, other):
        if not isinstance(other, BaseEntity) and not isinstance(other, EntityRef):
            return False
        if isinstance(other, BaseEntity):
            other = BaseEntity._gen_ref(other)
        return self.key == str(other)

    def __ne__(self, other):
        return not self.__eq__(other)


def set_entity_logging_ctx(ctx):
    BaseEntity.logging_ctx = ctx


def entity_method(func):
    def _get_entities_from_param(obj):
        ret: List[BaseEntity] = []
        if isinstance(obj, BaseEntity):
            ret += [obj]
        elif isinstance(obj, Iterable) and not isinstance(obj, str):
            for o in obj:
                ret += _get_entities_from_param(o)
        return ret

    def wrapper(entity, *args, **kwargs):
        try:
            assert BaseEntity.logging_ctx, 'No logging context found for entity {}'.format(entity)
            entities = [entity] + _get_entities_from_param(args) + _get_entities_from_param(list(kwargs.values()))

            with BaseEntity.logging_ctx({"+entities_context": [str(e) for e in entities]}):
                ret = func(entity, *args, **kwargs)
            return ret
        except Exception as e:
            if 'logger plugin' in str(e) or isinstance(e, AssertionError):
                return func(entity, *args, **kwargs)
            raise

    return wrapper


@entity(sourcetypes=[SourceTypes.LOCAL])
class NamedEntity(BaseEntity):
    name : str = PropertySpec(str, key=True)  # type: ignore[call-arg] ## mypy not supporting __defaults__


@entity(sourcetypes=[SourceTypes.LOCAL])
class UUIDEntity(BaseEntity):
    uuid : UUID = PropertySpec(UUID, key=True)  # type: ignore[call-arg] ## mypy not supporting __defaults__
