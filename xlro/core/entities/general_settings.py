# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import TypeVar, Dict, List, Type, Optional, Any, Tuple, Iterator
from xlro.core.entities import SDKEntity, SourceTypes, Manager
from xlro.core.entities.etypes import LoggingLevel
from xlro.core.entities.base import PropertySpec
from xlro.core.entities.sdk_base import sdk_entity
from xlro.core.sdk.Utils import MongoObj

RE = TypeVar('RE', bound='SDKEntity')

@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class GeneralSettings(SDKEntity):
    enableNVMf : bool = PropertySpec(bool)
    enableZones : bool = PropertySpec(bool)
    domain : str = PropertySpec(str)
    loggingLevel : LoggingLevel = PropertySpec(LoggingLevel)
    daysBeforeLogEntryExpires : int = PropertySpec(int)

    def __init__(self, *args, **kwargs):
        super(GeneralSettings, self).__init__(*args, **kwargs)

    @classmethod
    def _sdk_get(cls: Type[RE],
                page: Optional[float] = 0,
                count: Optional[int] = 0,
                sort_mongo_objs: Optional[List[MongoObj]] = None,
                filter_mongo_objs: Optional[List[MongoObj]] = None,
                projection_mongo_objs: Optional[List[MongoObj]] = None,
                mgmt: Optional[Manager] = None, routes: Optional[List[str]] = None) -> Iterator[Dict]:
        return super(GeneralSettings, cls)._sdk_get(page=None, count=None, mgmt=mgmt)  # type: ignore

    @classmethod
    def _makeGet(cls, mgr: 'Manager', routes) -> Tuple[Dict, List[Dict]]:
        return ({}, [1]) if 'count' in routes else cls._makeRequest('get', mgr, ['all'])

    @classmethod
    def _makeRequest(cls, method: str, mgr: 'Manager', routes, payload=None, timeout=None) -> Tuple[Dict, List[Dict]]:
        if method == 'post':
            payload = payload[0]
        return super()._makeRequest(method, mgr, routes, payload, timeout)

    def update_properties(self, **kwargs):
        self.set_properties(kwargs, source_type=SourceTypes.LOCAL)
        self.update()
