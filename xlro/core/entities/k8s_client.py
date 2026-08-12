# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import threading
import base64
from collections import defaultdict
from typing import Dict, List, Iterable, Any, Tuple, Optional, Union
from os import path
from xlro.core.entities import ExternalClient, Volume, Attachment, Manager
from xlro.core.entities.base import entity, SourceTypes
from xlro.core.util.ssh import execute_cmd_locally, Connection
from xlro.core.util.thread_manager import ThreadPoolManager

SC_RAID = {
    'Concatenated': 'nvmesh-concatenated',
    'Striped & Mirrored RAID-10': 'nvmesh-raid10',
    'Striped RAID-0': 'nvmesh-raid0',
    'Mirrored RAID-1': 'nvmesh-raid1',
    'Erasure Coding': 'nvmesh-ec-dual-target-redundancy'
}

VPG_RAID = {
    'Concatenated': 'DEFAULT_CONCATENATED_VPG',
    'Striped & Mirrored RAID-10': 'DEFAULT_RAID_10_VPG',
    'Striped RAID-0': 'DEFAULT_RAID_0_VPG',
    'Mirrored RAID-1': 'DEFAULT_RAID_1_VPG',
    'Erasure Coding': 'DEFAULT_EC_DUAL_TARGET_REDUNDANCY_VPG'
}


@entity(sourcetypes=[SourceTypes.PROC])
class K8sClient(ExternalClient):

    VCONF_LOCK: threading.Lock = threading.Lock()
    VOLUME2CONF_PATH: Dict[Volume, str] = {}  # class level dicts to prevent pv,pvc duplicates
    VOLUME2CLIENTS: Dict[Volume, List[ExternalClient]] = defaultdict(list)

    def do_setup(self):
        assert (self.initiator.execute(
            f"kubectl get node/{self._name} -o jsonpath='{{.status.conditions[?(@.type==\"Ready\")].status}}'")[0]
                == "True"), "K8s node {self._name} is not ready"

    def do_teardown(self):
        # TODO: use namespaces to create all bindings so we can kubectl delete pod,pv,pvc -n <namespace> --force here
        self.logger.info("Note that k8s_client.py@do_teardown doesn't do anything (expected)")
        pass

    @staticmethod
    def reservation_mode2access_mode(reservation_mode: str) -> List[str]:
        r2a = {
            Attachment.SHARED_READ_WRITE: ['ReadWriteMany'],
            Attachment.SHARED_READ_ONLY: ['ReadOnlyMany'],
            Attachment.EXCLUSIVE_READ_WRITE: ['ReadWriteOnce']
        }
        return r2a.get(reservation_mode, ['ReadWriteMany', 'ReadWriteOnce', 'ReadOnlyMany'])

    @staticmethod
    def get_k8s_pvc_conf(volume: Union[Volume, str], access_modes: List[str], storageclass: str, volume_mode: str = 'Block', capacity: str = None) -> Dict:
        """
        return json required for creating pvc
        """
        volume_mode = 'Block' if not volume_mode else volume_mode
        return \
            {
                'apiVersion': 'v1',
                'kind': 'PersistentVolumeClaim',
                'metadata': {
                    'name': f'pvc-{volume}' if isinstance(volume, str) else f'pvc-{volume.name}',
                },
                'spec': {
                    'accessModes': access_modes,
                    'volumeMode': volume_mode,
                    'resources': {
                        'requests': {
                            'storage': capacity if capacity else volume.capacity
                        }
                    },
                    'volumeName': volume if isinstance(volume, str) else volume.name,
                    'storageClassName': storageclass
                }
            }

    @staticmethod
    def get_k8s_pv_conf(volume: Volume, access_modes: List[str], storageclass: str, volume_mode: str = None) -> Dict:
        """
        return json required for creating pv
        """
        volume_mode = 'Block' if not volume_mode else volume_mode
        return \
            {
                'apiVersion': 'v1',
                'kind': 'PersistentVolume',
                'metadata': {
                    'name': volume.name
                },
                'spec': {
                    'accessModes': access_modes,
                    'persistentVolumeReclaimPolicy': 'Retain',
                    'capacity': {
                        'storage': volume.capacity
                    },
                    'storageClassName': storageclass,
                    'volumeMode': volume_mode,

                    'csi': {
                        'driver': 'nvmesh-csi.excelero.com',
                        'volumeHandle': f'single-zone-cluster:{volume.name}:{volume.uuid}'
                    }
                }
            }

    @staticmethod
    def get_k8s_base_pod(volume: Union[Volume, str] = None, volume_mode: str = 'Block', client_node: str = None,
                         metadata_name: str = None, volume_key_name: str = None, volumes_name: str = None,
                         pvc_claimName: str = None, affinity: bool = False, **kwargs):
        """
        return json required for creating pod
        """
        if volume_mode == 'Block' or not volume_mode:
            volume_key = "volumeDevices"
            device_key = "devicePath"
            device_value = "/dev/my_block_dev"
        else:
            volume_key = "volumeMounts"
            device_key = "mountPath"
            device_value = "/mnt/nvmesh"
        metadata_name = metadata_name if metadata_name else volume if volume and isinstance(volume, str) else volume.name
        volume_key_name = volume_key_name if volume_key_name else volume if volume and isinstance(volume, str) else volume.name
        volumes_name = volumes_name if volumes_name else volume if volume and isinstance(volume, str) else volume.name
        pvc_claimName = pvc_claimName if pvc_claimName else volume if volume and isinstance(volume, str) else volume.name
        pod = \
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": metadata_name
                },
                "spec": {
                    "containers": [
                        {
                            "name": "pod-monitor-container",
                            "image": "docker.io/iyoavco/io:latest",
                            "imagePullPolicy": "Always",
                            "command": ["/bin/bash", "-c", "--"],
                            "args": [
                                'sigterm() { echo "got SIGTERM"; exit 0; } ; trap sigterm SIGTERM ; while true ; do echo "running.." ; sleep 5 ; done ;'
                            ],
                            volume_key: [
                                {
                                    "name": volume_key_name,
                                    device_key: device_value
                                }
                            ],
                            "securityContext": {
                                "privileged": True,
                                "capabilities": {
                                    "add": ["SYS_ADMIN"]
                                },
                                "allowPrivilegeEscalation": True
                            }
                        }
                    ],
                    "restartPolicy": "Never",
                    "volumes": [
                        {
                            "name": volumes_name,
                            "persistentVolumeClaim": {
                                "claimName": f"pvc-{pvc_claimName}"
                            }
                        }
                    ]
                }
            }

        if client_node:
            pod['spec']['nodeName'] = client_node

        if affinity:
            pod['spec']['affinity'] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "nvmesh-csi/client-status",
                                        "operator": "In",
                                        "values": ["ready"]
                                    }
                                ]
                            }
                        ]
                    }
                }
            }

        return pod

    @staticmethod
    def get_k8s_storageclass(vpg: str, fs_type: str = None, encryption: dict = None, storageclass: str = None,
                             secret_name: str = 'automated', **kwargs):
        """
        return json required for creating storageclass
        :vpg: vpg name
        :fs_type: xfs/ext4
        :encryption: allow modification of default parameters encryption data values. to change a value send it as
                     key:value, for example: {'provisioner-secret-name':'hellowork'}
                     encryption["secret_name"] will set provisioner, node-stage and node-expand secret-name value
                     encryption["secret_namespace"] will set provisioner, node-stage and node-expand secret-namespace value
        :secret_name: in not overridden by encryption["secret_name"], will set provisioner, node-stage and node-expand
                      secret-name value. default: 'automated'
        """
        encryption_data = None

        if encryption is not None:

            if isinstance(encryption, str):
                encryption = {"encryption": encryption}
            elif isinstance(encryption, bool):
                encryption = {}

            if "secret_name" in encryption:
                encryption["provisioner-secret-name"] = encryption["secret_name"]
                encryption["node-stage-secret-name"] = encryption["secret_name"]
                encryption["node-expand-secret-name"] = encryption["secret_name"]

            if "secret-namespace" in encryption:
                encryption["provisioner-secret-namespace"] = encryption["secret-namespace"]
                encryption["node-stage-secret-namespace"] = encryption["secret-namespace"]
                encryption["node-expand-secret-namespace"] = encryption["secret-namespace"]

            encryption_data = {
                "vpg": vpg,
                "encryption": "dmcrypt" if "encryption" not in encryption else encryption["encryption"],
                "encryption/headerSize": "16" if "headerSize" not in encryption else encryption["headerSize"],
                "encryption/slot": "1" if "slot" not in encryption else encryption["slot"],
                "encryption/numberOfSlots": "6" if "numberOfSlots" not in encryption else encryption["numberOfSlots"],
                "encryption/keySize": "512" if "keySize" not in encryption else encryption["keySize"],
                "csi.storage.k8s.io/provisioner-secret-name": secret_name if "provisioner-secret-name" not in encryption else encryption["provisioner-secret-name"],
                "csi.storage.k8s.io/provisioner-secret-namespace": "default" if "provisioner-secret-namespace" not in encryption else encryption["provisioner-secret-namespace"],
                "csi.storage.k8s.io/node-stage-secret-name": secret_name if "node-stage-secret-name" not in encryption else encryption["node-stage-secret-name"],
                "csi.storage.k8s.io/node-stage-secret-namespace": "default" if "node-stage-secret-namespace" not in encryption else encryption["node-stage-secret-namespace"],
                "csi.storage.k8s.io/node-expand-secret-name": secret_name if "node-expand-secret-name" not in encryption else encryption["node-expand-secret-name"],
                "csi.storage.k8s.io/node-expand-secret-namespace": "default" if "node-expand-secret-namespace" not in encryption else encryption["node-expand-secret-namespace"]
            }

        storage_class =  \
            {
                "kind": "StorageClass",
                "apiVersion": "storage.k8s.io/v1",
                "metadata": {
                    "name": storageclass if storageclass else f"storageclass-{vpg.lower().replace('_','')}",
                    "labels": {
                        "nvmesh-csi-testing": ""
                    }
                },
                "provisioner": "nvmesh-csi.excelero.com",
                "allowVolumeExpansion": True,
                "volumeBindingMode": "Immediate",
                "parameters": {
                    "vpg": vpg
                }
            }

        if encryption_data:
            storage_class['parameters'] = encryption_data

        if fs_type:
            storage_class['parameters']['fsType'] = fs_type

        return storage_class

    def k8s_apply_secret(self, name, passphrase):
        """
        create a secret resource and apply it
        """
        secret = base64.b64encode(passphrase.encode()).decode()
        secret_resource = {
            'apiVersion': 'v1',
            'kind': 'Secret',
            'metadata': {
                'name': name,
                'namespace': 'default'
            },
            'data': {
                'passphrase': secret
            }
        }
        with K8sClient.VCONF_LOCK:
            self.apply_resources(name, secret_resource)

    def k8s_apply_resource(self, resource_name: str, resource_type: list, volume: Union[Volume, str] = None,
                           access_modes: List[str] = None, vpg: str = '', storageclass: str = '',
                           volume_mode: str = '', capacity: str = None, client_node: str = '', fs_type: str = '', encryption: dict = None, **kwargs):
        """
        create all specified resources and apply them
        :resource_name: name of resource
        :resource_type: pv, pvc, pod or sc
        :volume: Volume or string, if volume exists it will use data such as RAIDlevel from the object
        :access_modes: ReadWriteOnce/ReadWriteMany/ReadOnlyMany
        :vpg: vpg taken from Volume's RAIDlevel or a custom name
        :storageclass: if not creating a storageclass (sc), name should be given
        :volume_mode: Filesystem or Block
        :capacity: storage size
        :client_node: attach volume (through pod) to specific client
        :fs_type: xfs/ext4
        :encryption: see encryption_data in function get_k8s_storageclass
        """
        resources_names = {r: None for r in resource_type}
        if not access_modes and ('pv' in resource_type or 'pvc' in resource_type):
            access_modes = self.reservation_mode2access_mode(kwargs.get('reservation_mode'))
        if not storageclass and ('pv' in resource_type or 'pvc' in resource_type):
            storageclass = SC_RAID[volume.RAIDlevel] if volume in Manager.get_manager().volumes else SC_RAID[volume.RAIDLevel]
        if not vpg and 'sc' in resource_type:
            vpg = VPG_RAID[volume.RAIDlevel] if volume in Manager.get_manager().volumes else VPG_RAID[volume.RAIDLevel]
        sc = K8sClient.get_k8s_storageclass(vpg, fs_type, encryption, storageclass, **kwargs) if 'sc' in resource_type else None
        storageclass = storageclass if storageclass else sc["metadata"]["name"] if sc else None
        pv = K8sClient.get_k8s_pv_conf(volume, access_modes, storageclass, volume_mode) if 'pv' in resource_type else None
        pvc = K8sClient.get_k8s_pvc_conf(volume, access_modes, storageclass, volume_mode, capacity) if 'pvc' in resource_type else None
        pod = K8sClient.get_k8s_base_pod(volume, volume_mode, client_node, **kwargs) if 'pod' in resource_type else None
        if pvc and volume not in Manager.get_manager().volumes:
            pvc['spec'].pop('volumeName')
        if pod:
            pod['metadata']['name'] = f'attach-pod-{self._name}-pvc-{pod["metadata"]["name"]}'.replace(':', '-').replace('.','').lower()
        resources_names['pv'] = pv['metadata']['name'] if pv else None
        resources_names['pvc'] = pvc['metadata']['name'] if pvc else None
        resources_names['pod'] = pod['metadata']['name'] if pod else None
        resources_names['sc'] = sc['metadata']['name'] if sc else None
        with K8sClient.VCONF_LOCK:
            self.apply_resources(resource_name, sc, pv, pvc, pod)
        return resources_names

    @staticmethod
    def get_k8s_resources(volume, access_modes: List[str] = None, volume_mode: str = 'Block', vpg: str = None, client_node: str = None) -> Tuple[Optional[Dict], Dict, Dict]:
        """
        called from within bind_volume
        """
        if not vpg:
            vpg = VPG_RAID[volume.RAIDlevel] if volume in Manager.get_manager().volumes else VPG_RAID[volume.RAIDLevel]
        return \
            K8sClient.get_k8s_pv_conf(volume, access_modes, vpg, volume_mode) if volume in Manager.get_manager().volumes else None, \
            K8sClient.get_k8s_pvc_conf(volume, access_modes, vpg, volume_mode), \
            K8sClient.get_k8s_base_pod(volume, volume_mode, client_node)

    def create_storageclass(self, vpg, **kwargs):
        """
        creates and apply storageclass to a vpg
        """
        storageclass = K8sClient.get_k8s_storageclass(vpg, **kwargs)
        with K8sClient.VCONF_LOCK:
            self.apply_resources(f'{storageclass["metadata"]["name"]}', storageclass)
        return storageclass["metadata"]["name"]

    def bind_volume(self, volume, *args, **kwargs):
        """
        called from do_bind
        """
        volume_name = kwargs.get('metadata_name') if kwargs.get('metadata_name') else volume.name
        volume_mode = 'Filesystem' if kwargs.get('fs_type') or kwargs.get('volume_mode') else 'Block'
        storageclass_name = self.create_storageclass(**kwargs) if kwargs.get('vpg') else None
        pv, pvc, pod = self.get_k8s_resources(volume, self.reservation_mode2access_mode(kwargs.get('reservation_mode')), volume_mode, storageclass_name, kwargs.get('client_node'))
        if volume not in Manager.get_manager().volumes:
            pvc['spec'].pop('volumeName')
        if not K8sClient.VOLUME2CONF_PATH.get(volume):
            with K8sClient.VCONF_LOCK:
                if not K8sClient.VOLUME2CONF_PATH.get(volume):
                    K8sClient.VOLUME2CONF_PATH[volume] = self.apply_resources(f'pvc_{volume_name}', pv, pvc)

        pod['metadata']['name'] = f'attach-pod-{self._name}-pvc-{volume_name}'.replace(':', '-').replace('.','').lower()
        K8sClient.VOLUME2CLIENTS[volume].append(self)
        return volume, self.apply_resources(f'attach_pod_{self._name}_{volume_name}'.replace(':', '-').replace('.','').lower(), pod)

    def do_bind(self, volumes: Iterable[Volume], volume_mode: str = 'Block', *args, **kwargs) -> Dict[Volume, Any]:
        volume_mode = 'Filesystem' if volume_mode.lower() != 'block' else 'Block'
        volume2attach_pod = {}
        with ThreadPoolManager() as executor:
            for volume in volumes:
                executor.add_task(f"bind {self._name}: {volume.name}", self.bind_volume, volume, volume_mode, *args, **kwargs)
            executor.start()

        for task, res in executor.results.items():
            assert not res.exception(), f'Bind failed for {task} - {repr(res.exception())}'
            volume, r_file = res.result()
            volume2attach_pod[volume] = r_file

        return volume2attach_pod

    def get_k8s_status(self, resource_type: str, resource_name: str, expected_status: str, grep_key: str = 'Status', results_count: str = '1'):
        """
        use to check resource status (or something else), i.e: check if pod is running
        :resource_type: pv, pvc, pod etc.
        :resource_name: specific resource name
        :expected_status: value, i.e: running
        :grep_key: key, default 'Status'
        :results_count: number of grep_key in expected_status (value in key) to return True
        """
        status_results = self.initiator.execute(f'kubectl describe {resource_type} {resource_name} | '
                                                f'grep "{grep_key}" | grep -c "{expected_status}"', success=0)[0].strip()
        return status_results == results_count

    def get_node_label_status(self):
        """
        use for node labeling, function is under construction and neither implemented nor tested
        """
        label_results = self.initiator.execute('kubectl get nodes -o custom-columns="NAME:.metadata.name,CLIENT:.metadata.labels[' + "'nvmesh-csi/client-status']" + '"')[0]
        return label_results

    def create_namespace(self, delete_first=True, namespace='nvmesh-csi'):
        if delete_first:
            self.initiator.execute(f'kubectl delete namespace {namespace} --ignore-not-found')
        results = self.initiator.execute(f'kubectl get namespace {namespace} || kubectl create namespace {namespace}')[0]
        return results

    def create_nvmesh_mgmt_secret(self, delete_first=True, namespace='nvmesh-csi', item_type='generic', item='nvmesh-csi-credentials', username='csi@nvidia.com', password='admin'):
        if delete_first:
            self.initiator.execute(f'kubectl delete secret -n {namespace} {item} --ignore-not-found')
        results = self.initiator.execute(f'kubectl get secrets -n {namespace} {item} || kubectl create secret -n {namespace} {item_type} {item} --from-literal=username={username} --from-literal=password={password}')[0]
        return results

    def nvmesh_csi_pods_are_running(self, namespace='nvmesh-csi', number_of_pods=None):
        if not number_of_pods:
            number_of_pods = int(self.initiator.execute(f'kubectl get pods -n {namespace} | wc -l')[0].strip()) - 1
        results = self.initiator.execute(f'kubectl get pods -n {namespace} | grep -c Running')[0]
        return str(results).strip() == str(number_of_pods)

    def restart_nvmesh_csi(self, daemonset='daemonset/nvmesh-csi-node-driver', statefulset='statefulset/nvmesh-csi-controller'):
        results = self.initiator.execute(f'kubectl rollout restart -n nvmesh-csi {daemonset} {statefulset}', success=0)[0]
        return results

    @staticmethod
    def get_volume_name_from_pvc(pvc_name: str, volume_list=None) -> Volume:
        """
        use to return nvmesh volume name by pvc name. returns first result only.
        """
        if not volume_list:
            volume_list = Manager.get_manager().volumes()
        for volume in volume_list:
            if 'csi_metadata' in volume:
                if volume.csi_metadata['csi-storage-k8s-io/pvc/name'] == pvc_name:
                    return volume

    def is_fs_type(self, volume: Union[Volume, str], fs_type: str):
        """
        return True if filesystem of 'volume' is of type 'fs_type' (xfs, ext4)
        """
        volume_name = volume.name if isinstance(volume, Volume) else volume
        if isinstance(volume_name, str):
            fs_type_result = Connection.execute_on_host(self.name, "sudo df --output=source,fstype | grep " + volume_name + " | awk '{print $2}'")
            return True if fs_type_result[0].strip() == fs_type else False

    def volume_is_of_access_mode(self, volume: Union[Volume, str], access_mode: str):
        """
        return True if 'volume' access_mode is as expected in the os
        TODO here to add support for UM client
        """
        volume_name = volume.name if isinstance(volume, Volume) else volume
        count_access = Connection.execute_on_host(self.name, f"cat /proc/nvmeibc/volumes/{volume_name}/status | grep {access_mode} -c")
        return True if count_access[0].strip() == '1' else False

    def is_encrypted(self, volume: Union[Volume, str]):
        """
        return True if volume is encrypted in the os  using cryptsetup
        """
        volume_name = volume.name if isinstance(volume, Volume) else volume
        if isinstance(volume_name, str):
            isluks_result = Connection.execute_on_host(self.name, f"sudo cryptsetup status {volume_name}")
            return False if 'inactive' in isluks_result[0].strip() else True

    def expand_csi_volume(self, pvc_resource: str, storage='100Gi'):
        """
        use to expand csi volumes by patching pvc resource
        """
        spec = '{"spec": {"resources": {"requests": {"storage": "' + storage + '"}}}}'
        self.initiator.execute(f"kubectl patch pvc {pvc_resource} -p '{spec}'")

    def pod_file_md5sum(self, pod_name, partition_name='/mnt/nvmesh/', filename_pattern='fio*', expacted_md5=None):
        """
        check md5sum, function is under construction and neither implemented nor tested
        """
        sed_arg = '"s/ .*//"'
        cmd = f'kubectl exec -it {pod_name} -- sh -c "md5sum {partition_name}{filename_pattern} | sed {sed_arg}"'
        cmd_result = self.initiator.execute(cmd)[0]
        if expacted_md5:
            return cmd_result == expacted_md5
        return cmd_result

    def pod_partition_size(self, pod_name, partition_name='/mnt/nvmesh', expacted_size=None):
        """
        check a partition (fs volume) size inside a pod
        """
        awk_arg = "'NR==2 { print \$2 }'"
        cmd = f'kubectl exec -it {pod_name} -- sh -c "df -P {partition_name} | awk {awk_arg}"'
        cmd_result = self.initiator.execute(cmd)[0]
        if expacted_size:
            actual_kb = int(cmd_result.strip())
            actual_gib = actual_kb / (1024 ** 2)
            min_expected = expacted_size * 0.93
            return actual_gib >= min_expected
        return cmd_result

    def apply_resources(self, name, *resources_conf):
        """
        apply json resource(s) to k8s
        """
        resources_file = Connection.err2exc(execute_cmd_locally(f'mktemp -t {name}_XXXXX.json')).strip()
        with open(resources_file, 'a') as fp:
            for resource in resources_conf:
                if resource:
                    json.dump(resource, fp, indent=2)
                    fp.write('\n')

        self.initiator.connection.put_file(resources_file, resources_file)
        self.initiator.execute(f'kubectl apply -f {resources_file}', success=0)
        execute_cmd_locally(f"find {path.dirname(resources_file)} -type f -name {path.basename(resources_file)} -delete")
        return resources_file

    def delete_resources(self, resources_file):
        def stop_cmd(k, v, force=False):
            rc = f'kubectl delete {k} {v} {"--force" if force else "--grace-period=300"}'
            return rc

        for k in ['pod', 'pvc', 'pv']:
            try:
                if k in resources_file and resources_file[k]:
                    self.initiator.execute(stop_cmd(k, resources_file[k]), timeout=300)
            except Exception as e:
                self.logger.warning(f"Unable to shutdown pod(s) {resources_file} gracefully on {self.host} - {repr(e)}. Forcing deletion")
                self.initiator.execute(stop_cmd(k, resources_file[k], force=True), timeout=300)

    def unbind_volume(self, volume):
        self.delete_resources(self.bindings[volume])
        K8sClient.VOLUME2CLIENTS[volume].remove(self)
        if not K8sClient.VOLUME2CLIENTS.get(volume):
            with K8sClient.VCONF_LOCK:
                if not K8sClient.VOLUME2CLIENTS.get(volume):
                    self.delete_resources(K8sClient.VOLUME2CONF_PATH.pop(volume))

    def do_unbind(self, volumes: Iterable[Volume], *args, **kwargs) -> Iterable[Volume]:
        with ThreadPoolManager() as executor:
            for volume in volumes:
                if volume.csi_metadata['csi-storage-k8s-io/pvc/name']:
                    volume_name = volume.csi_metadata['csi-storage-k8s-io/pvc/name'] if volume.csi_metadata['csi-storage-k8s-io/pvc/name'] else volume.name
                executor.add_task(f"unbind {self._name}: {volume_name}", self.unbind_volume, volume)
            executor.start()

        failures = [(task, res.exception()) for task, res in executor.results.items() if res.exception()]
        assert not failures, f"Unbind failed for {failures}"
        return volumes

    def get_k8s_resources_list(self, resource_type, timeout=600):
        """
        get resource list by resource_type (pv, pvc, pod etc.)
        """
        results = self.initiator.execute(f'kubectl get {resource_type}', timeout=timeout)
        return results

    def clear_k8s_setup(self, resource_type, specific='--all', grace='300', timeout=600):
        """
        delete all resources, or specific one
        :resource_type: pv, pvc, pod etc.
        :specific: default is '--all' for all
        """
        try:
            self.initiator.execute(f'kubectl delete {resource_type} {specific}' + f' --grace-period={grace}' if grace else '', timeout=timeout)
        except Exception as err:
            pass

    def clear_k8s_storageclass(self, timeout=600):
        """
        clear all custom storageclass (not starting with nvmesh-)
        """
        try:
            self.initiator.execute("for sc in $(kubectl get storageclass -o name | sed 's|storageclass.storage.k8s.io/||'); do [[ $sc != nvmesh-* ]] && kubectl delete storageclass $sc; done", timeout=timeout)
        except:
            pass

    def get_nvmesh_csi_logs(self, namespace='nvmesh-csi', pod='nvmesh-csi-controller-0', lines=20):
        try:
            out = self.initiator.execute(f'kubectl logs -n {namespace} {pod} | tail -n {lines}')[0]
        except Exception as err:
            out = f"Error getting kubectl logs: {err}"
        return out
