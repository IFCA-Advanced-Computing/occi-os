# coding=utf-8
# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Copyright (c) 2012, Intel Performance Learning Solutions Ltd.
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

"""
OCCI registry
"""

#R0201:method could be func.E1002:old style obj,R0914-R0912:# of branches
#E1121:# positional args.
#pylint: disable=R0201,E1002,R0914,R0912,E1121

from oslo.config import cfg

from occi_os_api.backends import openstack
from occi_os_api.extensions import os_addon

from occi_os_api.nova_glue import vm
from occi_os_api.nova_glue import storage
from occi_os_api.nova_glue import net

from occi import registry as occi_registry
from occi import core_model
from occi.extensions import infrastructure
from occi import exceptions

CONF = cfg.CONF

from nova.openstack.common import log
LOG = log.getLogger(__name__)


class OCCIRegistry(occi_registry.NonePersistentRegistry):
    """
    Registry for OpenStack.

    Idea is the following: Create the OCCI entities (Resource and their
    links) here and let the backends handle actions, attributes etc.
    """

    def __init__(self):
        super(OCCIRegistry, self).__init__()
        self.cache = {}
        self.nets = {
            'admin': core_model.Resource('/network/admin',
                                         infrastructure.NETWORK,
                                         [infrastructure.IPNETWORK]),
            'public': core_model.Resource('/network/public',
                                          infrastructure.NETWORK,
                                          [infrastructure.IPNETWORK])
        }
        self._setup_network()

    def set_hostname(self, hostname):
        if CONF.occi_custom_location_hostname:
            hostname = CONF.occi_custom_location_hostname
        super(OCCIRegistry, self).set_hostname(hostname)

    def get_extras(self, extras):
        """
        Get data which is encapsulated in the extras.
        """
        sec_extras = None
        if extras is not None:
            sec_extras = {'user_id': extras['nova_ctx'].user_id,
                          'project_id': extras['nova_ctx'].project_id}
        return sec_extras

    def delete_resource(self, key, extras):
        """
        Avoid super messing
        """
        pass

    def add_resource(self, key, resource, extras):
        """
        Avoid super messing
        """
        pass

    # The following two are here to deal with the security group mixins
    def delete_mixin(self, mixin, extras):
        """
        Allows for the deletion of user defined mixins.
        If the mixin is a security group mixin then that mixin's
        backend is called.
        """
        if (hasattr(mixin, 'related') and
                os_addon.SEC_GROUP in mixin.related):
            backend = self.get_backend(mixin, extras)
            backend.destroy(mixin, extras)

        super(OCCIRegistry, self).delete_mixin(mixin, extras)

    def set_backend(self, category, backend, extras):
        """
        Assigns user id and tenant id to user defined mixins
        """
        if (hasattr(category, 'related') and
                os_addon.SEC_GROUP in category.related):
            backend = openstack.SecurityGroupBackend()
            backend.init_sec_group(category, extras)

        super(OCCIRegistry, self).set_backend(category, backend, extras)

    def _construct_occi_storage(self, vol_desc, extras):
        """
        Construct a OCCI storage instance.
        """
        iden = infrastructure.STORAGE.location + vol_desc['id']
        entity = core_model.Resource(iden, infrastructure.STORAGE, [])
        entity.attributes['occi.core.id'] = vol_desc['id']
        entity.extras = self.get_extras(extras)
        return entity

    def _construct_storage_link(self, vol_desc, source, extras):
        """
        Construct a storage link
        """
        target = self._construct_occi_storage(vol_desc, extras)
        link_id = '_'.join([source.attributes['occi.core.id'],
                            target.attributes['occi.core.id']])
        link = core_model.Link(infrastructure.STORAGELINK.location +
                               link_id,
                               infrastructure.STORAGELINK, [], source,
                               target)
        link.extras = self.get_extras(extras)
        link.attributes['occi.storagelink.deviceid'] = vol_desc['mountpoint']
        return link

    def _construct_occi_compute(self, instance, extras):
        context = extras['nova_ctx']
        # 1. create entity
        iden = infrastructure.COMPUTE.location + instance['uuid']
        entity = core_model.Resource(iden, infrastructure.COMPUTE,
                                     [os_addon.OS_VM])
        entity.attributes['occi.core.id'] = instance['uuid']
        entity.extras = self.get_extras(extras)

        # 2. os and res templates
        flavor_id = int(instance['instance_type_id'])
        res_tmp = self.get_category('/' + str(flavor_id) + '/', extras)
        if res_tmp:
            entity.mixins.append(res_tmp)

        image_id = instance['image_ref']
        image_tmp = self.get_category('/' + image_id + '/', extras)
        if image_tmp:
            entity.mixins.append(image_tmp)

        # 3. links
        storage_links = storage.get_attached_storage(instance['uuid'], context)
        for item in storage_links:
            entity.links.append(self._construct_storage_link(item,
                                                             entity,
                                                             extras))
        net_links = net.get_network_details_from_instance(instance, context)
        for net_type in ['public', 'admin']:
            for item in net_links[net_type]:
                link = self._construct_network_link(item, entity,
                                                    self.nets[net_type],
                                                    extras)
                entity.links.append(link)

        return entity

    def get_resource(self, key, extras):
        """
        Retrieve one resource
        """
        context = extras['nova_ctx']
        try:
            (loc, identifier) = key.rsplit('/', 1)
        except ValueError:
            raise AttributeError("Unexpected format for key %s" % key)
        loc = loc + '/'
        LOG.debug("Getting resource at %s with id: %s", loc, identifier)
        try:
            # XXX should use regular expressions?
            if loc == infrastructure.COMPUTE.location:
                compute_vm = vm.get_vm(identifier, context, False)
                return self._construct_occi_compute(compute_vm, extras)
            elif loc == infrastructure.STORAGE.location:
                vol = storage.get_storage(identifier, context)
                return self._construct_occi_storage(vol, extras)
            elif loc in [infrastructure.STORAGELINK.location,
                         infrastructure.NETWORKINTERFACE.location]:
                (compute_id, other_id) = identifier.split('_', 1)
                compute_vm = vm.get_vm(compute_id, context)
                occi_vm = self._construct_occi_compute(compute_vm, extras)
                # look for the link
                for link in occi_vm.links:
                    if link.identifier == key:
                        return link
            elif loc == infrastructure.NETWORK.location:
                return self.nets[identifier]
        except exceptions.HTTPError:
            # the nova_glue did not find the resource, just ignore
            pass
        # not found!
        raise KeyError(key)

    def get_resources(self, extras):
        """
        Retrieve a set of resources.
        """
        # TODO: add security rules!
        context = extras['nova_ctx']
        result = []

        # VMs
        vms = vm.get_vms(context)
        for instance in vms:
            occi_compute = self._construct_occi_compute(instance, extras)
            result.append(occi_compute)
            result.extend(occi_compute.links)
        # Volumes
        stors = storage.get_storage_volumes(context)
        for stor in stors:
            occi_storage = self._construct_occi_storage(stor, extras)
            result.append(occi_storage)
            result.extend(occi_storage.links)
        # Networks, XXX not sure if I need to return this
        result.extend(self.nets.values())
        return result

    def get_resource_keys(self, extras):
        """
        Retrieve the keys of all resources.
        """
        # this is not the most efficient implementation, but should work
        all_resources = self.get_resources(extras)
        return [r.identifier for r in all_resources]

    def _setup_network(self):
        """
        Add a public and an admin network interface.
        """
        # TODO: read from openstack!
        self.nets['public'].attributes = {
            'occi.network.vlan': 'external',
            'occi.network.label': 'default',
            'occi.network.state': 'active',
            'occi.networkinterface.address': '192.168.0.0/24',
            'occi.networkinterface.gateway': '192.168.0.1',
            'occi.networkinterface.allocation': 'static'
        }
        self.nets['admin'].attributes = {
            'occi.network.vlan': 'admin',
            'occi.network.label': 'default',
            'occi.network.state': 'active',
            'occi.networkinterface.address': '10.0.0.0/24',
            'occi.networkinterface.gateway': '10.0.0.1',
            'occi.networkinterface.allocation': 'static'
        }

    def _construct_network_link(self, net_desc, source, target, extras):
        """
        Construct a network link
        """
        link_id = '_'.join([source.attributes['occi.core.id'],
                            net_desc['address']])
        link = core_model.Link(infrastructure.NETWORKINTERFACE.location +
                               link_id,
                               infrastructure.NETWORKINTERFACE,
                               [infrastructure.IPNETWORKINTERFACE], source,
                               target)
        link.attributes = {
            'occi.networkinterface.interface': net_desc['interface'],
            'occi.networkinterface.mac': net_desc['mac'],
            'occi.networkinterface.state': net_desc['state'],
            'occi.networkinterface.address': net_desc['address'],
            'occi.networkinterface.gateway': net_desc['gateway'],
            'occi.networkinterface.allocation': net_desc['allocation']
        }
        link.extras = self.get_extras(extras)
        return link
