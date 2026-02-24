# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
import os
from abc import ABCMeta, abstractmethod
from xlro.core.util.ssh import Connection
import copy
import pipes, shlex
from typing import Any, Optional, Dict, TYPE_CHECKING
if TYPE_CHECKING:
    from xlro.core.entities import Host
__all__ = [ 'ConfigFile', 'TextConfig', 'PropsConfig', 'ModConfig' ]


class ConfigFile(object): # Should be ABCMeta?
    def __init__(self, host: 'Host', file_path: str) -> None:
        self.host = host
        self.file_path = file_path
        # Content is a string fo the file content
        self.content = None
        self.old_content = None
        # Config is any object. By default content string, but subclasses make a dict
        self.config: Optional[Any] = None
        # maybe need lock

    def apply(self):
        # Save current config to file. This could be optimized...
        assert self.old_content is not None, 'apply() called before upload() for {}:{}'.format(self.host, self.file_path)
        self.content = self._config_to_content(self.config)
        self._writefile(self.host, self.file_path, self.content)

    def upload(self):
        # Load current config from file
        self.content = self.host.connection.execute('cat {}'.format(self.file_path))[0]
        if self.old_content is None:
            # Only save old_content once, even if called multiple times
            self.old_content = self.content
        self.config = self._content_to_config(self.content)
        return self

    def backup(self):
        assert self.old_content is not None, 'backup() called before upload() for {}:{}'.format(self.host, self.file_path)
        # Consider adding backup-file on first backup, and removing on restore()
        return { 'content': self.content, 'config': copy.copy(self.config) }

    def restore(self, restore_opts={}):
        assert self.old_content is not None, 'restore() called before upload() for {}:{}'.format(self.host, self.file_path)
        # We should assert restore_opts vs. defaulting to old_content...
        restore_content = restore_opts.get('content', self.old_content)
        if restore_content != self.content:
            self._writefile(self.host, self.file_path, restore_content)
            self.content = restore_content
        # Need to keep config/content in sync
        try:
            self.config = restore_opts['config']
        except:
            self.config = self._content_to_config(self.content)

    def _config_to_content(self, config):
        return config

    def _content_to_config(self, content):
        return content

    @staticmethod
    def _writefile(host, file_path, content):
        Connection.err2exc(host.connection.execute('sudo dd of={}'.format(file_path), inbuf=content))


class TextConfig(ConfigFile):
    def conf_set(self, value):
        # for backwards compatibility of tests. Should be removed, IMO.
        # Inconsistent across types anyway, no getter, etc.
        # User's should interact directly with config object.
        self.config = value

def conf_to_dict(content: str) -> Dict[str, Any]:
    config = {}
    for line in content.split('\n'):
        key, _, value = line.strip().partition('=')
        key = key.strip()
        if key and not key.startswith('#'):
            config[key] = shlex.split(value, comments=True)[0].strip()
    return config


class PropsConfig(ConfigFile):
    def conf_set(self, key, value):
        # for backwards compatibility
        self.config[key] = value        # type: ignore ## conf_set() should be removed!

    def _config_to_content(self, config):
        # TODO: check for proper formatting
        return "\n".join(['{}={}'.format(key, pipes.quote(str(value))) for key, value in config.items()]) + "\n"

    def _content_to_config(self, content):
        return conf_to_dict(content)


def _read_sysmod_item(host, param_key_path):
    out, err, code = host.connection.tolerant_exec('cat {param_path}'.format(param_path=param_key_path))
    if code:
        raise KeyError("item {} isn't exist in {}".format(param_key_path, host.name))
    out = out.strip()
    return "" if out == "(null)" else out


def _write_sysmod_item(host, param_key_path, value):
    out, err, code = host.connection.tolerant_exec(
        'echo "{value}" | sudo tee {param_path}'.format(value=value, param_path=param_key_path))
    if code:
        raise KeyError("item {} isn't exist in {}".format(param_key_path, host.name))


class DictConfig(object):
    def __init__(self, host, path_prefix=""):
        self.host = host
        self.curr_config = {}
        self.old_config = {}
        self.path_prefix = path_prefix
        self.param_changed = set()

    def __setitem__(self, key, value):
        if key not in self.old_config:
            self.old_config[key] = self[key]
        if self.curr_config.get(key, None) != value:
            self.param_changed.add(key)
        self.curr_config[key] = value

    def __getitem__(self, item):
        if item not in self.curr_config:
            self.curr_config[item] = _read_sysmod_item(self.host, os.path.join(self.path_prefix, item))
        return self.curr_config[item]


class ModConfig(ConfigFile):

    def __init__(self, host, file_path):
        super(ModConfig, self).__init__(host, file_path)

    def upload(self):
        self.config = DictConfig(self.host, self.file_path)
        return self

    def apply(self):
        assert self.config and isinstance(self.config, DictConfig), 'apply() before upload()?'
        for key in self.config.param_changed:
            _write_sysmod_item(self.host, os.path.join(self.file_path, key), self.config.curr_config[key])
        self.config.param_changed.clear()

    def restore(self, restore_opts={}):
        assert self.config and isinstance(self.config, DictConfig), 'restore() before upload()?'
        assert restore_opts, 'Restore values required.'
        for key in self.config.old_config:
            _write_sysmod_item(self.host, os.path.join(self.file_path, key), self.config.old_config[key])
        for c_attr in restore_opts:
            prop = getattr(self.config, c_attr)
            prop.clear()
            prop.update(restore_opts[c_attr])

    def backup(self):
        assert self.config and isinstance(self.config, DictConfig), 'restore() before upload()?'
        backup_obj = {
            'curr_config': copy.copy(self.config.curr_config),
            'old_config': copy.copy(self.config.old_config),
            'param_changed': copy.copy(self.config.param_changed)
        }
        self.config.old_config.clear()
        return backup_obj
