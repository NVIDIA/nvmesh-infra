# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import logging
from os import devnull, path, walk
from re import sub, match
from abc import abstractmethod
from collections import defaultdict
from enum import Enum
from typing import NamedTuple, Optional, Dict, List, Type, Any, Union, Tuple
from types import SimpleNamespace
from argparse import Namespace
from threading import RLock
import inspect
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader, module_from_spec
from xlro.core.util.common import get_path
from xlro.core.util.general_utils import host_name

logger = logging.getLogger('DiagUtil')
DiagArgs = Namespace()

class StopDiag(Exception):
    ''' Flow control exceptions. Just raise upwards until handled. '''
    pass

class StopNode(StopDiag):
    ''' An exception which stops further diagnostics of this Node '''
    pass

class StopAll(StopDiag):
    ''' An exception which stops all further diagnostics '''
    pass

# TODO: What is this?
class FixEndException(Exception):
    pass

# TODO: Move to a Logger
class MsgLvl(Enum):
    HEADER = ('\033[1m\033[4m', logging.INFO, 'INFO')
    SUCCESS = ('\033[92m', logging.INFO, 'PASS')
    WARNING = ('\033[33m', logging.WARNING, 'WARN')
    ERROR = ('\033[31m', logging.ERROR, 'FAIL')

class DiagMessage(NamedTuple):
    msg: str
    level: str # 'INFO', 'PASS', 'WARN', 'FAIL'

class DiagModule(object):
    diag_max_level: int = 0
    _instance_by_node: Dict[str, Dict[Any, 'DiagModule']] = defaultdict(dict)
    _class2expectations: Dict[str, Dict[str, Any]]
    description = ''

    @classmethod
    def _cname(cls):
        return cls.__name__.rpartition('.')[2]

    def __init__(self, node=None):
        cname = self._cname()
        assert node not in self._instance_by_node[cname], f'Duplicate diag instance for node: {node}'
        self._instance_by_node[cname][node] = self
        self.node = node
        self.nodename = node.name.partition('.')[0]
        self.max_msg_level = 0
        self.diag_info = SimpleNamespace()
        self.discover_done = False
        self.validate_done = False
        self.details_done = False
        # Not sure if we'll need thread-safety, but better safe than sorry
        self.lock = RLock()
        self.logger = logging.getLogger(f'{self.__class__.__name__}#{node}')

        # Variables to be dumped into json
        self.status = MsgLvl.SUCCESS.value[2]
        self.messages: List[DiagMessage] = []

        # TODO: Refactor legacy properies
        self.set_parameters = False #DiagArgs.set_parameters

    @property
    def error_level(self):
        # Default will be to just track max message level, but I guess a Module could override for whatever reason
        # Also, presumably a "fix" should drop the error level?
        self.run_phase('validate') # Make sure we ran
        return self.max_msg_level

    @property
    def info(self):
        # Haven't yet decided how diagnosers might use each others results.
        # I think explicit instance properties are safest, avoiding more runtime errors
        # But need to ensure diag ran before info pulled, so use property's
        self.run_phase('discover') # Make sure we ran
        return self.diag_info

    @property
    def expectations(self):
        return self._class2expectations.get(self.__module__, {})

    def get_info(self, diag_name: str) -> SimpleNamespace:
        return DiagManager.diag_instance(diag_name, self.node).info

    @classmethod
    def instance(cls, node):
        try:
            return cls._instance_by_node[cls._cname()][node]
        except:
            return cls(node)

    @abstractmethod
    def discover(self):
        ''' Populate info needed by self or others to run validation '''
        pass

    @abstractmethod
    def validate(self):
        ''' Validate configurations and optionally suggest/fix invalid configs '''
        # NOTE: separating validate and fixup would complicate messages.  Is invalid config a warning or error if
        # we MAY fix it up at a later stage?
        pass

    @abstractmethod
    def details(self):
        ''' Detailed reporting on system status and configuration.  (Non-validated, just reported.) '''
        pass

    @abstractmethod
    def collect(self):
        ''' Collect artifacts needed for debugging to the target logs directory '''
        pass

    def run_phase(self, phase_name):
        if getattr(self, f'{phase_name}_done'):
            return

        with self.lock:
            if getattr(self, f'{phase_name}_done'):
                return

        try:
            self.logger.debug(f'{phase_name.upper()}: {self._cname()} on {self.nodename}')
            getattr(self, phase_name)()
        except StopDiag:
            raise
        except Exception as e:
            stop_msg = None
            # Handle some special case exceptions for known ssh/access problems.
            e_str = str(e)
            m = match('property (\w+) of entity (\w+)', e_str)
            if isinstance(e, AttributeError) and m:
                stop_msg = f'Skipping - cannot get {m.group(2)}.{m.group(1)}'
            elif e_str.startswith('Connect to "'):
                stop_msg = f'Skipping - no ssh access.'

            if stop_msg:
                self.skip(stop_msg)
            else:
                self.add_message(f'Failure: {repr(e)}', msg_level=MsgLvl.ERROR)
                raise
        finally:
            setattr(self, f'{phase_name}_done', True)

        if phase_name == "validate" and self.max_msg_level <= logging.DEBUG:
            # print ok message after final phase
            self.add_message(f'{self._cname()} ok on {self.nodename}') #, MsgLvl.SUCCESS)

    def skip(self, msg: str):
        self.add_message(msg, MsgLvl.WARNING)
        raise StopDiag(msg)

    def run_cmd(self, cmd, title=None, print_out=False, print_err=True, is_sudo=True):
        host_info = self.get_info('host')
        if not host_info.has_shell or (is_sudo and not host_info.has_root):
            self.skip(f'No remote {"root " if is_sudo else ""}execution access')
        if title:
            self.add_message(title)
        # TODO: Ensure localhost doesn't use ssh, even locally
        out, err, code = self.node.execute(cmd)
        if code != 0 and print_err:
            self.add_message(f'Failed execute {cmd} command - code: {code} err: {err.strip()}', MsgLvl.ERROR)
        elif print_out:
            self.add_message(out)
        return out, err, code

    def add_message(self, message, msg_level=None, prefix=""):
        msg_status = msg_level.value[2] if msg_level else 'INFO'
        if msg_level == MsgLvl.ERROR or msg_level == MsgLvl.WARNING and self.status == 'PASS': # 'FAIL' > 'WARN' > 'PASS', always select max(self.status,msg_status)
            self.status = msg_status

        # TODO: Switch all the printing/writing to logging and use the verbosity to set the level. Also, handle coloring by level?
        import inspect
        msg_priority = msg_level.value[1] if msg_level else logging.DEBUG
        self.max_msg_level = max(self.max_msg_level, msg_priority)
        DiagModule.diag_max_level = max(self.max_msg_level, DiagModule.diag_max_level)

        message = f'{prefix}{self._cname()} <{self.nodename}> {message}'
        _msg = f'{msg_level.value[0]}{message}\033[0m' if msg_level and not DiagArgs.nocolor else message
        if DiagArgs.json:
            self.messages.append(DiagMessage(message, msg_status))
        else: # Not json format; plain text only
            if msg_priority >= DiagArgs.console_level or msg_level == MsgLvl.SUCCESS: # We also print success messages for better prompts.
                print(_msg)

            if DiagArgs.output is not sys.stdout: # We want to output all messages only if it's to a real file
                print(_msg, file=DiagArgs.output)
                DiagArgs.output.flush()

        # For debugging, helpful to know where the message originated.
        stack = inspect.stack()
        for frame in stack:
            if frame.filename != __file__:
                break
        logger.debug(f'{msg_level._name_ if msg_level else "MSG"} @{path.basename(frame.filename)}#{frame.lineno} {message}')

        return _msg

    @property
    def json_data(self):
        return {
            "status": self.status,
            "messages": self.messages
        }

    def suggest_fix(self, message, break_after_fix, fix_method, *args, **kwargs):
        if not self.set_parameters:
            return
        # TODO: support -y
        replies = ["yes", "ye", "y", "no", "n"]
        while True:
            reply = input(f"{message} [Yes/No]").lower().strip()
            if reply not in replies:
                print(f"Invalid choice - {reply}")
            elif reply[0] == "y":
                fix_method(*args, **kwargs)
                if break_after_fix:
                    raise FixEndException()
            else:
                return

    # TODO: This should move out do a DM, e.g., platform-DM vs all generic stuff being built into base class
    def get_os_platform(self):
        linux_distributuion = self.node.os_info['ID'].lower()
        if any(rh_dist in linux_distributuion for rh_dist in ["redhat", "centos"]):
            return "rhel"
        elif "ubuntu" in linux_distributuion:
            return "ubuntu"
        else:
            self.add_message("Untested Linux distribution!", MsgLvl.WARNING)
            self.add_message(
                "RHEL/CentOS 7.3, 7.4; Oracle Linux 7.4, 7.5, SLES 12 SP3 and Ubuntu LTS 16.4, 18.4 are tested Linux "
                "distributions for this version.")

    def get_package_manager(self):
        os_platform = self.get_os_platform()
        if os_platform == 'rhel':
            return 'yum'
        elif os_platform == 'ubuntu':
            return 'apt-get'
        else:
            raise Exception('No package manager for requested os platform')

    def is_um(self):
        """
        Check if the current node is running the UM service.

        Returns:
            bool: True if nvmeshum service is active, False otherwise.
        """
        cmd = "systemctl is-active nvmeshum.service || echo inactive"
        um_status, _, _ = self.run_cmd(cmd, is_sudo=True)
        return um_status.strip() == "active"

class DiagGroup(DiagModule):
    ''' A parent diag for a set of children diags.
        NOTE: Since Diag's are Types, not Instances, a new "class" must be created via new_group_class()
    '''
    dm_classes: List[Type[DiagModule]] = []
    dm_instances: List[DiagModule] = []

    def __init__(self, node):
        super().__init__(node)
        self.dm_instances = [dm_cls.instance(node) for dm_cls in self.dm_classes]

    @classmethod
    def new_group_class(cls, name: str, nested_diags: List[Type[DiagModule]], is_set=False) -> Type['DiagGroup']:
        ''' Create a dynamic DiagGroup class wrapping the given children diags '''
        cls_name = sub(r'\W+', '_', name).strip('_').title() + 'Group'
        return type(cls_name, (DiagGroup,), {'description': f'{name} group', 'dm_classes': nested_diags, 'is_set': is_set})

    def run_phase(self, phase_name):
        for dm in self.dm_instances:
            try:
                dm.run_phase(phase_name)
            except StopDiag:
                pass

            if phase_name == 'validate':
                self.max_msg_level = max(dm.error_level, self.max_msg_level)
                self.diag_info.__dict__.update(dm.diag_info.__dict__)
                if dm.status == 'FAIL' or dm.status == 'WARN' and self.status == 'PASS':
                    self.status = dm.status
                self.messages.extend(dm.messages)


class ExcludedDiag(DiagModule):
    pass

class DiagManager(object):
    # Map mod name to diag class
    class_by_name: Dict[str, Type[DiagModule]] = {}
    _search_paths: Optional[List[str]] = None

    @classmethod
    def search_paths(cls) -> List[str]:
        if cls._search_paths is not None:
            return cls._search_paths
        paths: List[str] = []
        for p in DiagArgs.extensions.split(':'):
            paths.append(path.expanduser(p.strip()))
        paths.append(path.join(get_path(path.dirname(__file__)), 'modules'))
        paths.append(path.join(get_path(path.dirname(__file__)), path.join('modules', 'sets')))
        cls._search_paths = paths
        return paths

    @classmethod
    def diag_instance(cls, name: str, node: str):
        return cls.load_by_name(name).instance(node)

    # TODO: Is there a reason to pre-load in the current mod=class/instnace=node? Helps with run-time errors, but they could happen anyway
    @classmethod
    def load_by_name(cls, name: str) -> Type[DiagModule]:
        try:
            return cls.class_by_name[name]
        except:
            pass

        if name in DiagArgs.exclude:
            cls.class_by_name[name] = ExcludedDiag
            return ExcludedDiag

        # Check for diag-set file
        # NOTE: labeling harder to implement, as is YAML because hard to add/remove from list
        for ddir in cls.search_paths():
            file_found = False
            dpath = path.join(ddir, name + '.set')
            try:
                with open(dpath, 'r') as dfile:
                    file_found = True
                    classes = []
                    line_no = 0
                    try:
                        for line in dfile:
                            line_no += 1
                            dname = line.strip()
                            if dname and dname[0] != '#':
                                classes.append(cls.load_by_name(dname))
                        logger.debug(f'{name} loaded via {dpath}')
                        cls.class_by_name[name] = DiagGroup.new_group_class(name, classes, is_set=True)
                        return cls.class_by_name[name]
                    except Exception as e2:
                        raise Exception(f'Failure scanning "{name}" line: {line_no}. {e2}')
            except Exception as e:
                logger.debug(f'Error loading {dpath}: {repr(e)}')
                if file_found:
                    raise

        # Check for python module
        # IMO, Security issue to insert local_path to start of system path, so keep it on the end
        for pkgd in cls.search_paths():
            try:
                # Simple import_module() didn't work if diag name was same as a "real" python module (e.g., platform)
                # So use lower-level methods to explicitly load from full path
                pypath = path.join(pkgd, name + '.py')
                loader = SourceFileLoader(name, pypath)
                module = module_from_spec(spec_from_loader(loader.name, loader))
                loader.exec_module(module)
                classes = []
                for n, c in inspect.getmembers(module, inspect.isclass):
                    if issubclass(c, DiagModule) and c.__module__.rpartition('.')[2] == name:
                        logger.debug(f'Member {n} ({type(c)}) accepted')
                        classes.append(c)
                    else:
                        logger.debug(f'Member {n} ({c.__module__}) rejected')
                assert classes, f'No Diag classes found in {pypath}'
                if len(classes) == 1:
                    cls.class_by_name[name] = classes[0]
                else:
                    cls.class_by_name[name] = DiagGroup.new_group_class(name, classes)
                logger.debug(f'{name} loaded via module {module}.  Classes: {[c.__name__ for c in classes]}')
                return cls.class_by_name[name]
            except SyntaxError as se:
                raise
            except Exception as e:
                logger.debug(f'Error loading {pypath}: {repr(e)}')
                pass

        # TODO: Check for shells
        raise Exception(f'Cannot determine diagnostic class(es) for "{name}"')

    @staticmethod
    def list_modules() -> Tuple[List[str], List[Dict[str, List[str]]]]:
        modules = []
        group_modules = []
        mod_paths = [path.abspath(path.join(dirpath, fname)) for sp in DiagManager.search_paths() for dirpath, _, fnames in walk(sp) for fname in fnames]
        for mod_path in mod_paths:
            mod_name, extension = path.splitext(path.basename(mod_path))
            if extension == '.py' and mod_name[0] != '_':
                modules.append(mod_name)
            elif extension == '.set':
                group_mod_names = []
                with open(mod_path, 'r') as fp:
                    for m in fp.readlines():
                        mname = m.strip()
                        if mname and mname[0] != '#':
                            group_mod_names.append(mname)

                group_modules.append({mod_name: group_mod_names})

        return modules, group_modules

class ClusterDiagnostic(DiagModule):
    def __init__(self, node=None):
        super().__init__(node)
        self._instance_by_node[self._cname()]['cluster'] = self

    @classmethod
    def instance(cls, node):
        try:
            return cls._instance_by_node[cls._cname()]['cluster']
        except:
            return cls(node)
