# Copyright 2017 Datera
# All Rights Reserved.
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

import time
import uuid

from eventlet.green import threading
from oslo_config import cfg
from oslo_log import log as logging
import six

from cinder import exception
from cinder.i18n import _
from cinder import utils
from cinder.volume.drivers.san import san

import cinder.volume.drivers.datera.datera_api2 as api2
import cinder.volume.drivers.datera.datera_api21 as api21
import cinder.volume.drivers.datera.datera_api22 as api22
import cinder.volume.drivers.datera.datera_common as datc


LOG = logging.getLogger(__name__)

d_opts = [
    cfg.StrOpt('datera_api_port',
               default='7717',
               help='Datera API port.'),
    cfg.StrOpt('datera_api_version',
               default='2',
               deprecated_for_removal=True,
               help='Datera API version.'),
    cfg.StrOpt('datera_ldap_server',
               default=None,
               help='LDAP authentication server'),
    cfg.IntOpt('datera_503_timeout',
               default='120',
               help='Timeout for HTTP 503 retry messages'),
    cfg.IntOpt('datera_503_interval',
               default='5',
               help='Interval between 503 retries'),
    cfg.BoolOpt('datera_debug',
                default=False,
                help="True to set function arg and return logging"),
    cfg.BoolOpt('datera_debug_replica_count_override',
                default=False,
                help="ONLY FOR DEBUG/TESTING PURPOSES\n"
                     "True to set replica_count to 1"),
    cfg.StrOpt('datera_tenant_id',
               default=None,
               help="If set to 'Map' --> OpenStack project ID will be mapped "
                    "implicitly to Datera tenant ID\n"
                    "If set to 'None' --> Datera tenant ID will not be used "
                    "during volume provisioning\n"
                    "If set to anything else --> Datera tenant ID will be the "
                    "provided value"),
    cfg.BoolOpt('datera_enable_image_cache',
                default=False,
                help="Set to True to enable Datera backend image caching"),
    cfg.StrOpt('datera_image_cache_volume_type_id',
               default=None,
               help="Cinder volume type id to use for cached volumes"),
    cfg.BoolOpt('datera_disable_profiler',
                default=False,
                help="Set to True to disable profiling in the Datera driver"),
    cfg.DictOpt('datera_volume_type_defaults',
                default={},
                help="Settings here will be used as volume-type defaults if "
                     "the volume-type setting is not provided.  This can be "
                     "used, for example, to set a very low total_iops_max "
                     "value if none is specified in the volume-type to "
                     "prevent accidental overusage.  Options are specified "
                     "via the following format, WITHOUT ANY 'DF:' PREFIX: "
                     "'datera_volume_type_defaults="
                     "iops_per_gb:100,bandwidth_per_gb:200...etc'.")
]


CONF = cfg.CONF
CONF.import_opt('driver_use_ssl', 'cinder.volume.driver')
CONF.register_opts(d_opts)


@six.add_metaclass(utils.TraceWrapperWithABCMetaclass)
class DateraDriver(san.SanISCSIDriver, api2.DateraApi, api21.DateraApi,
                   api22.DateraApi):

    """The OpenStack Datera Driver

    Version history:
        1.0 - Initial driver
        1.1 - Look for lun-0 instead of lun-1.
        2.0 - Update For Datera API v2
        2.1 - Multipath, ACL and reorg
        2.2 - Capabilites List, Extended Volume-Type Support
              Naming convention change,
              Volume Manage/Unmanage support
        2.3 - Templates, Tenants, Snapshot Polling,
              2.1 Api Version Support, Restructure
        2.3.1 - Scalability bugfixes
        2.3.2 - Volume Placement, ACL multi-attach bugfix
        2.4.0 - Fast Retype Support
        2.5.0 - Glance Image Caching, retyping/QoS bugfixes
        2.6.0 - Api 2.2 support
        2.6.1 - Glance interoperability fix
        2.7.0 - IOPS/GB and BW/GB settings, driver level overrides
                (API 2.1+ only)
        2.7.2 - Allowing DF: QoS Spec prefix, QoS type leak bugfix
        2.7.3 - Fixed bug in clone_image where size was not set correctly
        2.7.4 - Fix for create_tenant incorrect API call
                Temporary fix for DAT-15931
        2.7.5 - Removed "force" parameter from /initiators v2.1 API requests
        2.8.0 - iops_per_gb and bandwidth_per_gb are now limited by
                total_iops_max and total_bandwidth_max (API 2.1+ only)
                Bugfix for cinder retype with online volume
        2.8.1 - Bugfix for missing default dict during retype
        2.8.2 - Updated most retype operations to not detach volume
        2.8.3 - Bugfix for not allowing fast clones for shared/community
                volumes
        2.8.4 - Fixed missing API version pinning in _offline_flip
        2.8.5 - Membership check for fast image cloning. Metadata API pinning
        2.8.6 - Added LDAP support and CHAP support
        2.8.7 - Bugfix for missing tenancy calls in offline_flip
    """
    VERSION = '2.8.7'

    CI_WIKI_NAME = "datera-ci"

    HEADER_DATA = {'Datera-Driver': 'OpenStack-Cinder-{}'.format(VERSION)}

    def __init__(self, *args, **kwargs):
        super(DateraDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(d_opts)
        self.username = self.configuration.san_login
        self.password = self.configuration.san_password
        self.ldap = self.configuration.datera_ldap_server
        self.cluster_stats = {}
        self.datera_api_token = None
        self.interval = self.configuration.datera_503_interval
        self.retry_attempts = (self.configuration.datera_503_timeout /
                               self.interval)
        self.driver_prefix = str(uuid.uuid4())[:4]
        self.datera_debug = self.configuration.datera_debug
        self.datera_api_versions = []

        if self.datera_debug:
            utils.setup_tracing(['method'])
        self.tenant_id = self.configuration.datera_tenant_id
        self.defaults = self.configuration.datera_volume_type_defaults
        if self.tenant_id and self.tenant_id.lower() == 'none':
            self.tenant_id = None
        self.api_check = time.time()
        self.api_cache = []
        self.api_timeout = 0
        self.do_profile = not self.configuration.datera_disable_profiler
        self.image_cache = self.configuration.datera_enable_image_cache
        self.image_type = self.configuration.datera_image_cache_volume_type_id
        self.thread_local = threading.local()

        self.use_chap_auth = self.configuration.use_chap_auth
        self.chap_username = self.configuration.chap_username
        self.chap_password = self.configuration.chap_password

        backend_name = self.configuration.safe_get(
            'volume_backend_name')
        self.backend_name = backend_name or 'Datera'

        datc.register_driver(self)

    def do_setup(self, context):
        # If we can't authenticate through the old and new method, just fail
        # now.
        if not all([self.username, self.password]):
            msg = _("san_login and/or san_password is not set for Datera "
                    "driver in the cinder.conf. Set this information and "
                    "start the cinder-volume service again.")
            LOG.error(msg)
            raise exception.InvalidInput(msg)

        self.login()
        self.create_tenant()

    # =================

    # =================
    # = Create Volume =
    # =================

    @datc._api_lookup
    def create_volume(self, volume):
        """Create a logical volume."""
        pass

    # =================
    # = Extend Volume =
    # =================

    @datc._api_lookup
    def extend_volume(self, volume, new_size):
        pass

    # =================

    # =================
    # = Cloned Volume =
    # =================

    @datc._api_lookup
    def create_cloned_volume(self, volume, src_vref):
        pass

    # =================
    # = Delete Volume =
    # =================

    @datc._api_lookup
    def delete_volume(self, volume):
        pass

    # =================
    # = Ensure Export =
    # =================

    @datc._api_lookup
    def ensure_export(self, context, volume, connector=None):
        """Gets the associated account, retrieves CHAP info and updates."""

    # =========================
    # = Initialize Connection =
    # =========================

    @datc._api_lookup
    def initialize_connection(self, volume, connector):
        pass

    # =================
    # = Create Export =
    # =================

    @datc._api_lookup
    def create_export(self, context, volume, connector):
        pass

    # =================
    # = Detach Volume =
    # =================

    @datc._api_lookup
    def detach_volume(self, context, volume, attachment=None):
        pass

    # ===================
    # = Create Snapshot =
    # ===================

    @datc._api_lookup
    def create_snapshot(self, snapshot):
        pass

    # ===================
    # = Delete Snapshot =
    # ===================

    @datc._api_lookup
    def delete_snapshot(self, snapshot):
        pass

    # ========================
    # = Volume From Snapshot =
    # ========================

    @datc._api_lookup
    def create_volume_from_snapshot(self, volume, snapshot):
        pass

    # ==========
    # = Retype =
    # ==========

    @datc._api_lookup
    def retype(self, ctxt, volume, new_type, diff, host):
        """Convert the volume to be of the new type.

        Returns a boolean indicating whether the retype occurred.
        :param ctxt: Context
        :param volume: A dictionary describing the volume to migrate
        :param new_type: A dictionary describing the volume type to convert to
        :param diff: A dictionary with the difference between the two types
        :param host: A dictionary describing the host to migrate to, where
                     host['host'] is its name, and host['capabilities'] is a
                     dictionary of its reported capabilities (Not Used).
        """
        pass

    # ==========
    # = Manage =
    # ==========

    @datc._api_lookup
    def manage_existing(self, volume, existing_ref):
        """Manage an existing volume on the Datera backend

        The existing_ref must be either the current name or Datera UUID of
        an app_instance on the Datera backend in a colon separated list with
        the storage instance name and volume name.  This means only
        single storage instances and single volumes are supported for
        managing by cinder.

        Eg.

        (existing_ref['source-name'] ==
             tenant:app_inst_name:storage_inst_name:vol_name)
        if using Datera 2.1 API

        or

        (existing_ref['source-name'] ==
             app_inst_name:storage_inst_name:vol_name)

        if using 2.0 API

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume
        """
        pass

    # ===================
    # = Manage Get Size =
    # ===================

    @datc._api_lookup
    def manage_existing_get_size(self, volume, existing_ref):
        """Get the size of an unmanaged volume on the Datera backend

        The existing_ref must be either the current name or Datera UUID of
        an app_instance on the Datera backend in a colon separated list with
        the storage instance name and volume name.  This means only
        single storage instances and single volumes are supported for
        managing by cinder.

        Eg.

        existing_ref == app_inst_name:storage_inst_name:vol_name

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume on the Datera backend
        """
        pass

    # =========================
    # = Get Manageable Volume =
    # =========================

    @datc._api_lookup
    def get_manageable_volumes(self, cinder_volumes, marker, limit, offset,
                               sort_keys, sort_dirs):
        """List volumes on the backend available for management by Cinder.

        Returns a list of dictionaries, each specifying a volume in the host,
        with the following keys:
        - reference (dictionary): The reference for a volume, which can be
          passed to "manage_existing".
        - size (int): The size of the volume according to the storage
          backend, rounded up to the nearest GB.
        - safe_to_manage (boolean): Whether or not this volume is safe to
          manage according to the storage backend. For example, is the volume
          in use or invalid for any reason.
        - reason_not_safe (string): If safe_to_manage is False, the reason why.
        - cinder_id (string): If already managed, provide the Cinder ID.
        - extra_info (string): Any extra information to return to the user

        :param cinder_volumes: A list of volumes in this host that Cinder
                               currently manages, used to determine if
                               a volume is manageable or not.
        :param marker:    The last item of the previous page; we return the
                          next results after this value (after sorting)
        :param limit:     Maximum number of items to return
        :param offset:    Number of items to skip after marker
        :param sort_keys: List of keys to sort results by (valid keys are
                          'identifier' and 'size')
        :param sort_dirs: List of directions to sort by, corresponding to
                          sort_keys (valid directions are 'asc' and 'desc')
        """
        pass

    # ============
    # = Unmanage =
    # ============

    @datc._api_lookup
    def unmanage(self, volume):
        """Unmanage a currently managed volume in Cinder

        :param volume:       Cinder volume to unmanage
        """
        pass

    # ====================
    # = Fast Image Clone =
    # ====================

    @datc._api_lookup
    def clone_image(self, context, volume, image_location, image_meta,
                    image_service):
        """Clone an existing image volume."""
        pass

    # ================
    # = Volume Stats =
    # ================

    @datc._api_lookup
    def get_volume_stats(self, refresh=False):
        """Get volume stats.

        If 'refresh' is True, run update first.
        The name is a bit misleading as
        the majority of the data here is cluster
        data.
        """
        pass

    # =========
    # = Login =
    # =========

    @datc._api_lookup
    def login(self):
        pass

    # ===========
    # = Tenancy =
    # ===========

    @datc._api_lookup
    def create_tenant(self):
        pass

    # =======
    # = QoS =
    # =======

    def _update_qos(self, resource, policies):
        url = datc.URL_TEMPLATES['vol_inst'](
            policies['default_storage_name'],
            policies['default_volume_name']) + '/performance_policy'
        url = url.format(datc._get_name(resource['id']))
        type_id = resource.get('volume_type_id', None)
        if type_id is not None:
            # Filter for just QOS policies in result. All of their keys
            # should end with "max"
            fpolicies = {k: int(v) for k, v in
                         policies.items() if k.endswith("max")}
            # Filter all 0 values from being passed
            fpolicies = dict(filter(lambda _v: _v[1] > 0, fpolicies.items()))
            if fpolicies:
                self._issue_api_request(url, 'post', body=fpolicies,
                                        api_version='2')

    def _get_lunid(self):
        return 0

    # ============================
    # = Volume-Types/Extra-Specs =
    # ============================

    def _init_vendor_properties(self):
        """Create a dictionary of vendor unique properties.

        This method creates a dictionary of vendor unique properties
        and returns both created dictionary and vendor name.
        Returned vendor name is used to check for name of vendor
        unique properties.

        - Vendor name shouldn't include colon(:) because of the separator
          and it is automatically replaced by underscore(_).
          ex. abc:d -> abc_d
        - Vendor prefix is equal to vendor name.
          ex. abcd
        - Vendor unique properties must start with vendor prefix + ':'.
          ex. abcd:maxIOPS

        Each backend driver needs to override this method to expose
        its own properties using _set_property() like this:

        self._set_property(
            properties,
            "vendorPrefix:specific_property",
            "Title of property",
            _("Description of property"),
            "type")

        : return dictionary of vendor unique properties
        : return vendor name

        prefix: DF --> Datera Fabric
        """
        LOG.debug("Using the following volume-type defaults: %s",
                  self.defaults)

        properties = {}

        self._set_property(
            properties,
            "DF:iops_per_gb",
            "Datera IOPS Per GB Setting",
            _("Setting this value will calculate IOPS for each volume of "
              "this type based on their size.  Eg. A setting of 100 will "
              "give a 1 GB volume 100 IOPS, but a 10 GB volume 1000 IOPS. "
              "A setting of '0' is unlimited.  This value is applied to "
              "total_iops_max and will be overridden by total_iops_max if "
              "iops_per_gb is set and a large enough volume is provisioned "
              "which would exceed total_iops_max"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('iops_per_gb', 0)))

        self._set_property(
            properties,
            "DF:bandwidth_per_gb",
            "Datera Bandwidth Per GB Setting",
            _("Setting this value will calculate bandwidth for each volume of "
              "this type based on their size in KiB/s.  Eg. A setting of 100 "
              "will give a 1 GB volume 100 KiB/s bandwidth, but a 10 GB "
              "volume 1000 KiB/s bandwidth. A setting of '0' is unlimited. "
              "This value is applied to total_bandwidth_max and will be "
              "overridden by total_bandwidth_max if set and a large enough "
              "volume is provisioned which woudl exceed total_bandwidth_max"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('bandwidth_per_gb', 0)))

        self._set_property(
            properties,
            "DF:placement_mode",
            "Datera Volume Placement",
            _("'single_flash' for single-flash-replica placement, "
              "'all_flash' for all-flash-replica placement, "
              "'hybrid' for hybrid placement"),
            "string",
            default=self.defaults.get('placement_mode', 'hybrid'))

        self._set_property(
            properties,
            "DF:round_robin",
            "Datera Round Robin Portals",
            _("True to round robin the provided portals for a target"),
            "boolean",
            default="True" == self.defaults.get('round_robin', "False"))

        if self.configuration.get('datera_debug_replica_count_override'):
            replica_count = 1
        else:
            replica_count = 3
        self._set_property(
            properties,
            "DF:replica_count",
            "Datera Volume Replica Count",
            _("Specifies number of replicas for each volume. Can only be "
              "increased once volume is created"),
            "integer",
            minimum=1,
            default=int(self.defaults.get('replica_count', replica_count)))

        self._set_property(
            properties,
            "DF:acl_allow_all",
            "Datera ACL Allow All",
            _("True to set acl 'allow_all' on volumes created.  Cannot be "
              "changed on volume once set"),
            "boolean",
            default="True" == self.defaults.get('acl_allow_all', "False"))

        self._set_property(
            properties,
            "DF:ip_pool",
            "Datera IP Pool",
            _("Specifies IP pool to use for volume"),
            "string",
            default=self.defaults.get('ip_pool', 'default'))

        self._set_property(
            properties,
            "DF:template",
            "Datera Template",
            _("Specifies Template to use for volume provisioning"),
            "string",
            default=self.defaults.get('template', ''))

        self._set_property(
            properties,
            "DF:default_storage_name",
            "Datera Default Storage Instance Name",
            _("The name to use for storage instances created"),
            "string",
            default=self.defaults.get('default_storage_name', "storage-1"))

        self._set_property(
            properties,
            "DF:default_volume_name",
            "Datera Default Volume Name",
            _("The name to use for volumes created"),
            "string",
            default=self.defaults.get('default_volume_name', "volume-1"))

        # ###### QoS Settings ###### #
        self._set_property(
            properties,
            "DF:read_bandwidth_max",
            "Datera QoS Max Bandwidth Read",
            _("Max read bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('read_bandwidth_max', 0)))

        self._set_property(
            properties,
            "DF:write_bandwidth_max",
            "Datera QoS Max Bandwidth Write",
            _("Max write bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('write_bandwidth_max', 0)))

        self._set_property(
            properties,
            "DF:total_bandwidth_max",
            "Datera QoS Max Bandwidth Total",
            _("Max total bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('total_bandwidth_max', 0)))

        self._set_property(
            properties,
            "DF:read_iops_max",
            "Datera QoS Max iops Read",
            _("Max read iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('read_iops_max', 0)))

        self._set_property(
            properties,
            "DF:write_iops_max",
            "Datera QoS Max IOPS Write",
            _("Max write iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('write_iops_max', 0)))

        self._set_property(
            properties,
            "DF:total_iops_max",
            "Datera QoS Max IOPS Total",
            _("Max total iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('total_iops_max', 0)))
        # ###### End QoS Settings ###### #

        return properties, 'DF'
