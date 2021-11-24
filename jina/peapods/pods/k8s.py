import copy
import os
import time
from argparse import Namespace
from typing import Optional, Dict, Union, Set, List, Iterable

import jina
from .k8slib import kubernetes_deployment, kubernetes_client
from ..networking import K8sGrpcConnectionPool
from ..pods import BasePod
from ... import __default_executor__
from ...enums import PeaRoleType
from ...logging.logger import JinaLogger
from ...excepts import RuntimeFailToStart


class K8sPod(BasePod):
    """The K8sPod (KubernetesPod)  is used for deployments on Kubernetes."""

    class _K8sDeployment:
        def __init__(
            self,
            name: str,
            head_port_in: int,
            version: str,
            pea_type: str,
            jina_pod_name: str,
            shard_id: Optional[int],
            common_args: Union['Namespace', Dict],
            deployment_args: Union['Namespace', Dict],
        ):
            self.name = name
            self.dns_name = kubernetes_deployment.to_dns_name(name)
            self.head_port_in = head_port_in
            self.version = version
            self.pea_type = pea_type
            self.jina_pod_name = jina_pod_name
            self.shard_id = shard_id
            self.common_args = common_args
            self.deployment_args = deployment_args
            self.k8s_namespace = self.common_args.k8s_namespace
            self.num_replicas = getattr(self.deployment_args, 'replicas', 1)
            self.cluster_address = None

        def _deploy_gateway(self):
            test_pip = os.getenv('JINA_K8S_USE_TEST_PIP') is not None
            image_name = (
                'jinaai/jina:test-pip'
                if test_pip
                else f'jinaai/jina:{self.version}-py38-standard'
            )
            self.cluster_address = kubernetes_deployment.deploy_service(
                self.dns_name,
                namespace=self.k8s_namespace,
                image_name=image_name,
                container_cmd='["jina"]',
                container_args=f'["gateway", '
                f'{kubernetes_deployment.get_cli_params(arguments=self.common_args, skip_list=("pod_role",))}]',
                logger=JinaLogger(f'deploy_{self.name}'),
                replicas=1,
                pull_policy='IfNotPresent',
                jina_pod_name='gateway',
                pea_type='gateway',
                port_expose=self.common_args.port_expose,
            )

        @staticmethod
        def _construct_runtime_container_args(
            deployment_args, uses, uses_metas, uses_with_string, pea_type, port_in
        ):
            container_args = (
                f'["executor", '
                f'"--native", '
                f'"--uses", "{uses}", '
                f'"--runtime-cls", {"WorkerRuntime" if pea_type.lower() == "worker" else "HeadRuntime"}, '
                f'"--uses-metas", "{uses_metas}", '
                + uses_with_string
                + f'{kubernetes_deployment.get_cli_params(arguments=deployment_args, port_in=port_in)}]'
            )
            return container_args

        def _get_image_name(self, uses: str):
            image_name = kubernetes_deployment.get_image_name(uses)
            if image_name == __default_executor__:
                test_pip = os.getenv('JINA_K8S_USE_TEST_PIP') is not None
                image_name = (
                    'jinaai/jina:test-pip'
                    if test_pip
                    else f'jinaai/jina:{self.version}-py38-perf'
                )

            return image_name

        def _get_init_container_args(self):
            return kubernetes_deployment.get_init_container_args(self.common_args)

        def _get_container_args(self, uses, pea_type=None, port_in=None):
            uses_metas = kubernetes_deployment.dictionary_to_cli_param(
                {'pea_id': self.shard_id}
            )
            uses_with = kubernetes_deployment.dictionary_to_cli_param(
                self.deployment_args.uses_with
            )
            uses_with_string = f'"--uses-with", "{uses_with}", ' if uses_with else ''
            if uses != __default_executor__:
                uses = 'config.yml'
            return self._construct_runtime_container_args(
                self.deployment_args,
                uses,
                uses_metas,
                uses_with_string,
                pea_type if pea_type else self.pea_type,
                port_in,
            )

        def _deploy_runtime(
            self,
            replace=False,
        ):
            image_name = self._get_image_name(self.deployment_args.uses)
            image_name_uses_before = (
                self._get_image_name(self.deployment_args.uses_before)
                if hasattr(self.deployment_args, 'uses_before')
                and self.deployment_args.uses_before
                else None
            )
            image_name_uses_after = (
                self._get_image_name(self.deployment_args.uses_after)
                if hasattr(self.deployment_args, 'uses_after')
                and self.deployment_args.uses_after
                else None
            )
            init_container_args = self._get_init_container_args()
            container_args = self._get_container_args(self.deployment_args.uses)
            container_args_uses_before = (
                self._get_container_args(
                    self.deployment_args.uses_before,
                    'worker',
                    port_in=K8sGrpcConnectionPool.K8S_PORT_USES_BEFORE,
                )
                if hasattr(self.deployment_args, 'uses_before')
                and self.deployment_args.uses_before
                else None
            )
            container_args_uses_after = (
                self._get_container_args(
                    self.deployment_args.uses_after,
                    'worker',
                    port_in=K8sGrpcConnectionPool.K8S_PORT_USES_AFTER,
                )
                if hasattr(self.deployment_args, 'uses_after')
                and self.deployment_args.uses_after
                else None
            )

            self.cluster_address = kubernetes_deployment.deploy_service(
                self.dns_name,
                namespace=self.k8s_namespace,
                image_name=image_name,
                image_name_uses_after=image_name_uses_after,
                image_name_uses_before=image_name_uses_before,
                container_cmd='["jina"]',
                container_cmd_uses_before='["jina"]',
                container_cmd_uses_after='["jina"]',
                container_args=container_args,
                container_args_uses_before=container_args_uses_before,
                container_args_uses_after=container_args_uses_after,
                logger=JinaLogger(f'deploy_{self.name}'),
                replicas=self.num_replicas,
                pull_policy='IfNotPresent',
                jina_pod_name=self.jina_pod_name,
                pea_type=self.pea_type,
                shard_id=self.shard_id,
                init_container=init_container_args,
                env=self.deployment_args.env,
                gpus=self.deployment_args.gpus
                if hasattr(self.deployment_args, 'gpus')
                else None,
                custom_resource_dir=getattr(
                    self.common_args, 'k8s_custom_resource_dir', None
                ),
                replace_deployment=replace,
            )

        def _restart_runtime(self):
            self._deploy_runtime(replace=True)

        def wait_start_success(self):
            _timeout = self.common_args.timeout_ready
            if _timeout <= 0:
                _timeout = None
            else:
                _timeout /= 1e3

            from kubernetes import client

            with JinaLogger(f'waiting_for_{self.name}') as logger:
                logger.debug(
                    f'🏝️\n\t\tWaiting for "{self.name}" to be ready, with {self.num_replicas} replicas'
                )
                timeout_ns = 1000000000 * _timeout if _timeout else None
                now = time.time_ns()
                exception_to_raise = None
                while timeout_ns is None or time.time_ns() - now < timeout_ns:
                    try:
                        api_response = self._read_namespaced_deployment()

                        if (
                            api_response.status.ready_replicas is not None
                            and api_response.status.ready_replicas == self.num_replicas
                        ):
                            logger.success(f' {self.name} has all its replicas ready!!')
                            return
                        else:
                            ready_replicas = api_response.status.ready_replicas or 0
                            logger.debug(
                                f'\nNumber of ready replicas {ready_replicas}, waiting for {self.num_replicas - ready_replicas} replicas to be available for {self.name}'
                            )
                            time.sleep(1.0)
                    except client.ApiException as ex:
                        exception_to_raise = ex
                        break
            fail_msg = f' Deployment {self.name} did not start with a timeout of {self.common_args.timeout_ready}'
            if exception_to_raise:
                fail_msg += f': {repr(exception_to_raise)}'
            raise RuntimeFailToStart(fail_msg)

        async def wait_restart_success(self, previous_uids: Iterable[str] = None):
            _timeout = self.common_args.timeout_ready
            if _timeout <= 0:
                _timeout = None
            else:
                _timeout /= 1e3

            if previous_uids is None:
                previous_uids = []

            from kubernetes import client
            import asyncio

            k8s_client = kubernetes_client.K8sClients().apps_v1

            with JinaLogger(f'waiting_restart_for_{self.name}') as logger:
                logger.info(
                    f'🏝️\n\t\tWaiting for "{self.name}" to be restarted, with {self.num_replicas} replicas'
                )
                timeout_ns = 1000000000 * _timeout if _timeout else None
                now = time.time_ns()
                exception_to_raise = None
                while timeout_ns is None or time.time_ns() - now < timeout_ns:
                    try:
                        api_response = k8s_client.read_namespaced_deployment(
                            name=self.dns_name, namespace=self.k8s_namespace
                        )
                        logger.debug(
                            f'\n\t\t Updated Replicas: {api_response.status.updated_replicas}.'
                            f' Replicas: {api_response.status.replicas}.'
                            f' Expected Replicas {self.num_replicas}'
                        )

                        has_pod_with_uid = self._has_pod_with_uid(previous_uids)
                        if (
                            api_response.status.updated_replicas is not None
                            and api_response.status.updated_replicas
                            == self.num_replicas
                            and api_response.status.replicas == self.num_replicas
                            and not has_pod_with_uid
                        ):
                            logger.success(
                                f' {self.name} has all its replicas updated!!'
                            )
                            return
                        else:
                            updated_replicas = api_response.status.updated_replicas or 0
                            alive_replicas = api_response.status.replicas or 0
                            if updated_replicas < self.num_replicas:
                                logger.debug(
                                    f'\nNumber of updated replicas {updated_replicas}, waiting for {self.num_replicas - updated_replicas} replicas to be updated'
                                )
                            elif has_pod_with_uid:
                                logger.debug(
                                    f'\nWaiting for old replicas to be terminated'
                                )
                            else:
                                logger.debug(
                                    f'\nNumber of alive replicas {alive_replicas}, waiting for {alive_replicas - self.num_replicas} old replicas to be terminated'
                                )

                            await asyncio.sleep(1.0)
                    except client.ApiException as ex:
                        exception_to_raise = ex
                        break
            fail_msg = f' Deployment {self.name} did not restart with a timeout of {self.common_args.timeout_ready}'
            if exception_to_raise:
                fail_msg += f': {repr(exception_to_raise)}'
            raise RuntimeFailToStart(fail_msg)

        async def wait_scale_success(self, replicas: int):
            scale_to = replicas
            _timeout = self.common_args.timeout_ready
            if _timeout <= 0:
                _timeout = None
            else:
                _timeout /= 1e3

            import asyncio
            from kubernetes import client

            with JinaLogger(f'waiting_scale_for_{self.name}') as logger:
                logger.info(
                    f'🏝️\n\t\tWaiting for "{self.name}" to be scaled, with {self.num_replicas} replicas,'
                    f'scale to {scale_to}.'
                )
                timeout_ns = 1000000000 * _timeout if _timeout else None
                now = time.time_ns()
                exception_to_raise = None
                while timeout_ns is None or time.time_ns() - now < timeout_ns:
                    try:
                        api_response = kubernetes_client.K8sClients().apps_v1.read_namespaced_deployment(
                            name=self.dns_name, namespace=self.k8s_namespace
                        )
                        logger.debug(
                            f'\n\t\t Scaled replicas: {api_response.status.ready_replicas}.'
                            f' Replicas: {api_response.status.replicas}.'
                            f' Expected Replicas {scale_to}'
                        )
                        if (
                            api_response.status.ready_replicas is not None
                            and api_response.status.ready_replicas == scale_to
                        ):
                            logger.success(
                                f' {self.name} has all its replicas updated!!'
                            )
                            return
                        else:
                            scaled_replicas = api_response.status.ready_replicas or 0
                            if scaled_replicas < scale_to:
                                logger.debug(
                                    f'\nNumber of replicas {scaled_replicas}, waiting for {scale_to - scaled_replicas} replicas to be scaled up.'
                                )
                            else:
                                logger.debug(
                                    f'\nNumber of replicas {scaled_replicas}, waiting for {scaled_replicas - scale_to} replicas to be scaled down.'
                                )

                            await asyncio.sleep(1.0)
                    except client.ApiException as ex:
                        exception_to_raise = ex
                        break
            fail_msg = f' Deployment {self.name} did not restart with a timeout of {self.common_args.timeout_ready}'
            if exception_to_raise:
                fail_msg += f': {repr(exception_to_raise)}'
            raise RuntimeFailToStart(fail_msg)

        def rolling_update(
            self, dump_path: Optional[str] = None, *, uses_with: Optional[Dict] = None
        ):
            assert (
                self.name != 'gateway'
            ), 'Rolling update on the gateway is not supported'
            if dump_path is not None:
                if uses_with is not None:
                    uses_with['dump_path'] = dump_path
                else:
                    uses_with = {'dump_path': dump_path}
            self.deployment_args.uses_with = uses_with
            self.deployment_args.dump_path = dump_path
            self._restart_runtime()

        def scale(self, replicas: int):
            """
            Scale the amount of replicas of a given Executor.

            :param replicas: The number of replicas to scale to
            """
            self._patch_namespaced_deployment_scale(replicas)

        def start(self):
            with JinaLogger(f'start_{self.name}') as logger:
                logger.debug(f'\t\tDeploying "{self.name}"')
                if self.name == 'gateway':
                    self._deploy_gateway()
                else:
                    self._deploy_runtime()
                if not self.common_args.noblock_on_start:
                    self.wait_start_success()
            return self

        def close(self):
            from kubernetes import client

            with JinaLogger(f'close_{self.name}') as logger:
                try:
                    resp = self._delete_namespaced_deployment()
                    if resp.status == 'Success':
                        logger.success(
                            f' Successful deletion of deployment {self.name}'
                        )
                    else:
                        logger.error(
                            f' Deletion of deployment {self.name} unsuccessful with status {resp.status}'
                        )
                except client.ApiException as exc:
                    logger.error(
                        f' Error deleting deployment {self.name}: {exc.reason} '
                    )

        def _delete_namespaced_deployment(self):
            return kubernetes_client.K8sClients().apps_v1.delete_namespaced_deployment(
                name=self.dns_name, namespace=self.k8s_namespace
            )

        def _read_namespaced_deployment(self):
            return kubernetes_client.K8sClients().apps_v1.read_namespaced_deployment(
                name=self.dns_name, namespace=self.k8s_namespace
            )

        def _patch_namespaced_deployment_scale(self, replicas: int):
            kubernetes_client.K8sClients().apps_v1.patch_namespaced_deployment_scale(
                self.dns_name,
                namespace=self.k8s_namespace,
                body={'spec': {'replicas': replicas}},
            )

        def get_pod_uids(self) -> List[str]:
            """Get the UIDs for all Pods in this deployment

            :return: list of uids as strings for all pods in the deployment
            """
            pods = kubernetes_client.K8sClients().core_v1.list_namespaced_pod(
                namespace=self.k8s_namespace, label_selector=f'app={self.dns_name}'
            )

            return [item.metadata.uid for item in pods.items]

        def _has_pod_with_uid(self, uids: Iterable[str]) -> bool:
            """Check if this deployment has any Pod with a UID contained in uids

            :param uids: list of UIDs to check
            :return: True if any Pod has a UID in uids
            """
            current_pods_uids = self.get_pod_uids()

            return any(uid in current_pods_uids for uid in uids)

        def __enter__(self):
            return self.start()

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.close()

        def to_node(self):
            return {
                'name': self.dns_name,
                'head_host': f'{self.dns_name}.{self.k8s_namespace}.svc',
                'head_port_in': self.head_port_in,
            }

    def __init__(
        self, args: Union['Namespace', Dict], needs: Optional[Set[str]] = None
    ):
        super().__init__()
        self.args = args
        self.k8s_namespace = self.args.k8s_namespace
        self.needs = needs or set()
        self.deployment_args = self._parse_args(args)
        self.version = self._get_base_executor_version()
        self.k8s_head_deployment = None
        self.k8s_connection_pool = getattr(args, 'k8s_connection_pool', True)
        if self.deployment_args['head_deployment'] is not None:
            name = f'{self.name}-head'
            self.k8s_head_deployment = self._K8sDeployment(
                name=name,
                head_port_in=K8sGrpcConnectionPool.K8S_PORT_IN,
                version=self.version,
                shard_id=None,
                jina_pod_name=self.name,
                common_args=self.args,
                deployment_args=self.deployment_args['head_deployment'],
                pea_type='head',
            )

        self.k8s_deployments = []
        for i, args in enumerate(self.deployment_args['deployments']):
            name = (
                f'{self.name}-{i}'
                if len(self.deployment_args['deployments']) > 1
                else f'{self.name}'
            )
            self.k8s_deployments.append(
                self._K8sDeployment(
                    name=name,
                    head_port_in=K8sGrpcConnectionPool.K8S_PORT_IN,
                    version=self.version,
                    shard_id=i,
                    common_args=self.args,
                    deployment_args=args,
                    pea_type='worker',
                    jina_pod_name=self.name,
                )
            )

    def _parse_args(
        self, args: Namespace
    ) -> Dict[str, Optional[Union[List[Namespace], Namespace]]]:
        return self._parse_deployment_args(args)

    def _parse_deployment_args(self, args):
        parsed_args = {
            'head_deployment': None,
            'deployments': [],
        }
        shards = getattr(args, 'shards', 1)
        uses_before = getattr(args, 'uses_before', None)
        uses_after = getattr(args, 'uses_after', None)

        if args.name != 'gateway':
            parsed_args['head_deployment'] = copy.copy(args)
            parsed_args['head_deployment'].replicas = 1
            parsed_args['head_deployment'].runtime_cls = 'HeadRuntime'
            parsed_args['head_deployment'].pea_role = PeaRoleType.HEAD
            parsed_args['head_deployment'].port_in = K8sGrpcConnectionPool.K8S_PORT_IN

            # if the k8s connection pool is disabled, the connection pool is managed manually
            if not args.k8s_connection_pool:
                connection_list = '{'
                for i in range(shards):
                    name = f'{self.name}-{i}' if shards > 1 else f'{self.name}'
                    connection_list += f'"{str(i)}": "{name}.{self.k8s_namespace}.svc:{K8sGrpcConnectionPool.K8S_PORT_IN}",'
                connection_list = connection_list[:-1]
                connection_list += '}'

                parsed_args['head_deployment'].connection_list = connection_list

        if uses_before:
            parsed_args[
                'head_deployment'
            ].uses_before_address = (
                f'127.0.0.1:{K8sGrpcConnectionPool.K8S_PORT_USES_BEFORE}'
            )
        if uses_after:
            parsed_args[
                'head_deployment'
            ].uses_after_address = (
                f'127.0.0.1:{K8sGrpcConnectionPool.K8S_PORT_USES_AFTER}'
            )

        for i in range(shards):
            cargs = copy.deepcopy(args)
            cargs.shard_id = i
            cargs.uses_before = None
            cargs.uses_after = None
            cargs.port_in = K8sGrpcConnectionPool.K8S_PORT_IN
            if args.name == 'gateway':
                cargs.pea_role = PeaRoleType.GATEWAY
            parsed_args['deployments'].append(cargs)

        return parsed_args

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        self.join()

    @property
    def port_expose(self) -> int:
        """Not implemented"""
        raise NotImplementedError

    @property
    def host(self) -> str:
        """Currently, when deploying on Kubernetes, Jina does not expose a public host.
        Instead Jina sends requests via port-forward and runs the requests against localhost.

        :return: localhost
        """
        return 'localhost'

    async def rolling_update(
        self, dump_path: Optional[str] = None, *, uses_with: Optional[Dict] = None
    ):
        """Reload all Deployments of this K8s Pod.
        :param dump_path: **backwards compatibility** This function was only accepting dump_path as the only potential arg to override
        :param uses_with: a Dictionary of arguments to restart the executor with
        """
        old_uids = {}
        for deployment in self.k8s_deployments:
            old_uids[deployment.dns_name] = deployment.get_pod_uids()
            deployment.rolling_update(dump_path=dump_path, uses_with=uses_with)

        for deployment in self.k8s_deployments:
            await deployment.wait_restart_success(old_uids[deployment.dns_name])

    async def scale(self, replicas: int):
        """
        Scale the amount of replicas of a given Executor.

        :param replicas: The number of replicas to scale to
        """
        for deployment in self.k8s_deployments:
            deployment.scale(replicas=replicas)
        for deployment in self.k8s_deployments:
            await deployment.wait_scale_success(replicas=replicas)
            deployment.num_replicas = replicas

    def start(self) -> 'K8sPod':
        """Deploy the kubernetes pods via k8s Deployment and k8s Service.

        :return: self
        """
        with JinaLogger(f'start_{self.name}') as logger:
            logger.debug(f'🏝️\tCreate deployments for "{self.name}"')
            if self.k8s_head_deployment is not None:
                self.enter_context(self.k8s_head_deployment)
            for k8s_deployment in self.k8s_deployments:
                self.enter_context(k8s_deployment)
        return self

    def wait_start_success(self):
        """Not implemented. It should wait until the deployment is up and running"""
        if not self.args.noblock_on_start:
            raise ValueError(
                f'{self.wait_start_success!r} should only be called when `noblock_on_start` is set to True'
            )
        try:
            if self.k8s_head_deployment is not None:
                self.k8s_head_deployment.wait_start_success()
            for p in self.k8s_deployments:
                p.wait_start_success()
        except:
            self.close()
            raise

    def join(self):
        """Not needed. The context managers will manage the proper deletion"""
        pass

    def update_pea_args(self):
        """
        Regenerate deployment args
        """
        self.deployment_args = self._parse_args(self.args)

    @property
    def head_args(self) -> Namespace:
        """Head args of the pod.

        :return: namespace
        """
        return self.args

    @property
    def num_peas(self) -> int:
        """Number of peas. Currently unused.

        :return: number of peas
        """
        return sum(
            [
                self.k8s_head_deployment.num_replicas
                if self.k8s_head_deployment is not None
                else 0
            ]
            + [k8s_deployment.num_replicas for k8s_deployment in self.k8s_deployments]
        )

    @property
    def deployments(self) -> List[Dict]:
        """Deployment information which describes the interface of the pod.

        :return: list of dictionaries defining the attributes used by the routing table
        """
        res = []

        if self.args.name == 'gateway':
            res.append(self.k8s_deployments[0].to_node())
        else:
            if self.k8s_head_deployment:
                res.append(self.k8s_head_deployment.to_node())
            res.extend([_.to_node() for _ in self.k8s_deployments])
        return res

    def _get_base_executor_version(self):
        import requests

        url = 'https://registry.hub.docker.com/v1/repositories/jinaai/jina/tags'
        tags = requests.get(url).json()
        name_set = {tag['name'] for tag in tags}
        if jina.__version__ in name_set:
            return jina.__version__
        else:
            return 'master'

    @property
    def _mermaid_str(self) -> List[str]:
        """String that will be used to represent the Pod graphically when `Flow.plot()` is invoked


        .. # noqa: DAR201
        """
        mermaid_graph = []
        if self.name != 'gateway':
            mermaid_graph = [f'subgraph {self.name};\n', f'direction LR;\n']

            num_replicas = getattr(self.args, 'replicas', 1)
            num_shards = getattr(self.args, 'shards', 1)
            uses = self.args.uses
            if num_shards > 1:
                shard_names = [
                    f'{args.name}/shard-{i}'
                    for i, args in enumerate(self.deployment_args['deployments'])
                ]
                for shard_name in shard_names:
                    shard_mermaid_graph = [
                        f'subgraph {shard_name}\n',
                        f'direction TB;\n',
                    ]
                    for replica_id in range(num_replicas):
                        shard_mermaid_graph.append(
                            f'{shard_name}/replica-{replica_id}[{uses}]\n'
                        )
                    shard_mermaid_graph.append(f'end\n')
                    mermaid_graph.extend(shard_mermaid_graph)
                head_name = f'{self.name}/head'
                head_to_show = self.args.uses_before
                if head_to_show is None or head_to_show == __default_executor__:
                    head_to_show = head_name
                if head_name:
                    for shard_name in shard_names:
                        mermaid_graph.append(
                            f'{head_name}[{head_to_show}]:::HEADTAIL --> {shard_name}[{uses}];'
                        )
            else:
                for replica_id in range(num_replicas):
                    mermaid_graph.append(f'{self.name}/replica-{replica_id}[{uses}];')

            mermaid_graph.append(f'end;')
        return mermaid_graph
