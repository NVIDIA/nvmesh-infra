# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

###
# Copied from infraClient/common/Tree.py
###
"""
/// -----------------------------------------------------------------------------------------------
///   Class:          Tree
///   Description:    Generic parsing based on spaces/indentation, key-value patterns.
///   Author:         Daniela Gottesman                    Date: July 30, 2018
///   Notes:
///   Revision History:
///   Name:                Date:             Description:
///   Daniela Gottesman       July 30, 2018      First iteration.
/// -----------------------------------------------------------------------------------------------
"""

from typing import List, Optional
from builtins import object
import json
import re


class TreeNode(object):
    def __init__(self, level, item):
        self.parent : Optional['TreeNode'] = None
        self.children : List['TreeNode'] = []
        self.level = level
        self.key, self.value = item


class Tree(object):
    def __init__(self, treeList):
        self.root = TreeNode(-1, ('root', {}))
        self.tree = self.buildTree(treeList)

    def buildTree(self, treeList):
        tree = []

        currentLevel = self.root.level
        prev = self.root

        for element in treeList:
            node = TreeNode(element[0], element[1])
            tree.append(node)

            if node.level == currentLevel:
                parent = prev.parent

            elif node.level > currentLevel:
                parent = prev

            else:
                sibling = prev
                while node.level < sibling.level:
                    assert sibling.parent is not None, 'Malformed tree'
                    sibling = sibling.parent
                parent = sibling.parent

            node.parent = parent
            if parent:
                parent.children.append(node)

            currentLevel = node.level
            prev = node

        return tree

    def treeToJson(self):
        def getJson(root):
            treeJson = {}
            valueDict = {}

            if not root.children:
                return {root.key: root.value}

            for child in root.children:
                if child.key not in valueDict:
                    valueDict.update(getJson(child))

            treeJson[root.key] = valueDict
            return treeJson

        return getJson(self.root)['root']

###
# Utility ported from infraClient/common/NetworkDeviceUtils.py
# Parses some specific network command output into json.
###
def toJson(text, priorityChars, lineDel, keyValueDel, strip=False, startPattern='', stopPattern='', patternsToIgnore=[]):
    def parseLine(line, keyValueDel):
        # Returns indentation level and (key, value) pair.
        noPriorityChars = line.lstrip(priorityChars)
        keyValue = re.split(keyValueDel, noPriorityChars, maxsplit=1) if keyValueDel != '' else [noPriorityChars]
        key = keyValue[0].rstrip()
        value = None if len(keyValue) == 1 or keyValue[1] == '' else keyValue[1].lstrip().rstrip()

        return len(line) - len(noPriorityChars), (key, value)

    if not text:
        return {}

    nodes = []
    lines = re.split(lineDel, text)
    canAddNode = startPattern == ''

    for line in lines:
        ignoreLine = False
        if line != '':
            if startPattern != '' and re.match(startPattern, line):
                canAddNode = True

            if stopPattern != '' and re.match(stopPattern, line):
                canAddNode = False

            for pattern in patternsToIgnore:
                if re.match(pattern, line):
                    ignoreLine = True
                    break

            if canAddNode and not ignoreLine:
                if strip:
                    line = line.lstrip(' ')
                nodes.append(parseLine(line, keyValueDel))
    # want to log this?
    return Tree(nodes).treeToJson()


