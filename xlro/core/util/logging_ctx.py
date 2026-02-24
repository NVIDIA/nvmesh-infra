# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import object
from abc import ABCMeta, abstractmethod


class LoggingContext(object): # Should be ABCMeta?
    _handler = None
    index = -1

    def __init__(self, overlay=None, **kwargs):
        self.overlay = overlay
        self.kwargs = kwargs

    def __enter__(self):
        self.index = self.push(self.overlay, **self.kwargs)
        return None

    def __exit__(self, *args, **kwargs):
        if self.index >= 0:
            self.reset(self.index)
            self.index = -1

    @classmethod
    def handler(cls):
        return None

    @classmethod
    def push(cls, overlay, **kwargs):
        handler = cls.handler()
        if handler:
            return handler.extra.push(overlay, **kwargs)
        return -1

    @classmethod
    def reset(cls, index):
        handler = cls.handler()
        if handler and index >= 0:
            return handler.extra.reset(index)
        return None
