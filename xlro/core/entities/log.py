#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
from datetime import datetime, timedelta
from typing import Union, Dict, Any

from xlro.core.entities import Manager

from xlro.core.entities.base import entity, PropertySpec, SourceTypes
from xlro.core.entities.sdk_base import sdk_entity, SDKEntity

class LogMeta(object):
    pass

@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class Log(SDKEntity):
    id : str = PropertySpec(str, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    audit : bool = PropertySpec(bool, default=False)

    # Id = AttributeRepresentation(display='ID', dbKey='_id')
    # TimeStamp = AttributeRepresentation(display='Date Created', dbKey='timestamp')
    # Level = AttributeRepresentation(display='Level', dbKey='level')
    # Message = AttributeRepresentation(display='Message', dbKey='message')
    # AcknowledgedBy = AttributeRepresentation(display='Acknowledged By', dbKey='acknowledgedBy')
    # DateModified = AttributeRepresentation(display='Date Modified', dbKey='dateModified')
    # Meta = AttributeRepresentation(display='', dbKey='meta', type=Meta)
    # __objectsToInstantiate = ['Meta']

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(Log, cls).map_props(propmap, source_type)
        meta = propmap.pop('meta', None)
        if meta:
            meta.pop('id', None)
            propmap.update(meta)
            link = propmap.pop('link', None)
            if link:
                propmap['link'] = { link['entityType']: link['entityText'] }
            else:
                propmap['link'] = {}

        if propmap.get('dateCreated') is not None:
            propmap.setdefault('dateModified', '')
        if propmap.get('acknowledged') == False:
            propmap.setdefault('acknowledgedBy', '')
        # TODO: All this should be handled by nested: and rest2infra:
        if 'isAudit' in propmap:
            propmap['audit'] = propmap.pop('isAudit')

        return propmap

    def acknowledge(self):
        return self.do_operation(self.mgmt, 'acknowledge', [self.id])

    @classmethod
    def acknowledgeAll(cls, mgmt: 'Manager'):
        return cls.do_operation(mgmt, 'acknowledgeAll')

    @classmethod
    def get_logs(cls, since: Union[datetime, str] = None, mgmt: Manager = None, **kwargs):
        import dateparser
        from pytz import UTC

        if not since:
            since = datetime.utcnow()
        elif isinstance(since, datetime):
            since = since.astimezone(UTC)
        else:
            parsed = dateparser.parse(since)
            assert parsed, f'Invalid since param: {since}'
            since = parsed.astimezone(UTC)
        return cls.get_filtered(
                timestamp={ 'gte': since.isoformat(timespec='milliseconds').partition('+')[0] + 'Z'}, **kwargs)


def main():
    from xlro.core.util.cli_util import CLIArgumentParser
    argparser = CLIArgumentParser(require_manager=True)
    argparser.add_argument('--since', default='10m', help='time back from now to start logs. N[hms]. Default is "s"')
    argparser.add_argument('--nouser', action='store_true', help='filter User login/logout messages')
    args = argparser.parse_args()
    if args.since.isdigit():
        seconds = int(args.since)
    else:
        try:
            unitfactor = {'s': 1, 'm': 60, 'h': 3600, 'd': 3600*24}
            count, units = (args.since[:-1], args.since[-1].lower())
            seconds = int(count) * unitfactor[units]
        except:
            print(f'Invalid value for --since: {args.since}')
            sys.exit(2)
    filters: Dict[str, Any] = { 'since': datetime.now()-timedelta(seconds=seconds) }
    if args.nouser:
        filters ['message'] = {'regex': '^[^U]'}
    for n, l in enumerate(Log.get_logs(**filters)):
        print(f'#{n}: {l.dateCreated} [{l.level}] {l.header}: {l.message} {l.link or ""}' )

if __name__ == '__main__':
    main()
