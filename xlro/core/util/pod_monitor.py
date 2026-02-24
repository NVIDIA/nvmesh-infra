# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
from typing import Dict, Union, Optional
from copy import deepcopy

from xlro.core.entities import Manager, Attachment, Client, Volume, K8sClient, Node
from xlro.core.util.cli_util import str2entity
from xlro.core.util.monitor import CmdMonitor
from xlro.core.util.ssh import execute_cmd_locally, Connection


class BasePodMonitor(CmdMonitor):
    DEFAULT_CONTAINER_CONF = [{
        "name": "pod-monitor-container",
        "image": "docker.io/iyoavco/io:latest",
        "imagePullPolicy": "Always",
        "command": ["/bin/bash", '-c', '--'],
        "args": ["echo 'starting base pod monitor on $(date). Will exit in 30 seconds.' && sleep 30"]
    }]

    DEFAULT_POD_CONF = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "base-pod-monitor"},
        "spec": {
            "containers": DEFAULT_CONTAINER_CONF,
            "restartPolicy": "Never"
        }
    }

    def __init__(self, pod_name: str, ctrl_node: Union[Node, str], pod_conf: Dict, cmd: str = '', timeout: int = 10, *args, **kwargs):
        self.conf_path = None
        if cmd:
            # pod already exists, run cmd on pod
            cmd = f'kubectl wait pod/{pod_name} --for=jsonpath="{{.status.phase}}"=Running --timeout={timeout}s && ' \
                  f'kubectl exec -it {pod_name} -- sh -c "{cmd}"'
        else:
            self.pod_conf = deepcopy(BasePodMonitor.DEFAULT_POD_CONF)
            if pod_conf:
                self.pod_conf.update(pod_conf)

            self.conf_path = os.path.join(Connection.err2exc(execute_cmd_locally(f'mktemp -t pod_XXXXX.json')).strip())
            cmd = f'podname=$(kubectl apply -o name -f {self.conf_path}) && ' \
                  f'kubectl wait $podname --for=jsonpath="{{.status.phase}}"=Running --timeout={timeout}s && ' \
                  f'kubectl logs -f $podname && ' \
                  f'exit $(kubectl get $podname -o jsonpath="{{.status.containerStatuses[].state.terminated.exitCode}}")'

        kwargs['host'] = ctrl_node if isinstance(ctrl_node, str) else ctrl_node.name
        super(BasePodMonitor, self).__init__(cmd, *args, **kwargs)

    def start(self):
        if self.conf_path:
            with open(self.conf_path, 'w') as fp:
                json.dump(self.pod_conf, fp, indent=2)

            Connection.get_connection(self.host).put_file(self.conf_path, self.conf_path)
        super(BasePodMonitor, self).start()

    def get_stop_cmd(self, force=False):
        rc = f'[ ! -f {self.conf_path} ] || {{ kubectl delete -f {self.conf_path} {"--force" if force else ""} && rm -f {self.conf_path}; }}'
        return rc

    def stop(self):
        super(BasePodMonitor, self).stop()
        if self.conf_path:
            # if monitor started the pod, stop it
            try:
                Connection.err2exc(Connection.execute_on_host(self.host, self.get_stop_cmd()))
            except Exception as e:
                self.logger.warning(f"Unable to shutdown pod {self.name} gracefully on {self.host} - {repr(e)}. Forcing deletion")
                Connection.err2exc(Connection.execute_on_host(self.host, self.get_stop_cmd(force=True)))

            execute_cmd_locally(f'rm -f {self.conf_path}')
        else:
            cmd = "ps aux | grep " + self.volume_name + r" | grep -v grep | awk '{print \$1}' | xargs -r kill"
            Connection.err2exc(Connection.execute_on_host(self.host, f'kubectl exec {self.pod_data["metadata"]["name"]} -- bash -c "{cmd}"'))


class IOPodMonitor(BasePodMonitor):
    def get_io_mon_pod_conf(self, name, cmd):
        container_conf = deepcopy(BasePodMonitor.DEFAULT_CONTAINER_CONF)

        container_data = {
            'args': [cmd],
            'securityContext': {
                'privileged': True,
                'capabilities': {'add': ['SYS_ADMIN']},
                'allowPrivilegeEscalation': True
            }
        }

        if not self.pvc_data or (self.io_tool == 'fio' and '--directory' in cmd):
            container_data.update({'volumeMounts': [{'name': self.volume_name, 'mountPath': '/mnt/nvmesh'}]})
            volume_data = [{"name": self.volume_name, "hostPath": {'path': '/mnt/nvmesh', 'type': 'Directory'}}]
        elif self.pvc_data:
            if self.pvc_data['spec']['volumeMode'] == 'Block':
                # we are using a persistentVolumeClaim which means csi driver will take care of attach
                container_data.update({"volumeDevices": [{"name": self.volume_name, "devicePath": "/dev/my_block_dev"}]})
                volume_data = [{"name": self.volume_name, "persistentVolumeClaim": {"claimName": f"pvc-{self.volume_name}"}}]
            else:
                container_data.update({'volumeMounts': [{'name': self.volume_name, 'mountPath': '/mnt/nvmesh'}]})
                volume_data = [{"name": self.volume_name, "hostPath": {'path': '/mnt/nvmesh', 'type': 'Directory'}}]

        container_conf[0].update(container_data)
        pod_conf = {
            "metadata": {'name': name},
            "spec": {
                "containers": container_conf,
                "nodeName": self.k8s_client.name,
                "restartPolicy": "Never",
                "volumes": volume_data
            }
        }
        return pod_conf

    def get_resource_data(self, r_type: str, r_name: str) -> Optional[Dict]:
        try:
            return json.loads(Connection.err2exc(self.k8s_client.initiator.connection.execute(f'kubectl get {r_type}/{r_name} -o json')))
        except:
            return None

    def __init__(self, client: Union[K8sClient, str], name, cmd, debug_di=None, mgmt=None, io_tool='btest', **monitor_kwargs):
        self.k8s_client = client if isinstance(client, K8sClient) else str2entity(client)
        self.volume_name = monitor_kwargs['volume_name_override'] if monitor_kwargs['volume_name_override'] else f"pvc-{monitor_kwargs['event_defaults']['volume']}"
        pod_name = f'attach-pod-{self.k8s_client._name.replace(".","")}-{self.volume_name}'.lower()
        self.pod_data = self.get_resource_data('pods', pod_name)
        self.pvc_data = self.get_resource_data('pvc', self.volume_name.lower())
        self.volume_name = monitor_kwargs['event_defaults']['volume']
        self.io_tool = io_tool

        if self.pod_data:
            # pod already exist. exec cmd on pod.
            if 'fio' in cmd.split(' ')[1]:
                monitor_kwargs['cmd'] = f'/usr/bin/fio --group_reporting --invalidate=1  --rw=randrw --numjobs=1 --thread=5 --iodepth=32 --bs=4k --ioengine=libaio --direct=1 --rwmixread=80 --runtime=500 --time_based=600 --size=2g --directory /mnt/nvmesh --name {name.replace(":","")} --status-interval=20'
            else:
                monitor_kwargs['cmd'] = cmd

        monitor_kwargs['pod_conf'] = self.get_io_mon_pod_conf(pod_name, cmd)
        super(IOPodMonitor, self).__init__(pod_name, self.k8s_client.initiator, **monitor_kwargs)
        self.name = name
        self.mgmt = mgmt or Manager.get_manager()
        #self.debug_di = debug_di
        self.attachment = Attachment.instance(client=self.k8s_client.nvmesh_client,
                                              volume=Volume.instance(name=self.volume_name, mgmt=self.mgmt))
        self.prev_status = None

    def start(self):
        super(IOPodMonitor, self).start()

    def stop(self):
        super(IOPodMonitor, self).stop()
