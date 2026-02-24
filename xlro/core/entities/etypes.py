# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import List, Optional
from collections import UserDict

from xlro.core.entities import BaseEntity
from xlro.core.sdk.Utils import Utils
from xlro.core.util.general_utils import host_name

class Size(int):
    def __new__(self, *args):
        try:
            args = (Utils.convertUnitCapacityToBytes(args[0]) if args[0] != Utils.MAX_STR else Utils.MAX_INT,) + args[1:]
        except:
            pass
        return super(Size, self).__new__(self, *args)

    @property
    def ivalue(self):
        return int(self)

    def __str__(self):
        i = int(self)
        return Utils.convertBytesToUnit(i, False) if i != Utils.MAX_INT else Utils.MAX_STR

class HostName(str):
    def __new__(self, *args):
        try:
            args = (host_name(args[0]),) + args[1:]
        except:
            from traceback import print_exc
            print_exc()
            pass
        return super(HostName, self).__new__(self, *args)

class Domain(dict):
    def __init__(self, *args):
        if not isinstance(args[0], str):
            super(Domain, self).__init__(*args)
        else:
            super(Domain, self).__init__(*args[1:])
            self['scope'], self['identifier'] = args[0].split(':', maxsplit=1)

class KeyValue(UserDict):
    logger = logging.getLogger('KeyValue')
    # A marker interface for ConfigProfile config
    def __init__(self, *args, **kwargs):
        kwargs.pop('mgmt', None)
        return super().__init__(*args, **kwargs)

    def __setitem__(self, k, v):
        if v is not None:
            return super().__setitem__(k, v)
        super().pop(k, None)

class ChoiceStr(str):
    # You might want an Enum, but for Entity properties, I consider that too strict.
    # If a source of truth reports a value - it is, IMO, valid, whether I knew of it before or not.
    # So this will log it's surprise, but not generate an error
    # We could validate against .choices in set_property(SourceTypes.LOCAL) to be strict on user side
    choices: List[str] = []
    consts_key: Optional[str] = None

    def __new__(cls, *args):
        if cls.choices and args[0] and args[0] not in cls.choices:
            logging.getLogger(cls.__name__).info(f'{cls.__name__.upper()} created with invalid value: "{args[0]}"')
        return super().__new__(cls, *args)

class NotificationLevel(ChoiceStr):
    choices = ['NONE', 'WARNING', 'ERROR']

class Role(ChoiceStr):
    choices = ['Admin', 'Observer']
    consts_key = 'userRoles'

class ECSeparationType(ChoiceStr):
    choices = ['Full Separation', 'Minimal Separation', 'Ignore Separation']
    consts_key = 'ecSeparationTypes'

class RAIDLevel(ChoiceStr):
    choices = ['Concatenated', 'Striped RAID-0', 'Mirrored RAID-1', 'Striped & Mirrored RAID-10', 'Erasure Coding']
    consts_key = 'RAIDLevel'

class LoggingLevel(ChoiceStr):
    choices = ['INFO', 'WARNING', 'ERROR', 'DEBUG', 'VERBOSE', 'NONE']
    consts_key = 'loggingLevel'

class HTTPSServerAuthMethod(ChoiceStr):
    choices = ['credentials', 'MTLS']
    consts_key = 'HTTPSServerAuthenticationMethods'

class EmulationMode(ChoiceStr):
    choices = ['NONE', 'STATIC', 'HOTPLUG']
    consts_key = 'emulationModeNames'

class UpgradeExecutionModes(ChoiceStr):
    choices = ['MANUAL', 'MANUAL_START', 'AUTOMATIC']
    consts_key = 'upgradeExecutionModes'

class UpgradeRedundancyLevels(ChoiceStr):
    choices = ['MINIMAL', 'MAX', 'NONE']
    consts_key = 'upgradeRedundancyLevels'
