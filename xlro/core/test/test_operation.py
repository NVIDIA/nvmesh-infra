#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import unittest
from xlro.core.util.operations import Operation, SimpleMultiOperation, ParallelMultiOperation


class IncrementXbyZ(Operation):
    def __init__(self, x, z):
        super(IncrementXbyZ, self).__init__()
        self.x = x
        self.z = z

    def _do(self):
        self._org_x = self.x
        self.x += self.z
        assert True

    def _undo(self):
        self.x -= self.z
        assert True

    def _verify_do(self):
        assert self.x == self._org_x + self.z

    def _verify_undo(self):
        assert self.x == self._org_x


class TestOperation(unittest.TestCase):
    def test_increment_by_z_operation(self):
        test_x = 30
        test_z = 5
        with IncrementXbyZ(test_x, test_z) as d:
            assert d.state == d.OperationState.OPERATION_DO
            assert(d.x == test_x + test_z)
        assert d.state == d.OperationState.OPERATION_UNDO
        assert d.x == test_x


class TestMultiOperation(unittest.TestCase):
    def _test_increment_by_z_multi_operation(self, op_cls):
        test_x = 99
        test_z = 30
        operations = [IncrementXbyZ(test_x, test_z), IncrementXbyZ(test_x, test_z), IncrementXbyZ(test_x, test_z)]
        mop = op_cls(operations)
        assert mop.operations == operations
        with mop as mop:
            assert all([op.state == op.OperationState.OPERATION_DO for op in mop.operations])
            assert all([op.x == test_x + test_z for op in mop.operations])

        assert mop.state == mop.OperationState.OPERATION_UNDO
        assert all([op.state == op.OperationState.OPERATION_UNDO for op in mop.operations])
        assert all([op.x == test_x for op in mop.operations])

    def test_simple_multi_operations(self):
        self._test_increment_by_z_multi_operation(SimpleMultiOperation)

    def test_parallel_multi_operations(self):
        self._test_increment_by_z_multi_operation(ParallelMultiOperation)


if __name__ == '__main__':
    unittest.main()
