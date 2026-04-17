#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from typing import Iterator, get_type_hints, _GenericAlias
import sys
from typing import Any, Union, Type, TypeVar, Tuple, Set, Dict, List, Sequence, Optional, MutableMapping, MutableSequence, Callable, TYPE_CHECKING, IO
from collections import defaultdict
from functools import partial
from enum import Enum
from uuid import UUID
from jinja2 import Template
import json
import ast
import re
import time

if TYPE_CHECKING:
    from xlro.core.entities import Manager

from xlro.core import infra_conf
from xlro.core.entities.base import entity, SourceTypes, BaseEntity, PropertySpec, prop_loader, EntityRef, LoaderStatus
from xlro.core.entities.rest_info import RestEntityInfo, RestVersionManager, RestVersionInfo
from xlro.core.entities.etypes import Size
from xlro.core.sdk.ConnectionManager import ConnectionManagerError
from xlro.core.sdk.Utils import Utils, MongoObj, AttributeRepresentation
# TODO: Should go away!
from deprecated import deprecated
from xlro.core.util.consts import Deprecate
from xlro.core.util.cli_util import str2entity
from xlro.core.util.dict_util import expand_dots
from xlro.core.util.general_utils import batched_operation

# For reference only


RE = TypeVar('RE', bound='SDKEntity')
D_OR_E = TypeVar('D_OR_E', Dict[Any, Any], 'SDKEntity')
# TODO: REST - This needs re-thinking.  Who is sending a Dict and Why?!
# A) It creates confusion (REST-keyed dict vs. Infra-keyed dict)
# B) It creates overhead (see in CRUD methods that we go from dict to entity and back to dict to send to rest)

def sdk_entity(sourcetypes: List[str]) -> Callable[..., Type[RE]]:
    """ Same as entity wrapper, but casts return to SDKEntity instead of BaseEntity """

    def sdk_cast(cls: Type[RE]) -> Type[RE]:
        e_class: Any = entity(sourcetypes)(cls)
        assert issubclass(e_class, SDKEntity), f'{cls.__name__} is not an SDKEntity, but was wrapped as one.'
        return e_class
    return sdk_cast

class SdkEncoder(json.JSONEncoder):
    def default(self, obj): # pylint: disable=method-hidden
        from xlro.core.entities import BaseEntity
        if isinstance(obj, SDKEntity):
            return obj.rest_id
        if isinstance(obj, BaseEntity):
            return obj.key().partition(':')[2]
        try:
            return json.JSONEncoder.default(self, obj)
        except:
            return str(obj)

class SdkObject(object):
    # A marker object for sub-objects in SDK Entities
    # They can't be proper Entity objects, because they have no id, views, etc.
    # OTOH, the spec typing is useful.  Helpful for CLI typing -maybe better set/get, later.
    from threading import Lock
    _lock = Lock()
    _specs: Optional[Dict[str, PropertySpec]] = None

    def __init__(self, obj_as_dict={}, **kwargs):
        for k, v in obj_as_dict.items():
            setattr(self, k, v)
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def _get_specs(cls) -> Dict[str, PropertySpec]:
        if cls._specs:
            return cls._specs
        with cls._lock:
            if cls._specs:
                return cls._specs

            cls._specs = {}
            # get_type_hints will resolve forward-ref types (names in quotes)
            namespace = vars(sys.modules['typing'])
            namespace.update(vars(sys.modules['xlro.core.entities']))
            namespace.update(vars(sys.modules[cls.__module__]))
            for prop, ptype in get_type_hints(cls, globalns=namespace).items():
                if prop[0] == '_':
                    continue
                if isinstance(ptype, _GenericAlias):
                    if ptype.__origin__ == list:
                        ptype = [ptype.__args__[0]]
                    elif getattr(ptype.__origin__, '_name', None) == 'Union' and ptype.__args__[1] == type(None):
                        # We're ignoring Optional here, but probably need to assess better value for "required"
                        ptype = ptype.__args__[0]
                    else:
                        raise Exception(f'Cannot determine typing for {cls.__name__}.{prop}')
                cls._specs[prop] = ptype
        return cls._specs

    def _to_dict(self):
        return {k:getattr(self, k, None) for k in self._get_specs() if getattr(self, k, None) is not None}

    def __str__(self):
        return json.dumps(self._to_dict(), cls=SdkEncoder)

class SdkException(Exception):
    """an exception we create for sdk calls failures. contains optional related entity reference"""
    def __init__(self, ent, reason, *args):
        super(SdkException, self).__init__(*((reason, ) + args))
        self.ent = ent

    def __str__(self):
        return "{}, {}".format(self.ent, super(SdkException, self).__str__())

class RestException(Exception):
    """ Indicates a failure to complete the REST request """
    def __init__(self, reason, error_obj, *args):
        self.error_obj = error_obj
        super(RestException, self).__init__(*((reason, error_obj) + args))

class RequestException(Exception):
    """ Indicates an unsuccessful, but completed REST request """
    pass

class MongoComparison(object):
    class OP(Enum):
        """
        just an enum representing the query operators:
        https://docs.mongodb.com/manual/reference/operator/query-comparison/
        """
        OP_IN = 'in'
        OP_NIN = 'nin'
        OP_EQ = 'eq'
        OP_GTE = 'gte'
        OP_LT = 'lt'
        OP_LTE = 'lte'
        OP_NE = 'ne'
        OP_NOT = 'not'
        OP_RE = 'regex'

    @staticmethod
    def get_oper_query_val_dict(oper: OP, values: Any) -> Dict[str, Any]:
        return {"${}".format(oper.value): values}

    @classmethod
    def get_in_query_val(cls, values: List[str]) -> Dict[str, list]:
        return cls.get_oper_query_val_dict(cls.OP.OP_IN, values)

    @classmethod
    ## SDK: Note! dbKey could be nested: a.b.c for attr X (see MongoObj.py).  Is this just US? Need to hide DB schema in REST!
    def get_comparison(cls, key: Union[AttributeRepresentation, List[AttributeRepresentation]], op: OP, val: Union[List, str]) -> MongoObj:
        return MongoObj(key, cls.get_oper_query_val_dict(op, val))

    @staticmethod
    def update_mongo_fields(mongo_list: List[MongoObj], parent_attr: AttributeRepresentation) -> List[MongoObj]:
        """adds the 'parent_attr' prop to every MongoObj object in 'mongo_list'"""
        for mongo in mongo_list:
            mongo.field = "{parant_attr_db}.{child_fields}" \
                .format(parant_attr_db=parent_attr.dbKey, child_fields=mongo.field)
        return mongo_list

@entity([SourceTypes.MANAGEMENT])
class SDKEntity(BaseEntity):
    """ An Entity managed via Management REST interface. """
    FETCHED_TS = 'fetched_ts'
    _read_only_props = ('dateCreated', 'createdBy', 'dateModified', 'modifiedBy', 'dbUUID', 'fetched_ts', 'mgmt')
    IGNORE_OOB = False
    IS_SQL = False
    _size_props: Optional[Set] = None # Lazy, cached list of Size properties per entity for GB conversion

    def __init__(self, *args, **kwargs):
        self.base_uuid : Optional[str] = kwargs.get('uuid')
        super(SDKEntity, self).__init__(*args, **kwargs)
        # Can't cache rest_info because install could/will switch Mgmt version
        # self._rest_info: Optional[RestEntityInfo] = None
        # self._is_top_level: Optional[bool] = None

    # TODO: mgmt_id might be simpler, cleaner, more serializable.  Could be str(UUID) or str(Manager), i.e., endpoints
    mgmt : 'Manager' = PropertySpec('Manager', key=True)

    @classmethod
    def create_instance(cls: Type[RE], **properties: Any) -> RE:
        ''' combined instance() + create() required for objects with mgr assigned UUID as key '''
        from uuid import uuid4
        from xlro.core.entities.manager import Manager
        assert 'uuid' in cls._xlro_keyprops, 'create_instance() only relevant for entities with UUID key'
        # Create a temp instance with a fake uuid
        mgmt = properties.setdefault('mgmt', Manager.get_manager())
        assert mgmt, f'create_instance() - Entity requires mgmt property'
        tmp_instance = cls.instance(source=None, uuid=uuid4(), **properties)
        res_dict = cls._create_or_update(mgmt, 'save', entities = [tmp_instance])[0]
        tmp_instance.remove()
        return cls.instance(source=None, uuid=res_dict['_id'], **properties)

    #####
    # Basic Entity overrides
    #####

    @classmethod
    def _genkey(cls, source: Optional[str] = None, kwargs: Optional[dict] = None) -> str:
        """ Ensure default manager object, if needed for key """
        from xlro.core.entities import Manager
        if kwargs and 'mgmt' in cls._xlro_keyprops:
            kwargs.setdefault('mgmt', Manager.get_manager())
        return super(SDKEntity, cls)._genkey(source, kwargs)

    @prop_loader(SourceTypes.MANAGEMENT, None)
    def load_from_mgmt_sdk(self):
        """ Default management loader for all fields. """
        # NOTE: Loaders can be in REST terms because map_props() will be called on results
        # WARNING: This has been greatly simplified from original _get_key_queries() based on the assumption we have
        # a single, known rest_id per restful object type.

        if not self.rest_info.route:
            # A sub-entity not supporting direct mgmt discovery, e.g., Chunk.  Could load REST root, but that's not yet supported
            return {}

        # Get one result by key
        result = next(self._sdk_get(mgmt=self.mgmt, count=1,
                                     filter_mongo_objs=[MongoObj(self.rest_info.dbkey, self.rest_id)]), None)
        if not result:
            # Not found is legit.  Might not have been created yet.
            return {}

        self._set_mgmt(result, self.mgmt)
        return result

    #####
    # REST Entity utilities
    #####
    @property
    def rest_info(self) -> RestEntityInfo:
        """ Property to lazily load the REST definitions for this class and version of mgmt """
        # We don't cache because of the potential for installing Management :-(
        # version_info() caches calculations, so it should just be a lookup
        return RestVersionManager.version_info(self.rest_version).entities[self.__class__.__name__]

    @classmethod
    def cls_rest_info(cls, mgr) -> RestEntityInfo:
        from xlro.core.entities import Manager
        if not mgr:
            mgr = Manager.get_manager()
        try:
            mversion = mgr.api_version
        except:
            mversion = RestVersionManager.default_version()
            cls.logger.info(f'Could not get REST Version of {mgr}.  Defaulting to {mversion}')
        return RestVersionManager.version_info(mversion).entities[cls.__name__]

    @property
    def is_top_level(self) -> bool:
        """ Decide if this is a top-level REST object, i.e., has an independent CRUD route. """
        # Same caching problem as rest_info :-(
        # TODO: Really should have a super-class RestStruct and sub-class RestObject?
        rest_info = self.rest_info
        return bool(rest_info.route)

    @property
    def rest_id(self):
        rest_info = self.rest_info
        dbkey = rest_info.dbkey
        assert dbkey, f'No dbkey defined for {self.__class__.__name__}'
        iprop = rest_info.rest2infra.get(dbkey, dbkey)
        if iprop in self._xlro_props:
            return self.get_property(iprop)
        # A major kludge, but "name" and "_id" are same in Volume.  Need to understand how this worked before
        return self.get_property(self._xlro_keyprops[0])


    @property
    def rest_version(self) -> str:
        try:
            return self.mgmt.api_version
        except Exception as e:
            self.logger.warning(f'Cannot determine mgmt version of {self}')
            raise

    def rest_feature(self, feature):
        return RestVersionManager.version_info(self.rest_version).features.get(feature, None)

    ###
    # CRUD Operations and support
    ###
    @classmethod
    def _set_mgmt(cls, obj, mgmt):
        """ Add mgmt entry to all dicts, recursively """
        if isinstance(obj, dict):
            obj['mgmt'] = mgmt
            for o in list(obj.values()):
                cls._set_mgmt(o, mgmt)
            for o in list(obj.keys()):
                cls._set_mgmt(o, mgmt)
        elif isinstance(obj, list):
            list(map(lambda o: cls._set_mgmt(o, mgmt), obj))

    @classmethod
    def _unset_mgmt(cls, obj: Any) -> Any:
        """ Strip mgmt from rest dicts, recursively """
        if isinstance(obj, MutableMapping):
            return {k: cls._unset_mgmt(v) for k, v in obj.items() if k != 'mgmt'}
        elif isinstance(obj, MutableSequence):
            return [cls._unset_mgmt(item) for item in obj]
        return obj

    def _do_op(self, check_success=True):
        import inspect
        caller = inspect.stack()[1].frame
        op = caller.f_code.co_name
        kwargs = caller.f_locals
        kwargs.pop('self', None)
        result = self.do_operation(self.mgmt, op, [self], **kwargs)
        if check_success:
            try:
                assert result[0]['success'], f'OP: {self.__class__.__name__}.{op}({kwargs}) failed.  result: {result}'
            except Exception as e:
                raise Exception(f'OP: {self.__class__.__name__}.{op} failed or invalid response: {e}, result: {result}')
        return result

    @classmethod
    def _do_operation(cls: Type[RE], mgmt: 'Manager', op: str, entities: Optional[Sequence[Union[str, dict, RE]]] = None, timeout=None, **kwargs) -> List[Dict]:
        """ Invoke a REST Operation """
        rest_info = cls.cls_rest_info(mgmt)
        try:
            op_info = rest_info.kwargs['ops'][op]
            route = op_info.get('route', op)
        except:
            raise Exception(f'No op defined for {cls.__name__}/{op}.')

        # Generate payload based on template
        op_args = expand_dots(kwargs)
        style = op_info.get('style', 'none')
        ents_for_template: List = []
        if style == 'keys':
            op_keys: List[str] = []
            for e in entities or []:
                if isinstance(e, str):
                    # Don't use raw string e, since the Entity may manipulate it
                    e = str2entity(e, cls)
                if isinstance(e, SDKEntity):
                    op_keys.append(e.rest_id)
                elif isinstance(e, dict):
                    op_keys.append(cls.from_dict(e).rest_id)
            ents_for_template = op_keys
        elif style in ['one', 'entities']:
            ents_for_template = cls.get_objs(entities or [], mgmt)
        else:
            ents_for_template = list(entities) if entities else []
        payload = None
        payload_template = op_info.get('payload')
        if payload_template:
            payload_txt = None
            try:
                payload_txt = Template(payload_template).render(entities=ents_for_template, kwargs=kwargs, **kwargs)
                payload = ast.literal_eval(payload_txt)
            except Exception as e:
                cls.logger.info(f'Failed to render/eval payload for {cls.__name__} /{route}. Template: {payload_template}, Payload: {payload_txt}')
                raise Exception(f'Failed to render/eval payload for {cls.__name__} /{route}. {repr(e)}')
        err, out = cls._makePost(mgmt, [route], payload, timeout=timeout)
        if err:
            raise RestException(f'{cls.__name__}/{op} failed. Error: {err}.', err)

        # delete and create are sometimes an "operation", mostly because of inconsistent payloads :-(
        if op in ('delete', 'snapshot-detach-and-delete'):
            cls.post_delete(mgmt, [e.rest_id if isinstance(e, SDKEntity) else e for e in ents_for_template], out)
        elif op in ('create', 'snapshot-create-and-attach'):
            cls.post_create(mgmt, [e if isinstance(e, SDKEntity) else str2entity(e, cls) for e in ents_for_template], out)
        return out


    @classmethod
    def do_operation(cls: Type[RE], mgmt: 'Manager', op: str, entities: Optional[Sequence[Union[str, dict, RE]]] = None, timeout=None, **kwargs) -> List[Dict]:
        """ Invoke a REST Operation """
        if not entities:
            # "Batching" won't work (e.g., user change-password is implicit on logged user.)
            return cls._do_operation(mgmt, op, [], timeout, **kwargs)

        op_info = cls.cls_rest_info(mgmt).kwargs['ops'][op]
        batch_op = op_info.get('batch_op', {})
        batch_arg = batch_op.get('key') or 'entities'
        batch_size = batch_op.get('size') or infra_conf.root.general.max_rest_batch_size
        fast_fail = batch_op.get('fast_fail') or infra_conf.root.general.fast_fail_rest_operations

        all_info = op_info.get('all')
        if all_info and kwargs.get('all'):
            from xlro.core.entities import Volume
            # a special arg for implying all references of an entity.  really only works for attached volumes... :-(
            assert len(entities) == 1, '--all only supported on single object'
            param, propname = tuple(all_info.items())[0]
            obj = cls.get_objs(entities, mgmt)[0]
            all_refs = obj.get_property(propname, SourceTypes.MANAGEMENT, no_cache=True)
            # This next line is where the "genericness" falls apart :-(  It's really designed for Attached Volumes...
            # For example, all_refs.keys() assumes a Dict property, name= assumes an SDKEntity with name, etc.
            # TODO: maybe try to figure this out.  In the case of attach/detach it makes sense.  I can't think of other
            # examples, and I don't want to invest in trying to genericize this right now.
            kwargs[param] = tuple(Volume.instance(name=e) for e in all_refs.keys())

        return batched_operation(batch_arg, cls._do_operation, batch_size=batch_size, fast_fail=fast_fail, mgmt=mgmt, op=op, entities=entities, timeout=timeout, **kwargs)

    @classmethod
    def bulk_get_property(cls, prop: str, source: str, entities: Optional[List[BaseEntity]],
                          no_cache: Optional[bool] = False, fastfail: bool = False) -> Set['BaseEntity']:
        ''' Get property in parallel to allow scaling vs. serial get_property() on large set of entities
            NOTE: Since we get entities by keys, not all will be found.  So we return a list of found entities
        '''
        # Some cases we can't/shouldn't handle this way - non.Mgmt and special loaders (which maybe shouldn't exist for Mgmt?)
        if source != SourceTypes.MANAGEMENT or prop in cls._xlro_loaders[SourceTypes.MANAGEMENT]:
            if source == SourceTypes.MANAGEMENT:
                cls.logger.info(f'Cannot use SDKEntity.bulk_get_property({prop}) because of private loader.')
            return super().bulk_get_property(prop, source, entities, no_cache, fastfail)

        #TODO: Would be nice to handle entities=None, no_cache=False without going to MGMT, but requires an ALL cache
        found = set()

        # I can't honestly see a multi-mgmt request, but to be safe - but still not multi-thread!
        for mgmt, entities in cls._group_by_mgmt(entities).items():
            rest_info = cls.cls_rest_info(mgmt)
            if entities:
                dbkey = rest_info.dbkey
                keys = [e.rest_id for e in entities]
                filter_objs = cls._get_filter(entities[0].mgmt, **{dbkey: keys})
            else:
                keys = []
                filter_objs = None

            projection = None
            # If no default projection, no point in special request for prop - it's either there or it's not
            # Projection == Nested is the same as no projection
            default_projection = rest_info.projection
            nested = rest_info.kwargs.get('nested')
            if default_projection and (len(default_projection) > 1 or default_projection[0].field != nested):
                p2p = rest_info.kwargs.get('prop2proj', {}) # Some props are calculated based on others
                needed_fields = {p for p in p2p.get(prop, rest_info.infra2rest.get(prop, prop)).split(',')}
                if '*' in p2p:
                    needed_fields.update(p2p['*'].split(','))
                proj_fields = {mo.field for mo in default_projection}
                if default_projection[0].value:
                    # It's a positive projection - add prop if needed
                    projection = default_projection[:]
                    for rest_prop in (needed_fields-proj_fields):
                        projection += [MongoObj(rest_prop, 1)]
                else:
                    # It's a negative projection - remove props if needed
                    projection = [mo for mo in default_projection if mo.field not in needed_fields]

            # TODO: I guess it would be nice to enable fast-fail variant of sdk_get() for ids...
            results = cls.sdk_get(filter_mongo_objs=filter_objs, projection_mongo_objs=projection, mgmt=mgmt)
            if fastfail and keys and len(results) < len(keys):
                missing = set(keys) - set([e.rest_id for e in results])
                raise Exception(f'Objects not found: {list(missing)}')
            found.update(results)
        return found

    @classmethod
    def _makeRequest(cls, method: str, mgr: 'Manager', routes, payload=None, timeout=None) -> Tuple[Dict, List[Dict]]:
        if not mgr.connection:
            raise ConnectionManagerError(f'Cannot connect to Manager at {mgr.host}')
        e_info = RestVersionManager.version_info(mgr.api_version).entities[cls.__name__]
        assert e_info.route, f'No route for {cls.__name__}!'
        route = Utils.createRouteString(routes=routes, endPointRoute=e_info.route)
        if method == 'post':
            err, out = mgr.connection.post(route, cls._unset_mgmt(payload), timeout=timeout)
        else:
            err, out = mgr.connection.get(route, timeout=timeout)
        # REST can return a single dict vs. array.  Not clear when/why.
        return (err, out if isinstance(out, list) else [out])

    @classmethod
    def _makePost(cls, mgr: 'Manager', routes, payload, timeout=None) -> Tuple[Dict, List[Dict]]:
        try:
            json.dumps(payload) # Trigger an early exception if not serializeable
        except:
            cls.logger.warning(f'REST payload: {payload} is not serializable to JSON')
            raise Exception('Failed to generate REST payload')
        return cls._makeRequest('post', mgr, routes, payload, timeout)

    @classmethod
    def _makeGet(cls, mgr: 'Manager', routes) -> Tuple[Dict, List[Dict]]:
        return cls._makeRequest('get', mgr, routes)

    @classmethod
    def sdk_get(cls: Type[RE],
                page: Optional[float] = 0,
                count: Optional[int] = 0,
                sort_mongo_objs: Optional[List[MongoObj]] = None,
                filter_mongo_objs: Optional[List[MongoObj]] = None,
                projection_mongo_objs: Optional[List[MongoObj]] = None,
                mgmt: Optional['Manager'] = None, routes: Optional[List[str]] = None) -> List[RE]:
        from xlro.core.entities import Manager

        m = mgmt or Manager.get_manager()
        # TODO: MAYBE this should also be a generator, but not for phase 1.
        # a) Too many consumers need a full list anyway, so we save nothing
        # b) Many consumers assume caching, so we'd need to cache the "generator"
        entities = [cls._rdict_to_entity(d, m) for d in cls._sdk_get(page=page, count=count,
            sort_mongo_objs=sort_mongo_objs, filter_mongo_objs=filter_mongo_objs,
            projection_mongo_objs=projection_mongo_objs, mgmt=m, routes=routes)]
        if not projection_mongo_objs:
            # This might be called in bulk (from manager) or otherwise.
            # To avoid re-calling load_from_mgmt_sdk() on each again, mark the loader as passed
            # if we didn't project fields
            for e in entities:
                e._loaders_called[SourceTypes.MANAGEMENT][SDKEntity.load_from_mgmt_sdk] = LoaderStatus.PASSED
        return entities

    @classmethod
    def _sdk_get(cls: Type[RE],
                page: Optional[float] = None,
                count: Optional[int] = 0,
                sort_mongo_objs: Optional[List[MongoObj]] = None,
                filter_mongo_objs: Optional[List[MongoObj]] = None,
                projection_mongo_objs: Optional[List[MongoObj]] = None,
                mgmt: Optional['Manager'] = None, routes: Optional[List[str]] = None,
                ignore_nested=False) -> Iterator[Dict]:
        """ Lowest level GET returning REST dictionaries as a generator. filter/sort/project must currently be in terms of REST keys """
        from xlro.core.entities import Manager

        rest_info = cls.cls_rest_info(mgmt)
        nested = rest_info.kwargs.get('nested')
        if projection_mongo_objs and projection_mongo_objs[0].value == 1:
            # alway add UUID if a positive projection
            projection_mongo_objs.append(MongoObj('uuid', 1))
        if not ignore_nested and nested:
            # Prepend nested prefix to all mongo_obj field names
            for mo_list in (sort_mongo_objs, filter_mongo_objs, projection_mongo_objs):
                for mo in mo_list or []:
                    mo.field = f'{nested}.{mo.field}'
            # Select subdict "nested" from all results
            for result in cls._sdk_get(page=page, count=count, sort_mongo_objs=sort_mongo_objs,
                    filter_mongo_objs=filter_mongo_objs, projection_mongo_objs=projection_mongo_objs,
                    mgmt=mgmt, routes=routes, ignore_nested=True):
                yield result.get(nested, result)
            return

        cls.logger.debug(f'_sdk_get routes={routes}, page={page}, count={count}')
        # Build query string for REST request
        if projection_mongo_objs is None:
            projection_mongo_objs = rest_info.projection
        if cls.IS_SQL:
            cls.logger.debug(f'SQL FILTER: {", ".join([str(o) for o in filter_mongo_objs or []])}, SORT: {", ".join([str(o) for o in sort_mongo_objs or []])}, PROJ: {", ".join([str(o) for o in projection_mongo_objs or []])}')
            # Not all filters supported
            for f in filter_mongo_objs or []:
                if isinstance(f.value, dict):
                    k, v = next(iter(f.value.items()))
                    ops = MongoComparison.OP
                    assert k[1:] in (ops.OP_EQ.value, ops.OP_IN.value, ops.OP_RE.value), f'Unsupported op: {k} for SQL object "{cls.__name__}"'
                elif isinstance(f.value, list):
                    raise Exception('Unknown filter type {f} for SQL object "{cls.__name__}"')

            # Ignore sort/proj for SQL for now
            if sort_mongo_objs or projection_mongo_objs:
                cls.logger.debug(f'SQL Object {cls.__name__}.  Ignoring SORT: {", ".join([str(o) for o in sort_mongo_objs or []])}, PROJECTION: {", ".join([str(o) for o in projection_mongo_objs or []])}')
                sort_mongo_objs = None
                projection_mongo_objs = None

        query = Utils.buildQueryStr({'filter': filter_mongo_objs, 'sort': sort_mongo_objs, 'projection': projection_mongo_objs})
        full_routes = routes[:] if routes else ['all']

        # Generator-based fetching
        current_page = page or 0
        page_size = count or infra_conf.root.general.max_rest_query_size

        while True:
            # Non-paging cases, just do one request:
            # 1) page and count is None - no paging support from mgmt on this route
            # 2) page is None - do not paginate after all/0/0
            if page is None and count is None:
                current_route = full_routes + [query]
            else:
                current_route = full_routes + [f'{current_page}', f'{page_size}{query}']

            cls.logger.debug(f'_sdk_get page routes={current_route}')
            err, out = cls._makeGet(mgmt or Manager.get_manager(), current_route)
            fetch_time = time.time_ns()

            if err:
                cls.logger.info(f'GET {current_route}, ERR: {err}')
                raise RequestException(err)

            if not out or out == [None]:
                break

            if not isinstance(out, list):
                cls.logger.warning(f'Unexpected result: ({type(out)}) - {out}')
                out = [out]

            for r in out:
                r[cls.FETCHED_TS] = fetch_time
                yield r

            # Break conditions:
            # 1) Non-paging case: always break after first request
            # 2) Paging case: break if we got fewer results than requested
            if page is None or count or len(out) < page_size:
                break

            current_page += 1

    @classmethod
    def get_headlines(cls: Type[RE], extra_props: List[str] = [], mgmt: Optional['Manager'] = None,
            query: Optional[dict]=None, **kwargs: Any) -> Dict[str, RE]:
        """ Lighter weight fetch, returning key-props and optional extra props, but not full, deep objects """
        from xlro.core.entities import Manager
        if not mgmt:
            mgmt = Manager.get_manager()
        rest_info = RestVersionManager.version_info(mgmt.api_version).entities[cls.__name__]
        props = cls._xlro_keyprops + extra_props
        projection = [MongoObj(field=rest_info.infra2rest.get(prop, prop), value=1) for prop in props]
        selection = kwargs.pop('filter_mongo_objs', [])
        if query:
            selection.extend(cls._get_filter(mgmt, **query))
        return {e.key(): e for e in cls.sdk_get(mgmt=mgmt, projection_mongo_objs=projection, filter_mongo_objs=selection or None, **kwargs)}

    @classmethod
    def get_filtered(cls: Type[RE], page: float = 0, count: int = 0, mgmt: Optional['Manager'] = None, **kwargs: Any) -> List[RE]:
        """calls sdk_get with '$in' filters requested by **kwargs"""
        sort_obj = kwargs.pop('sort_obj', None)
        return cls.sdk_get(page, count, mgmt=mgmt, filter_mongo_objs=cls._get_filter(mgmt, **kwargs), sort_mongo_objs=sort_obj)

    @classmethod
    def _get_filter(cls: Type[RE], mgmt: Optional['Manager'] = None, **kwargs: Any) -> List[MongoObj]:
        """ Convert kwargs into a Mongo filter list """
        rest_info = cls.cls_rest_info(mgmt)

        mfilter = []
        for prop, values in kwargs.items():
            rprop = rest_info.infra2rest.get(prop, prop)
            iprop = rest_info.rest2infra.get(prop, prop)
            # cast value if needed (for SQL objects where key is int)
            spec = cls._property_spec(iprop)
            if spec and spec.ptype == int:
                try:
                    if isinstance(values, list):
                        values = [spec.ptype(v) for v in values]
                    elif not isinstance(values, dict): # TODO: If dict, should we recursively case?
                        values = spec.ptype(values)
                except Exception as e:
                    cls.logger.info(f'Failed to cast "{iprop}" = ({type(values)}){values} to {spec.ptype}')
            if isinstance(values, str):
                mfilter.append(MongoObj(rprop, MongoComparison.get_oper_query_val_dict(MongoComparison.OP.OP_RE if any(c in values for c in Utils.RE_CHARS) else MongoComparison.OP.OP_EQ, values)))
            elif isinstance(values, (int, float)):
                mfilter.append(MongoObj(rprop, values))
            elif isinstance(values, dict):
                for op, vals in values.items():
                    in_dict_val = MongoComparison.get_oper_query_val_dict(MongoComparison.OP(op), vals)
                    mfilter.append(MongoObj(rprop, in_dict_val))
            elif isinstance(values, list):
                in_dict_val = MongoComparison.get_in_query_val(values)
                mfilter.append(MongoObj(rprop, in_dict_val))
            else:
                raise Exception(f'Unsupported query option: {prop}=({type(values)}){values}')
        return mfilter

    @classmethod
    def _rdict_to_entity(cls: Type[RE], d: Dict, mgr: 'Manager') -> RE:
        """ Convert a REST dict to an Entity """
        cls._set_mgmt(d, mgr)
        # Map names immediately, so everyone downstream uses only infra names
        return cls.from_dict(d, SourceTypes.MANAGEMENT)

    #####
    # Infra conversion to REST dicts (nee foreign objects)
    #
    # Hopefully, this can be simplified, but is required backwards compatibility with various specific overrides
    # Preserved names for now...
    # TODO: refactor once REST working
    ####

    def to_foreign_sdk_entity(self, local_only: bool=False) -> Dict:
        """ Convert object _WITH_ defaults and local changes, to REST dictionary for create/update. """
        self.logger.info(f'TO-REST: {self} (local? {local_only}')

        d = {}
        if local_only:
            d[self.rest_info.infrakey] = self.rest_id
            d['_id'] = self.rest_id
            try:
                d['uuid'] = self.get_property('uuid')
            except:
                pass
        else:
            # TODO: We're going to have problems if there are defaults for Non-REST fields...
            d = self.get_properties(source_type=SourceTypes.DEFAULT)
            # In case of update, _create_or_update pre-fetched the object
            d.update(self.get_properties(source_type=SourceTypes.MANAGEMENT))
        # In create OR update, LOCAL values can be set
        # NOTE: There's an assumption that no one would set a field not accepted in REST, but server will enforce
        d.update(self.get_properties(source_type=SourceTypes.LOCAL))

        # Delete always RO and internal fields.
        # TODO: move to rest.yaml
        for readonly in SDKEntity._read_only_props:
            d.pop(readonly, None)

        # Now, convert keys and values
        rest_info = self.rest_info
        i2r = rest_info.infra2rest
        return {i2r.get(iprop, iprop): self.foreign_sdk_type_conversion(d.get(iprop)) for iprop in d if iprop != 'mgmt'}

    def foreign_sdk_type_conversion(self, value: Any, attr: str = 'no-attr') -> Any:
        """recursively checks whether to change the value or not, and change if needed """
        from xlro.core.entities.etypes import KeyValue
        if isinstance(value, BaseEntity):
            if isinstance(value, SDKEntity):
                return value.rest_id if value.is_top_level else value.to_foreign_sdk_entity()
            elif len(value._xlro_keyprops) == 1:
                return getattr(value, value._xlro_keyprops[0])
            raise TypeError("failed transforming value to foreign entity", self, attr, type(value), value)
        elif isinstance(value, Size) and 'useGB' in RestVersionManager.version_info(self.rest_version).features:
            return (-1 if value < 0 else value/(1000**3))
        elif isinstance(value, (str, int, bool, float)) or value is None:
            return value

        elif isinstance(value, UUID):
            return str(value)
        elif isinstance(value, MutableSequence):
            return [self.foreign_sdk_type_conversion(item, f'{attr}[]') for item in value]
        elif isinstance(value, KeyValue):
            return {key:  self.foreign_sdk_type_conversion(val, f'KV: {attr}[{key}]') for key, val in value.items() if val != None}
        elif isinstance(value, MutableMapping):
            return {key:  self.foreign_sdk_type_conversion(val, f'MAP: {attr}[{key}]') for key, val in value.items()}
        elif isinstance(value, SdkObject):
            return self.foreign_sdk_type_conversion(value._to_dict())
        raise Exception(f'Cannot convert value of type: {type(value)} ({value})')

    def get_self(self, optional=False, projection_mongo_objs=None) -> bool:
        """ Fetch self, which will refresh all values from management (side effect of cls.instance() in fetch) """
        results = self.sdk_get(mgmt=self.mgmt, count=1,
                filter_mongo_objs=[MongoObj(self.rest_info.dbkey, self.rest_id)], projection_mongo_objs=projection_mongo_objs)
        if not results and not optional:
            raise RequestException(f'sdk_get() of {self} failed.')
        return bool(results)

    @classmethod
    def get_by_key(cls: Type[RE], mgr: 'Manager', dbkey, key) -> Optional[RE]:
        """ Fetch objects by ID """
        results = cls.sdk_get(mgmt=mgr, count=1, filter_mongo_objs=[MongoObj(dbkey, key)])
        return results[0] if len(results) == 1 else None

    @classmethod
    def _get_by_keys(cls: Type[RE], mgr: 'Manager', dbkey, keys: List[str]) -> List[RE]:
        ''' Per batch query by key.  See get_by_keys() '''
        return cls.sdk_get(mgmt=mgr, filter_mongo_objs=cls._get_filter(mgr, **{dbkey: keys}))

    @classmethod
    def get_by_keys(cls: Type[RE], mgr: 'Manager', objs: List[RE | dict | str], optional=True) -> List[RE]:
        """ Although get_sdk() is batched, the query for IDs could blow up.  So this is safer. """
        keys = cls.get_ids(objs, mgr)
        dbkey = cls.cls_rest_info(mgr).dbkey
        assert dbkey, f'No dbkey for {cls.__name__}'
        results = batched_operation('keys', cls._get_by_keys, batch_size=300, mgr=mgr, dbkey=dbkey, keys=keys)
        if not optional and len(results) < len(keys):
            not_found = set(keys) - set(cls.get_ids(results, mgr))
            raise Exception(f'Keys not found: {", ".join(not_found)}')
        return results

    @staticmethod
    def _err2exc(err_out_tuple):
        (err, out) = err_out_tuple
        if err:
            raise RestException(err, err)
        return out

    @classmethod
    def count(cls, manager=None):
        from xlro.core.entities import Manager
        return cls._err2exc(cls._makeGet(manager or Manager.get_manager(), ['count']))[0]

    def create(self):
        objs, results = self.create_or_update(self.mgmt, 'save', [self])
        if results[0].get('success'):
            self.clear_uuid_change()
            return self
        raise SdkException(ent=self, reason=results[0].get('error', f'Unknown error. ({results[0]})'))

    def update(self):
        objs, results = self.create_or_update(self.mgmt, 'update', [self])
        if results[0].get('success'):
            return self
        raise SdkException(ent=self, reason=results[0].get('error', f'Unknown error. ({results[0]})'))

    def delete(self):
        return self.bulk_delete([self])

    def set_property(self, prop: str, value: Any, source: Optional[str] = None) -> 'BaseEntity': # pylint: disable=too-many-branches
        ''' Override set_property to allow UUID tracking '''
        if prop == 'uuid' and not self.base_uuid:
            self.base_uuid = str(value)
        return super().set_property(prop, value, source)

    @classmethod
    def any_uuid_changed(cls, entities: List[RE]):
        if not cls.has_prop('uuid') or cls.IGNORE_OOB:
            return []
        # If no base_uuid, nothing to check for changes
        have_base_uuid = [e for e in entities if e.base_uuid]
        changed = []
        for mgr, ents in cls._group_by_mgmt(entities).items():
            changed.extend([e for e in cls.get_by_keys(mgr, ents) if e.base_uuid and str(e.uuid) != str(e.base_uuid)])
        return changed

    def is_uuid_changed(self, no_cache=False):
        try:
            return not self.IGNORE_OOB and self.base_uuid is not None and str(self.get_property('uuid', no_cache=no_cache)) != str(self.base_uuid)
        except:
            # No UUID or no entity yet
            return False

    def clear_uuid_change(self):
        try:
            self.base_uuid = self.get_property('uuid')
        except:
            pass

    @classmethod
    def post_create(cls, mgr: 'Manager', objs: List[RE], results: List[Dict]):
        """ Method called after create, to allow updating container - e.g., create-volume might update manager.volumes """
        return

    @classmethod
    def post_delete(cls, mgr: 'Manager', ids: List[str], results: List[Dict]):
        """ Method called after delete, to allow updating container - e.g., delete-volume might update manager.volumes """
        for res in results:
            try:
                if res['success']:
                    cls.clear_entity(str2entity(res.get('_id', res.get('id')), cls))
            except Exception as e:
                cls.logger.info(f'post-delete error {repr(e)} on {res}')
        return

    @classmethod
    def _group_by_mgmt(cls, objs: Sequence[D_OR_E]) -> Dict['Manager', List[D_OR_E]]:
        """ Group entities by manager, for bulk methods which MAY get groups from different managers """
        ret: Dict['Manager', List[D_OR_E]] = defaultdict(list)
        for o in objs:
            if isinstance(o, dict):
                ret[o['mgmt']].append(o)
            elif isinstance(o, SDKEntity):
                ret[o.mgmt].append(o)
        return ret

    ####
    # REST To/From INFRA
    ####
    @classmethod
    def map_props(cls, propmap, source_type=None):
        """ Convert management dict to infra-compatible dict by renaming.  Type conversions handled in base-entity.set_property() """
        if source_type in [SourceTypes.MANAGEMENT, SourceTypes.LOCAL]:
            mgmt = propmap.get('mgmt')
            if not mgmt:
                from xlro.core.entities import Manager
                # cls.logger.debug(f'propmap({cls.__name__}, {source_type}) called without "mgmt"!')
                mgmt = propmap['mgmt'] = Manager.get_manager()
            if isinstance(mgmt, (str, EntityRef)):
                propmap['mgmt'] = BaseEntity.instance_from_key(str(mgmt))
            elif isinstance(mgmt, dict):
                propmap['mgmt'] = Manager.from_dict(mgmt)
            try:
                rest_ver_info = RestVersionManager.version_info(propmap['mgmt'].api_version)
                rest_info = rest_ver_info.entities[cls.__name__]
            except Exception as e:
                cls.logger.warning(f'Missing or invalid mgmt {propmap.get("mgmt", None)} in propmap of {cls.__name__}: {repr(e)}')
                raise
            if source_type == SourceTypes.MANAGEMENT:
                propmap.setdefault('description', '')
                for rest_prop, infra_prop in rest_info.rest2infra.items():
                    if rest_prop in propmap:
                        propmap[infra_prop] = propmap.pop(rest_prop)

                if 'useGB' in rest_ver_info.features:
                    if cls._size_props is None:
                        cls._size_props = set()
                        for p in cls._xlro_props:
                            spec = cls._property_spec(p)
                            if spec and spec.ptype == Size:
                                cls._size_props.add(p)
                        cls.logger.debug(f'Initialized size-props for {cls} to {cls._size_props}')

                    for p, v in propmap.items():
                        if p in cls._size_props and type(v) != Size:
                            propmap[p] = Size(v * (1000**3))

        return super(SDKEntity, cls).map_props(propmap, source_type)

    @staticmethod
    def results_list(entities: Sequence[D_OR_E], sdk_results: List[dict]) -> List[Union[D_OR_E, Exception]]:
        """append ent if method succeed, otherwise append relevant SdkException"""
        return [ent if res['success'] else SdkException(ent=ent, reason=res['error'])
                for res, ent in zip(sdk_results, entities)]

    @classmethod
    def _bulk_create_or_update(cls, entities: Sequence[D_OR_E], op: str, **kwargs: Any) -> Tuple[List['SDKEntity'], List[Dict]]:
        all_objs, all_results = [], []
        for mgmt, ents in cls._group_by_mgmt(entities).items():
            sdk_objs, sdk_results = cls.create_or_update(mgmt, op, ents, **kwargs)
            all_objs += sdk_objs
            all_results += sdk_results
        return all_objs, all_results

    @classmethod
    def bulk_create(cls, entities: Sequence[D_OR_E], **kwargs: Any) -> List[Union['SDKEntity', Exception]]:
        """new version of the 'create_many' with interface change and validation of 'success' status"""
        return cls.results_list(*cls._bulk_create_or_update(entities=entities, op='save', **kwargs))

    @classmethod
    def bulk_update(cls, entities: Sequence[D_OR_E], **kwargs) -> List[Union['SDKEntity', Exception]]:
        """new version of the 'update_many' with interface change and validation of 'success' status"""
        return cls.results_list(*cls._bulk_create_or_update(entities=entities, op='update', **kwargs))

    @classmethod
    def bulk_delete(cls, entities: Sequence[D_OR_E], **kwargs: Any) -> List[Union[Dict, Exception]]:
        """new version of the 'delete_many' with interface change and validation of 'success' status"""
        all_results: List[Dict] = []
        for mgmt, ents in cls._group_by_mgmt(entities).items():
            all_results += cls._delete_many(ents, mgmt)
        return cls.results_list(all_results, all_results)

    @classmethod
    def _delete_payload(cls: Type[RE], entities: Sequence[Union[str, dict, RE]], mgmt: 'Manager') -> List[Dict]:
        """ Delete payload was inconsistent - usually, [{"_id": key},...], but allow class override """
        v_info: RestVersionInfo = RestVersionManager.version_info(mgmt.api_version)
        if v_info.features.get('use_uuids', False) and cls.has_prop('uuid'):
            payload = []
            rest_info = cls.cls_rest_info(mgmt)
            for obj in cls.get_objs(entities, mgmt, fields=[rest_info.rest2infra.get(rest_info.dbkey, rest_info.dbkey), '_id', 'uuid']):
                try:
                    uuid = str(obj.uuid)
                except Exception as e:
                    cls.logger.debug(f'Failed to get uuid for {obj} - {repr(e)}')
                    # PROBABLY the volume already deleted.  But even so, we need to make sure the result list
                    # matches the request list, so we can't just skip it.
                    # This will result in not-found, which is legit.
                    uuid = '00000000-0000-0000-0000-000000000000'
                payload.append({'_id': obj.rest_id, 'uuid': uuid})
            return payload
        else:
            return [{'_id': key} for key in cls.get_ids(entities, mgmt)]

    @classmethod
    def get_ids(cls: Type[RE], entities: Sequence[Union[str, dict, RE]], mgmt: 'Manager') -> List[str]:
        rest_info: Optional[RestEntityInfo] = None
        ids: List[str] = []
        for ent in entities:
            if isinstance(ent, cls):
                ids.append(ent.rest_id)
            elif isinstance(ent, dict):
                if not rest_info:
                    rest_info = cls.cls_rest_info(mgmt)
                try:
                    ids.append(ent[rest_info.dbkey])
                except:
                    ids.append(ent[rest_info.rest2infra[rest_info.dbkey]])
            else:
                ids.append(str(ent))
        return ids

    @classmethod
    def get_objs(cls: Type[RE], entities: Sequence[Union[str, dict, RE]], mgmt: 'Manager', key_ok=True, fields: Optional[List[str]] = None) -> List[RE]:
        ''' convert a list of names/dicts/objects to objects '''
        objs: List[RE] = []
        for ent in entities or []:
            try:
                if isinstance(ent, cls):
                    objs.append(ent)
                elif isinstance(ent, dict):
                    if fields:
                        ent = {k:ent[k] for k in ent if k in fields}
                    objs.append(cls.from_dict(ent))
                else:
                    assert key_ok, 'Arg must be dict or entity'
                    objs.append(str2entity(str(ent), cls))
            except Exception as e:
                cls.logger.info(f'Failed to convert {ent} to an Entity Object.  {repr(e)}')
                raise
        return objs

    @classmethod
    def _delete_default(cls: Type[RE], entities: Sequence[Union[str, dict, RE]], mgmt: 'Manager') -> List[Dict]:
        payload = cls._delete_payload(entities, mgmt)
        cls.logger.debug(f'Deleting {payload}')
        sdk_result = cls._makePost(mgmt, ['delete'], payload)
        return cls._err2exc(sdk_result)

    @classmethod
    def _delete_many(cls: Type[RE], entities: Sequence[Union[str, dict, RE]], mgmt: 'Manager') -> List[Dict]:
        """
        temporary method to serve both 'bulk_delete' 'delete_many' (deprecated)
        which are very similar but differ in their interfaces
        """
        rest_info = RestVersionManager.version_info(mgmt.api_version).entities[cls.__name__]
        assert rest_info and rest_info.route and rest_info.dbkey, f'{cls.__name__} cannot support REST without route/dbkey.'

        if len(entities) == 0:
            cls.logger.debug("{}.delete_many received an empty entities list".format(cls))
            return []

        entity_keys = cls.get_ids(entities, mgmt)

        try:
            result = cls.do_operation(mgmt, 'delete', entities)
        except Exception as e:
            cls.logger.debug(f'Custom delete operation for {cls.__name__} failed or undefined - {repr(e)}')
            result = batched_operation('entities', cls._delete_default, entities=entities, mgmt=mgmt)

        cls.post_delete(mgmt, entity_keys, result)
        return result

    @classmethod
    def _create_or_update(cls: Type[RE], mgmt: 'Manager', route: str, entities: List[RE], **kwargs: Any) -> List[Dict]:
        """
        union method for both create and updating entities though the SDK layer
        have 3 steps:
        1. convert the given objects into SDKEntities
        2. call the correlative SDK method (create / update)
        3. reset the given entities's loaders cache (because the methods are likely to change the props values)

        returns - the operated entities list

        * temporary - support 'is_validate' flag which determines
        * the method relation to 'success' field in the MGMT server's result
        """
        api_info = RestVersionManager.version_info(mgmt.api_version)
        rest_info = api_info.entities[cls.__name__]
        assert rest_info and rest_info.route and rest_info.dbkey, f'{cls.__name__} cannot support REST without route/dbkey.'

        cls.logger.debug("{}-{} (entities) {}".format(cls.__name__, route, list(map(str, entities))))

        # Mini-update only requires updated fields (with id and uuid)
        partial_update = False
        if route == 'update':
            # REST behavior is inconsistent
            if api_info.features.get('partial-update') and cls.__name__ == 'Volume':
                partial_update = True
            if api_info.features.get('has-defaults') and cls.__name__ == 'VPG':
                partial_update = True

        if route == 'update':
            # For updates, we need to pre-load the entities, pre-partial needs fields and partial needs uuid
            # Note this means we ignore out-of-band changes
            id_set = set([e.rest_id for e in entities])
            in_filter = MongoComparison.get_in_query_val(list(id_set))
            res = cls.sdk_get(mgmt=mgmt, filter_mongo_objs=[MongoObj(rest_info.dbkey, in_filter)])
            missing = id_set - set([o.rest_id for o in res])
            if missing:
                raise Exception(f"{cls.__name__} {', '.join(list(missing))} not found")
        else:
            # For create, there's a possibility we are re-creating something that once existed (e.g., reusing name)
            # So clear anything we ever learned from non-Local sources
            for e in entities:
                cls.clear_entity(e, sources=[SourceTypes.MANAGEMENT, SourceTypes.PROC])

        sdk_dicts = [e.to_foreign_sdk_entity(local_only=partial_update) for e in entities]
        # Always clear UUID, if any, on create.  It might be left over
        if route == 'save':
            for d in sdk_dicts:
                d.pop('uuid', None)
        elif 'extend' in rest_info.kwargs.get('ops', {}):
            # This is a kludge.  But much simpler than putting the kludge in rest.yaml
            # For Volume and VPG which support /extend, don't send capacity to update because REST is VERY sensitive
            for d in sdk_dicts:
                d.pop('capacity', None)

        if False:
            # Testing filter in to_foreign_sdk_entity...

            # Only send key, uuid and local updates
            # TODO: Should be filter to to_foriegn_sdk_entity, but too big a change, especially until this is tested
            for d, e in zip(sdk_dicts, entities):
                cls.logger.debug(f'UPDATE: {e} - {list(e.get_properties(SourceTypes.LOCAL))}')

            try:
                create_params = rest_info.kwargs['ops']['create']['params']
                # YAML 1.2 removed support for extending lists, so they're nested and must be flattened! UGH!
                flattened = [i for l in create_params for i in (l if isinstance(l, list) else [l])]
                create_only = [rest_info.infra2rest.get(p, p) for p in flattened]
                cls.logger.debug(f'Screening create-only fields on update: {create_only}')
                for d in sdk_dicts:
                    for p in create_only:
                        d.pop(p, None)
            except Exception as e:
                cls.logger.debug(f'No Screening: {repr(e)}')
                create_only = None

        sdk_result = cls._makePost(mgmt, [route], sdk_dicts)

        # TODO - do the below in more solid way - (for just some entities or sources, for other methods as well)
        #  also relates to the 'pending mode'
        # resetting all given entities's loaders cache - for all sources
        for ent in entities:
            ent.reset_loaders_called_cache()
        results = cls._err2exc(sdk_result)

        only_successful = []
        for i, result in enumerate(results[:len(entities)]):
            if result['success']:
                only_successful.append(entities[i])
            else:
                cls.logger.info('{cls}.{method}({obj}) failed. Error: {err}'.format(
                    cls=cls.__name__, method=route, obj=entities[i], err=result.get("error", "No message")))

        if only_successful:
            # We refetch after create/update, e.g., volume-size might not be as we specified, but as mgmt decided.
            cls._refetch_changes(only_successful, rest_info, mgmt)
            list(map(cls.clear_local_source, entities))

        if route == 'save':
            cls.post_create(mgmt, entities, results)
        return results

    @classmethod
    def create_or_update(cls: Type[RE], mgmt: 'Manager', route: str, objects: Sequence[Union[RE, Dict]], **kwargs: Any) -> Tuple[List[RE], List[Dict]]:
        if len(objects) == 0:
            cls.logger.debug("{}.create_or_update ({}) received an empty objects list".format(cls, route))
            return [], []

        # JW: This was a tricky typing side-effect. On return, entities that were Dict will be SDKEntity. Changing to explicit.
        entities: List[RE] = cls.get_objs(objects, mgmt, key_ok=False)
        results = batched_operation('entities', cls._create_or_update, mgmt=mgmt, route=route, entities=entities, **kwargs)
        return entities, results

    @classmethod
    def _refetch_changes(cls, entities, rest_info, mgmt):
        # TODO: Maybe a flag per object whether this is needed?
        in_filter = MongoComparison.get_in_query_val([e.rest_id for e in entities])
        cls.sdk_get(mgmt=mgmt, filter_mongo_objs=[MongoObj(rest_info.dbkey, in_filter)])

    @classmethod
    def clear_local_source(cls, e):
        cls.clear_entity(e, [SourceTypes.LOCAL])

    @classmethod
    def clear_entity(cls, e, sources: Optional[List[str]] = None):
        e.base_uuid = None
        super().clear_entity(e, sources)

    @classmethod
    @deprecated(Deprecate.ToBeReplaced(bulk_create.__func__))   # type: ignore[attr-defined] ### need to do something about @deprecated...
    def create_many(cls, entities: Sequence[D_OR_E]) -> List[Dict]:
        return cls._bulk_create_or_update(entities=entities, op='save')[1]

    @classmethod
    @deprecated(Deprecate.ToBeReplaced(bulk_update.__func__))   # type: ignore[attr-defined] ### need to do something about @deprecated...
    def update_many(cls, entities: Sequence[D_OR_E]) -> List[Dict]:
        return cls._bulk_create_or_update(entities=entities, op='update')[1]

    @classmethod
    @deprecated(Deprecate.ToBeReplaced(bulk_delete.__func__))   # type: ignore[attr-defined] ### need to do something about @deprecated...
    def delete_many(cls, entities: Sequence['SDKEntity']) -> List[Dict]:
        sdk_results: List[Dict] = []
        for mgmt, ents in cls._group_by_mgmt(entities).items():
            sdk_results += cls._delete_many(ents, mgmt)
        return sdk_results

class UnknownMeta(type):
    def __getattr__(cls, key):
        raise Exception("{} Not supported in current nvmesh sdk version".format(cls.__name__))


class UnknownEntity(object): # Should be ABCMeta?
    def __getattr__(self, item):
        raise Exception("{} Not supported in current nvmesh sdk version".format(self.__class__.__name__))

    def __call__(self):
        raise Exception("{} Not supported in current nvmesh sdk version".format(self.__class__.__name__))
