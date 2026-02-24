#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# TODO: get rid of this
from __future__ import division, unicode_literals
from future import standard_library
standard_library.install_aliases()
from builtins import zip, object
from xlro.core.util.general_utils import old_div
from typing import Dict
import inspect
import json
import datetime
import re
import subprocess
import os
import urllib.request, urllib.parse, urllib.error

class Utils(object):
    MAX_INT = -1
    MAX_STR = 'MAX'
    RE_CHARS = '*?[]'

    @staticmethod
    def createMongoQueryObj(mongoObects):
        return {mongoObj.field: mongoObj.value for mongoObj in mongoObects}

    @staticmethod
    def is_pattern(s: str) -> bool:
        return isinstance(s, str) and any(c for c in s if c in Utils.RE_CHARS)

    @staticmethod
    def anchor_regex(s: str) -> str:
        return ('' if not s or s[0] == '^' else '^') + s # for now, don't anchor the end of the regex

    @staticmethod
    def buildQueryStr(queryParams):
        query = '?'
        isFirstParam = True

        for paramName, paramValue in queryParams.items():
            if paramValue is not None:
                if not isFirstParam:
                    query += '&'
                else:
                    isFirstParam = False

                query += '{0}={1}'.format(paramName, json.dumps(Utils.createMongoQueryObj(paramValue)))

        if len(query) == 1:
            query = ''

        return query

    @staticmethod
    def isStr(s):
        return isinstance(s, str)

    @staticmethod
    def convertUnitCapacityToBytes(unitCapacity):
        def getMultipleOfBytesType(unitCapacity):
            binary = 1024
            decimal = 1000
            return binary if 'i' in unitCapacity else decimal

        def getFactor(termFirstLetter):
            return {'b': 0, 'k': 1, 'm': 2, 'g': 3, 't': 4, 'p': 5}[termFirstLetter]

        if not Utils.isStr(unitCapacity) or unitCapacity.lower() == 'max':
            return unitCapacity

        unitCapacity = unitCapacity.lower()
        multipleOfBytesType = getMultipleOfBytesType(unitCapacity)

        search = re.search(r"([0-9]*\.?[0-9]+)(\w+)", unitCapacity)
        assert search, f'Invalid unit capacity: {unitCapacity}'
        value = search.group(1)
        term = search.group(2)
        factor = getFactor(term[:1])

        return float(value) * multipleOfBytesType ** factor

    @staticmethod
    def convertBytesToUnit(bytes, isBinary=True):
        def getUnitType(multiplier, isBinary):
            if multiplier == 1:
                unitType = 'KiB'
            elif multiplier == 2:
                unitType = 'MiB'
            elif multiplier == 3:
                unitType = 'GiB'
            elif multiplier == 4:
                unitType = 'TiB'
            else:
                unitType = 'PiB'

            return unitType.replace('i', '') if not isBinary else unitType

        if not isinstance(bytes, (int, int, float)):
            return bytes

        counter = 0
        someUnits = bytes

        while old_div(someUnits, 1000) >= 1:
            counter += 1
            someUnits /= 1000

        if counter == 0:
            return str(bytes) + 'B'
        else:
            unitFactor = 1024 if isBinary else 1000
            division = float(bytes) / float(unitFactor ** counter)

            return str(round((old_div((division * 100), 100)), 2)) + getUnitType(counter, isBinary)

    @staticmethod
    def executeLocalCommand(command):
        try:
            out = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            stdout, stderr = out.communicate()
            return stdout, stderr
        except OSError as e:
            return None, e

    @staticmethod
    def readConfFile(confFile):
        g: Dict[str, str]  = {}
        l: Dict[str, str]  = {}

        try:
            if not os.path.exists(confFile):
                return False
            else:
                with open(confFile) as fp:
                    exec(fp.read(), g, l)
                return l
        except Exception:
            return False

    @staticmethod
    def getTimeoutEndTime(timeout):
        def addSecs(time, secs):
            fullDate = datetime.datetime(time.year, time.month, time.day, time.hour, time.minute, time.second)
            fullDate = fullDate + datetime.timedelta(seconds=secs)
            return fullDate

        startTime = datetime.datetime.now()
        endTime = addSecs(startTime, timeout)

        return endTime

    @staticmethod
    def createDirIfNotExsits(path):
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            os.makedirs(path)

    @staticmethod
    def encodePlusInRoute(route):
        return ''.join([urllib.parse.quote('+') if c == '+' else c for c in list(route)]) if '+' in route else route

    @staticmethod
    def runLocalScript(scriptPath, args=[], sudo=False, logger=None):
        if not os.path.exists(scriptPath):
            err = 'Could not find local script {0}'.format(scriptPath)
            if logger:
                logger.error(err)
            else:
                print(err)
        else:
            cmd = ['sudo'] if sudo else []
            cmd.append(scriptPath)
            cmd = cmd + args
            return Utils.executeLocalCommand(command=cmd)

    @staticmethod
    def transformManagementClusterToUrls(managementCluster, managementServerProtocol, httpServerPort):
        managementServerProtocol = managementServerProtocol + '://'

        return list(map(lambda address: managementServerProtocol + address,
                   map(lambda address: '{0}:{1}'.format(address.split(':')[0], httpServerPort),
                       managementCluster.split(','))))

    @staticmethod
    def createRouteString(routes, endPointRoute):
        return re.sub(r'/*/', '/', '/{0}/{1}'.format(endPointRoute, '/'.join(routes)))

    @staticmethod
    def fnmatch2mongo_regex(value: str) -> Dict[str, str]:
        return {"$regex": value.replace('.', '\\.').replace('*', '.*').replace('?', '.').replace('[!', '[^')}

    @staticmethod
    def readFile(path):
        content = None
        path = os.path.expanduser(path)
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    content = f.read()
            else:
                print('File {} does not exist.'.format(path))
        except Exception as e:
            print('Failed to read file {}, ex: {}'.format(path, e))

        return content

class AttributeRepresentation(object):
    def __init__(self, display, dbKey, type=None):
        self.display = display
        self.dbKey = dbKey
        self.type = type

class MongoObj(object):
    def __init__(self, field, value):
        """**Represents a mongoDB query object**

        :param field: entity attribute
        :type field: str
        :param value: mongoDB query value
        :type value: int or str or dict
        """
        self.value = value

        if isinstance(field, list):
            self.field = self.__getNestedFieldsStr(fields=[f.dbKey if isinstance(f, AttributeRepresentation) else f for f in field])
        else:
            self.field = field.dbKey if isinstance(field, AttributeRepresentation) else field

    def __getNestedFieldsStr(self, fields):
        return '.'.join(fields)

    def __str__(self):
        return f'{self.field}={self.value}'
