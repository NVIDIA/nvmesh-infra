# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import List, Dict, Type, Optional
from itertools import islice
from uuid import UUID

from xlro.core.entities import Manager
from xlro.core.entities.etypes import NotificationLevel, Role
from xlro.core.entities.base import SourceTypes, PropertySpec
from xlro.core.entities.sdk_base import SDKEntity, sdk_entity, RE, RequestException


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class User(SDKEntity):
    email : str = PropertySpec(str, key=True)
    uuid : UUID = PropertySpec(UUID)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    role : Role = PropertySpec(Role, default=Role('Observer'))
    notificationLevel : str = PropertySpec(NotificationLevel, default=NotificationLevel('NONE'))
    password : str = PropertySpec(str) # That seems dangerous to me
    confirmationPassword : str = PropertySpec(str) # That seems funny to me
    relogin : bool = PropertySpec(bool)
    description : str = PropertySpec(str)

    @classmethod
    def _sdk_get(cls: Type[RE], page=None, count=None, **kwargs) -> List[RE]: # type: ignore[override]
        """ No paging support! """
        start = int((page or 0) * (count or 0))
        return islice(super()._sdk_get(page=None, count=None, **kwargs), start, (start+count if count else None))  # type: ignore[misc]

    @classmethod
    def count(cls, manager=None):
        from xlro.core.entities import Manager
        result = cls._err2exc(cls._makeGet(manager or Manager.get_manager(), ['count']))
        try:
            # Legacy REST returned a dict of counts by role
            return sum([int(d['total']) for d in result])
        except:
            return int(result[0])

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super().map_props(propmap, source_type)
        if source_type == SourceTypes.LOCAL and 'password' in propmap:
            propmap.setdefault('confirmationPassword', propmap['password'])
        propmap.pop('layout', None)
        return propmap

    @classmethod
    def _refetch_changes(cls, entities, rest_info, mgmt):
        try:
            super()._refetch_changes(entities, rest_info, mgmt)
        except RequestException as e:
            # If you update your permissions and now can't get the result, do not fail operation
            if not (isinstance(e.args[0], dict) and b'Operation not permitted' in e.args[0].get('content', b'')):
                raise
