# -*- coding: utf-8 -*-

#    Copyright 2013 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock
from oslo_serialization import jsonutils

from nailgun import consts
from nailgun.db.sqlalchemy.models import Cluster
from nailgun.db.sqlalchemy.models import DeploymentGraph
from nailgun.db.sqlalchemy.models import NetworkGroup
from nailgun.db.sqlalchemy.models import Node
from nailgun import errors
from nailgun.test.base import BaseIntegrationTest
from nailgun.test.base import fake_tasks
from nailgun.test.utils import make_mock_extensions
from nailgun.utils import reverse


class TestHandlers(BaseIntegrationTest):

    def delete(self, cluster_id):
        return self.app.delete(
            reverse('ClusterHandler', kwargs={'obj_id': cluster_id}),
            headers=self.default_headers
        )

    def test_cluster_get(self):
        cluster = self.env.create_cluster(api=False)
        resp = self.app.get(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            headers=self.default_headers
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual(cluster.id, resp.json_body['id'])
        self.assertEqual(cluster.name, resp.json_body['name'])
        self.assertEqual(cluster.release.id, resp.json_body['release_id'])

    def test_cluster_creation(self):
        release = self.env.create_release(api=False)
        yet_another_cluster_name = 'Yet another cluster'
        resp = self.app.post(
            reverse('ClusterCollectionHandler'),
            params=jsonutils.dumps({
                'name': yet_another_cluster_name,
                'release': release.id
            }),
            headers=self.default_headers
        )
        self.assertEqual(201, resp.status_code)
        self.assertEqual(yet_another_cluster_name, resp.json_body['name'])
        self.assertEqual(release.id, resp.json_body['release_id'])

    def test_cluster_update(self):
        updated_name = u'Updated cluster'
        cluster = self.env.create_cluster(api=False)

        clusters_before = len(self.db.query(Cluster).all())

        resp = self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'name': updated_name}),
            headers=self.default_headers
        )
        self.db.refresh(cluster)
        self.assertEqual(resp.status_code, 200)
        clusters = self.db.query(Cluster).filter(
            Cluster.name == updated_name
        ).all()
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].name, updated_name)

        clusters_after = len(self.db.query(Cluster).all())
        self.assertEqual(clusters_before, clusters_after)

    def test_cluster_update_fails_on_net_provider_change(self):
        cluster = self.env.create_cluster(
            api=False,
            net_provider=consts.CLUSTER_NET_PROVIDERS.nova_network)
        resp = self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'net_provider': 'neutron'}),
            headers=self.default_headers,
            expect_errors=True
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json_body["message"],
            "Changing 'net_provider' for environment is prohibited"
        )

    def test_cluster_node_list_update(self):
        node1 = self.env.create_node(api=False, hostname='name1')
        cluster = self.env.create_cluster(api=False)
        resp = self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'nodes': [node1.id]}),
            headers=self.default_headers,
            expect_errors=True
        )
        self.assertEqual(resp.status_code, 200)
        node2 = self.env.create_node(api=False, hostname='name1')

        nodes = self.db.query(Node).filter(Node.cluster == cluster).all()
        self.assertEqual(1, len(nodes))
        self.assertEqual(nodes[0].id, node1.id)

        resp = self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'nodes': [node2.id]}),
            headers=self.default_headers
        )
        self.assertEqual(resp.status_code, 200)

        self.assertEqual('node-{0}'.format(node1.id), node1.hostname)

        nodes = self.db.query(Node).filter(Node.cluster == cluster)
        self.assertEqual(1, nodes.count())

    def test_cluster_node_list_update_error(self):
        node1 = self.env.create_node(api=False, hostname='name1')
        cluster = self.env.create_cluster(api=False)
        self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'nodes': [node1.id]}),
            headers=self.default_headers,
            expect_errors=True
        )
        node2 = self.env.create_node(api=False, hostname='name1')

        # try to add to cluster one more node with the same hostname
        resp = self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'nodes': [node1.id, node2.id]}),
            headers=self.default_headers,
            expect_errors=True
        )
        self.assertEqual(resp.status_code, 409)

    def test_empty_cluster_deletion(self):
        cluster = self.env.create_cluster(api=True)
        resp = self.delete(cluster['id'])

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.db.query(Node).count(), 0)
        self.assertEqual(self.db.query(Cluster).count(), 0)

    @fake_tasks()
    def test_cluster_deletion(self):
        cluster = self.env.create(
            cluster_kwargs={},
            nodes_kwargs=[
                {"pending_addition": True},
                {"status": "ready"}])

        graphs_before_deletion = self.db.query(DeploymentGraph).count()
        cluster_id = self.env.clusters[0].id
        resp = self.delete(cluster_id)
        self.assertEqual(resp.status_code, 202)

        self.assertIsNone(self.db.query(Cluster).get(cluster.id))

        graphs_after_deletion = self.db.query(DeploymentGraph).count()
        self.assertEqual(1, graphs_before_deletion - graphs_after_deletion)

        # Nodes should be in discover status
        self.assertEqual(self.db.query(Node).count(), 2)
        for node in self.db.query(Node):
            self.assertEqual(node.status, 'discover')
            self.assertIsNone(node.cluster_id)
            self.assertIsNone(node.group_id)
            self.assertEqual(node.roles, [])
            self.assertFalse(node.pending_deletion)
            self.assertFalse(node.pending_addition)

    @fake_tasks(recover_offline_nodes=False)
    def test_cluster_deletion_with_offline_nodes(self):
        cluster = self.env.create(
            cluster_kwargs={},
            nodes_kwargs=[
                {'pending_addition': True},
                {'online': False, 'status': 'ready'}])

        resp = self.delete(cluster.id)
        self.assertEqual(resp.status_code, 202)

        self.assertIsNone(self.db.query(Cluster).get(cluster.id))
        self.assertEqual(self.db.query(Node).count(), 1)

        node = self.db.query(Node).first()
        self.assertEqual(node.status, 'discover')
        self.assertIsNone(node.cluster_id)

    def test_cluster_deletion_delete_networks(self):
        cluster = self.env.create_cluster(api=True)
        cluster_db = self.db.query(Cluster).get(cluster['id'])
        ngroups = [n.id for n in cluster_db.network_groups]
        self.db.delete(cluster_db)
        self.db.commit()
        ngs = self.db.query(NetworkGroup).filter(
            NetworkGroup.id.in_(ngroups)
        ).all()
        self.assertEqual(ngs, [])

    def test_cluster_generated_data_handler(self):
        cluster = self.env.create(
            nodes_kwargs=[
                {'pending_addition': True},
                {'online': False, 'status': 'ready'}])
        get_resp = self.app.get(
            reverse('ClusterGeneratedData',
                    kwargs={'cluster_id': cluster.id}),
            headers=self.default_headers
        )
        self.assertEqual(get_resp.status_code, 200)
        self.datadiff(get_resp.json_body, cluster.attributes.generated)

    def test_cluster_name_length(self):
        long_name = u'ю' * 2048
        cluster = self.env.create_cluster(api=False)

        resp = self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster.id}),
            jsonutils.dumps({'name': long_name}),
            headers=self.default_headers
        )
        self.assertEqual(resp.status_code, 200)
        self.db.refresh(cluster)
        self.assertEqual(long_name, cluster.name)


class TestClusterModes(BaseIntegrationTest):

    def test_fail_to_create_cluster_with_multinode_mode(self):
        release = self.env.create_release(
            version='2015-7.0',
            modes=[consts.CLUSTER_MODES.ha_compact],
        )
        cluster_data = {
            'name': 'CrazyFrog',
            'release_id': release.id,
            'mode': consts.CLUSTER_MODES.multinode,
        }

        resp = self.app.post(
            reverse('ClusterCollectionHandler'),
            jsonutils.dumps(cluster_data),
            headers=self.default_headers,
            expect_errors=True
        )
        self.check_wrong_response(resp)

    def check_wrong_response(self, resp):
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            'Cannot deploy in multinode mode in current release. '
            'Need to be one of',
            resp.json_body['message']
        )

    def test_update_cluster_to_wrong_mode(self):
        update_resp = self._try_cluster_update(
            name='SadCrazyFrog',
            mode=consts.CLUSTER_MODES.multinode,
        )
        self.check_wrong_response(update_resp)

    def test_update_cluster_but_not_mode(self):
        update_resp = self._try_cluster_update(
            name='HappyCrazyFrog',
        )
        self.assertEqual(update_resp.status_code, 200)

    def _try_cluster_update(self, **attrs_to_update):
        release = self.env.create_release(
            version='2015-7.0',
            modes=[consts.CLUSTER_MODES.ha_compact],
        )
        create_resp = self.env.create_cluster(
            release_id=release.id,
            mode=consts.CLUSTER_MODES.ha_compact,
            api=True,
        )
        cluster_id = create_resp['id']

        return self.app.put(
            reverse('ClusterHandler', kwargs={'obj_id': cluster_id}),
            jsonutils.dumps(attrs_to_update),
            headers=self.default_headers,
            expect_errors=True
        )


class TestClusterComponents(BaseIntegrationTest):

    def setUp(self):
        super(TestClusterComponents, self).setUp()
        self.release = self.env.create_release(
            version='2015.1-8.0',
            operating_system='Ubuntu',
            modes=[consts.CLUSTER_MODES.ha_compact],
            components_metadata=[
                {
                    'name': 'hypervisor:test_hypervisor'
                },
                {
                    'name': 'network:core:test_network_1',
                    'incompatible': [
                        {'name': 'hypervisor:test_hypervisor'}
                    ]
                },
                {
                    'name': 'network:core:test_network_2'
                },
                {
                    'name': 'storage:test_storage',
                    'compatible': [
                        {'name': 'hypervisors:test_hypervisor'}
                    ],
                    'requires': [
                        {'name': 'hypervisors:test_hypervisor'}
                    ]
                }
            ])

        self.cluster_data = {
            'name': 'TestCluster',
            'release_id': self.release.id,
            'mode': consts.CLUSTER_MODES.ha_compact
        }

    def test_component_validation_failed(self):
        error_msg = "Component validation error"
        self.cluster_data.update(
            {'components': ['hypervisor:test_hypervisor']})
        with mock.patch('nailgun.utils.restrictions.ComponentsRestrictions.'
                        'validate_components') as validate_mock:
            validate_mock.side_effect = errors.InvalidData(error_msg)
            resp = self._create_cluster_with_expected_errors(self.cluster_data)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(error_msg, resp.json_body['message'])

    def test_components_not_in_release(self):
        self.cluster_data.update(
            {'components': ['storage:not_existing_component']})
        resp = self._create_cluster_with_expected_errors(self.cluster_data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            u"[u'storage:not_existing_component'] components are not "
            "related to used release.",
            resp.json_body['message']
        )

    def test_incompatible_components_found(self):
        self.cluster_data.update(
            {'components': [
                'hypervisor:test_hypervisor',
                'network:core:test_network_1']})
        resp = self._create_cluster_with_expected_errors(self.cluster_data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            u"Incompatible components were found: "
            "'hypervisor:test_hypervisor' incompatible with "
            "[u'network:core:test_network_1'].",
            resp.json_body['message']
        )

    def test_requires_components_not_found(self):
        self.cluster_data.update(
            {'components': ['storage:test_storage']})
        resp = self._create_cluster_with_expected_errors(self.cluster_data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            u"Component 'storage:test_storage' requires any of components "
            "from [u'hypervisors:test_hypervisor'] set.",
            resp.json_body['message']
        )

    def _create_cluster_with_expected_errors(self, cluster_data):
        return self.app.post(
            reverse('ClusterCollectionHandler'),
            jsonutils.dumps(cluster_data),
            headers=self.default_headers,
            expect_errors=True
        )


class TestClusterExtension(BaseIntegrationTest):

    def setUp(self):
        super(TestClusterExtension, self).setUp()
        self.env.create_cluster()
        self.cluster = self.env.clusters[0]

    def test_get_enabled_extensions(self):
        enabled_extensions = 'volume_manager', 'bareon'
        self.cluster.extensions = enabled_extensions
        self.db.commit()

        resp = self.app.get(
            reverse(
                'ClusterExtensionsHandler',
                kwargs={'cluster_id': self.cluster.id}),
            headers=self.default_headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertItemsEqual(resp.json_body, enabled_extensions)

    def test_enabling_duplicated_extensions(self):
        extensions = 'bareon', 'volume_manager'
        requested_extensions = 2 * extensions

        with mock.patch(
                'nailgun.api.v1.validators.extension.get_all_extensions',
                return_value=make_mock_extensions(extensions)):
            resp = self.app.put(
                reverse(
                    'ClusterExtensionsHandler',
                    kwargs={'cluster_id': self.cluster.id}),
                jsonutils.dumps(requested_extensions),
                headers=self.default_headers,
            )
        self.assertEqual(resp.status_code, 200)

        self.db.refresh(self.cluster)
        for ext in extensions:
            self.assertIn(ext, self.cluster.extensions)

    def test_enabling_extensions(self):
        extensions = 'bareon', 'volume_manager'

        with mock.patch(
                'nailgun.api.v1.validators.extension.get_all_extensions',
                return_value=make_mock_extensions(extensions)):
            resp = self.app.put(
                reverse(
                    'ClusterExtensionsHandler',
                    kwargs={'cluster_id': self.cluster.id}),
                jsonutils.dumps(extensions),
                headers=self.default_headers,
            )
        self.assertEqual(resp.status_code, 200)

        self.db.refresh(self.cluster)
        for ext in extensions:
            self.assertIn(ext, self.cluster.extensions)

    def test_enabling_invalid_extensions(self):
        existed_extensions = 'bareon', 'volume_manager'
        requested_extensions = 'network_manager', 'volume_manager'

        with mock.patch(
                'nailgun.api.v1.validators.extension.get_all_extensions',
                return_value=make_mock_extensions(existed_extensions)):
            resp = self.app.put(
                reverse(
                    'ClusterExtensionsHandler',
                    kwargs={'cluster_id': self.cluster.id}),
                jsonutils.dumps(requested_extensions),
                headers=self.default_headers,
                expect_errors=True,
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(u"No such extensions:", resp.json_body['message'])
        self.assertIn(requested_extensions[0], resp.json_body['message'])
        self.assertNotIn(requested_extensions[1], resp.json_body['message'])

    def test_disabling_invalid_extensions(self):
        requested_extensions = 'network_manager', 'bareon'

        self.cluster.extensions = 'volume_manager', 'bareon'
        self.db.commit()
        url = reverse('ClusterExtensionsHandler',
                      kwargs={'cluster_id': self.cluster.id})
        query_str = 'extension_names={0}'.format(
            ','.join(requested_extensions))

        resp = self.app.delete(
            '{0}?{1}'.format(url, query_str),
            headers=self.default_headers,
            expect_errors=True,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(u"No such extensions to disable:",
                      resp.json_body['message'])
        self.assertIn(requested_extensions[0], resp.json_body['message'])
        self.assertNotIn(requested_extensions[1], resp.json_body['message'])

    def test_disabling_extensions(self):
        existed_extensions = 'network_manager', 'volume_manager', 'bareon'
        requested_extensions = 'network_manager', 'bareon'

        self.cluster.extensions = existed_extensions
        self.db.commit()
        url = reverse('ClusterExtensionsHandler',
                      kwargs={'cluster_id': self.cluster.id})
        query_str = 'extension_names={0}'.format(
            ','.join(requested_extensions))

        self.app.delete(
            '{0}?{1}'.format(url, query_str),
            headers=self.default_headers,
        )

        self.db.refresh(self.cluster)
        for ext in requested_extensions:
            self.assertNotIn(ext, self.cluster.extensions)

    def test_disabling_dublicated_extensions(self):
        existed_extensions = 'network_manager', 'volume_manager', 'bareon'
        requested_extensions = 'network_manager', 'bareon'

        self.cluster.extensions = existed_extensions
        self.db.commit()
        url = reverse('ClusterExtensionsHandler',
                      kwargs={'cluster_id': self.cluster.id})
        query_str = 'extension_names={0}'.format(
            ','.join(2 * requested_extensions))

        self.app.delete(
            '{0}?{1}'.format(url, query_str),
            headers=self.default_headers,
        )

        self.db.refresh(self.cluster)
        for ext in requested_extensions:
            self.assertNotIn(ext, self.cluster.extensions)
