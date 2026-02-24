# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from abc import abstractmethod
import threading
from typing import Dict, Any, Iterable, Optional
from xlro.core.entities import SourceTypes, Volume, Client, Node, Attachment
from xlro.core.entities.base import entity, PropertySpec

XC_LOCK: threading.Lock = threading.Lock()

@entity(sourcetypes=[SourceTypes.PROC])
class ExternalClient(Client):
    """
    Non-NVMesh clients that consume IO and pass it to NVMesh Client. (example: NFS, NVMft, DPU, K8s)
    When `attach` operation is issued with such client, a `bind` operation will wrap it to allow the NVMesh attachment.
    `Setup` and `Teardown` operations allow ExternalClient setup to `bind` and `unbind` our external bindings.
    """
    initiator: Node = PropertySpec(Node, key=True)

    def __init__(self, *args, **kwargs):
        super(ExternalClient, self).__init__(*args, **kwargs)
        self.nvmesh_client = Client.instance(name=self.name)
        self.get_property = self._get_property
        self.is_setup: bool = False
        self.bindings: Dict[Volume, Any] = {}

    def _get_property(self, prop: str, *args, **kwargs) -> Any:
        return (super() if prop in self.__class__.xc_props() else self.nvmesh_client).get_property(prop, *args, **kwargs)

    @classmethod
    def xc_props(cls):
        return [p for p in cls._xlro_props if p not in Client._xlro_props]

    @abstractmethod
    def do_setup(self):
        """ Overridable method to implement the setup phase """
        pass

    @abstractmethod
    def do_teardown(self):
        """ Overridable method to implement the teardown phase """
        pass

    @abstractmethod
    def do_bind(self, volumes: Iterable[Volume], *args, **kwargs) -> Dict[Volume, Any]:
        """ Overridable method to implement the bind operation """
        pass

    @abstractmethod
    def do_unbind(self, volumes: Iterable[Volume], *args, **kwargs) -> Iterable[Volume]:
        """ Overridable method to implement the unbind operation """
        pass

    def setup(self):
        """
        A wrapper of abstract method `do_setup`.
        Sets up an ExternalClient allowing bindings to happen.
        """
        if not self.is_setup:
            with XC_LOCK:
                if not self.is_setup:
                    self.logger.debug(f'Setting up {self}')
                    self.do_setup()
                    self.is_setup = True

    def teardown(self):
        """
        A wrapper of abstract method `do_teardown`.
        Tears down an ExternalClient setup and validates no binding leftovers.
        """
        if self.bindings:
            self.logger.debug(f'{self} still bound: {self.bindings.keys()} Unbinding before teardown')
            self.unbind(self.bindings.keys())

        if self.is_setup:
            with XC_LOCK:
                if self.is_setup:
                    self.logger.debug(f'Tearing down {self}')
                    self.do_teardown()
                    self.is_setup = False

    def bind(self, volumes: Iterable[Volume], *args, **kwargs):
        """
        A wrapper of abstract method `do_bind`.
        Allows an ExternalClient to make all preparations needed in order to create NVMesh attachments.
        """
        if not self.is_setup:
            self.setup()

        unbound = set(volumes) - set(self.bindings.keys())
        if unbound:
            self.logger.debug(f"Binding {self}, Volumes: {unbound}, args: {args}")
            self.bindings.update(self.do_bind(unbound, *args, **kwargs))
            self.logger.debug(f"Bindings are now {self.bindings}")

    def unbind(self, volumes: Iterable[Volume], *args, **kwargs):
        """
        A wrapper of abstract method `do_unbind`.
        An opposite operation of `bind`. Will detach volumes and undo bind preparations.
        """
        assert self.is_setup, f'Unable to unbind {volumes}. {self} is not set up!'

        bound = set(volumes) & set(self.bindings.keys())
        if bound:
            self.logger.debug(f"Unbinding {self}, Volumes: {bound}, args: {args}")
            unbound = self.do_unbind(bound, *args, **kwargs)
            for u in unbound:
                self.bindings.pop(u)

    # for compatibility w/ IO Test functions
    def attach(self, volumes, *args, **kwargs):
        return self.bind(volumes, *args, **kwargs)

    def detach(self, volumes, *args, **kwargs):
        return self.unbind(volumes, *args, **kwargs)
