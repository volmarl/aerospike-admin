# Copyright 2013-2017 Aerospike, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import re
import socket
from telnetlib import Telnet
from time import time
import threading
import logging
import lib
from distutils.version import LooseVersion
from lib.client.assocket import ASSocket
from lib.client import util
from lib.collectinfo_parser.full_parser import parse_system_live_command

#### Remote Server connection module

NO_MODULE = 0
OLD_MODULE = 1
NEW_MODULE = 2

try:
    from pexpect import pxssh
    PEXPECT_VERSION = NEW_MODULE
except ImportError:
    try:
        # For old versions of pexpect ( < 3.0)
        import pexpect
        import pxssh
        PEXPECT_VERSION = OLD_MODULE
    except ImportError:
        PEXPECT_VERSION = NO_MODULE

COMMAND_PROMPT = '[#$] '

def getfqdn(address, timeout=0.5):
    # note: cannot use timeout lib because signal must be run from the
    #       main thread

    result = [address]

    def helper():
        result[0] = socket.getfqdn(address)

    t = threading.Thread(target=helper)

    t.start()

    t.join(timeout)

    return result[0]


def return_exceptions(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            args[0].alive = False
            return e

    return wrapper


class Node(object):
    dns_cache = {}
    pool_lock = threading.Lock()

    def __init__(self, address, port=3000, tls_name=None, timeout=3, user=None,
                 password=None,  ssl_context=None, consider_alumni=False, use_services_alt=False):
        """
        address -- ip or fqdn for this node
        port -- info port for this node
        timeout -- number of seconds to wait before giving up on the node
        If address is ip then get fqdn else get ip
        store ip in self.ip
        store fqdn in self.fqdn
        store port in self.port

        NOTE: would be nice if the port could either be the service or telnet
        access port. Can we detect from the socket?
        ALSO NOTE: May be better to just use telnet instead?
        """
        self.logger = logging.getLogger('asadm')
        self._update_IP(address, port)
        self.port = port
        self.xdr_port = 3004  # TODO: Find the xdr port
        self._timeout = timeout
        self._use_telnet = False
        self.user = user
        self.password = password
        self.tls_name = tls_name
        self.ssl_context = ssl_context
        if ssl_context:
            self.enable_tls = True
        else:
            self.enable_tls = False
        self.consider_alumni = consider_alumni
        self.use_services_alt = use_services_alt

        # System Details
        self.sys_ssh_port = None
        self.sys_user_id = None
        self.sys_pwd = None
        self.sys_credential_file = None
        self.sys_default_ssh_port = None
        self.sys_default_user_id = None
        self.sys_default_pwd = None
        self.sys_cmds = [
            ('hostname', ['hostname -I', 'hostname']),
            ('top', ['top -n1 -b', 'top -l 1']),
            ('lsb', ['lsb_release -a', 'ls /etc|grep release|xargs -I f cat /etc/f']),
            ('meminfo', ['cat /proc/meminfo', 'vmstat -s']),
            ('interrupts', ['cat /proc/interrupts', '']),
            ('iostat', ['iostat -x 1 1', '']),
            ('df', ['df -h', '']),
            ('free-m', ['free -m', '']),
            ('uname', ['uname -a', ''])
        ]

        # hack, _key needs to be defines before info calls... but may have
        # wrong (localhost) address before info_service is called. Will set
        # again after that call.

        self._key = hash(self.create_key(address, self.port))
        self.peers_generation = -1
        self.service_addresses = []
        self.socket_pool = {}
        self.socket_pool[self.port] = set()
        self.socket_pool[self.xdr_port] = set()
        self.connect(address, port)
        self.localhost = False
        try:
            if address.lower() == "localhost":
                self.localhost = True
            else:
                o, e = util.shell_command(["hostname -I"])
                self.localhost = self._is_any_my_ip(o.split())
        except Exception:
            pass

    def _is_any_my_ip(self, ips):
        if not ips:
            return False
        s_a = [a[0] for a in self.service_addresses]
        if set(ips).intersection(set(s_a)):
            return True
        return False

    def connect(self, address, port):
        try:
            self.node_id = self.info_node()
            if isinstance(self.node_id, Exception):
                # Not able to connect this address
                raise self.node_id

            # Original address may not be the service address, the
            # following will ensure we have the service address
            service_addresses = self.info_service(address, return_None=True)
            if service_addresses and not isinstance(self.service_addresses, Exception):
                self.service_addresses = service_addresses
            # else : might be it's IP is not available, node should try all old
            # service addresses
            self.close()
            if (not self.service_addresses
                    or (self.ip, self.port, self.tls_name) not in
                    self.service_addresses):

                # if asd >= 3.10 and node has only IPv6 address
                self.service_addresses.append(
                    (self.ip, self.port, self.tls_name))
            for s in self.service_addresses:
                try:
                    address = s[0]
                    # calling update ip again because info_service may have provided a
                    # different IP than what was seeded.
                    self._update_IP(address, self.port)
                    self.node_id = self.info_node()

                    if not isinstance(self.node_id, Exception):
                        break
                except Exception:
                    # Sometime unavailable address might be present in service
                    # list, for ex. Down NIC address (server < 3.10).
                    # In such scenario, we want to try all addresses from
                    # service list till we get available address
                    pass

            if isinstance(self.node_id, Exception):
                raise self.node_id
            self._service_IP_port = self.create_key(self.ip, self.port)
            self._key = hash(self._service_IP_port)
            self.features = self.info('features')
            self.use_peers_list = self.is_feature_present(feature="peers")
            if self.has_peers_changed():
                self.peers = self._find_friend_nodes()
            self.alive = True
        except Exception:
            # Node is offline... fake a node
            self.ip = address
            self.fqdn = address
            self.port = port
            self._service_IP_port = self.create_key(self.ip, self.port)
            self._key = hash(self._service_IP_port)

            self.node_id = "000000000000000"
            self.service_addresses = [(self.ip, self.port, self.tls_name)]
            self.features = ""
            self.use_peers_list = False
            self.peers = []
            self.alive = False

    def refresh_connection(self):
        self.connect(self.ip, self.port)

    @property
    def key(self):
        """Get the value of service_IP_port"""
        return self._service_IP_port

    @staticmethod
    def create_key(address, port):
        if address and ":" in address:
            # IPv6 format
            return "[%s]:%s" % (address, port)
        return "%s:%s" % (address, port)

    def __hash__(self):
        return hash(self._key)

    def __eq__(self, other):
        return self._key == other._key

    def _update_IP(self, address, port):
        if address not in self.dns_cache:
            self.dns_cache[address] = (socket.getaddrinfo(address, port,
                                                          socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4][0],
                                       getfqdn(address))

        self.ip, self.fqdn = self.dns_cache[address]

    def sock_name(self, use_fqdn=False):
        if use_fqdn:
            address = self.fqdn
        else:
            address = self.ip

        return self.create_key(address, self.port)

    def __str__(self):
        return self.sock_name()

    def is_XDR_enabled(self):
        config = self.info_get_config('xdr')
        if isinstance(config, Exception):
            return False
        try:
            xdr_enabled = config['xdr']['enable-xdr']
            return xdr_enabled == 'true'
        except Exception:
            pass
        return False

    def is_feature_present(self, feature):
        if not self.features or isinstance(self.features, Exception):
            return False

        return (feature in self.features)

    def has_peers_changed(self):
        try:
            if not self.use_peers_list:
                # old server code < 3.10
                return True
            new_generation = self.info("peers-generation")
            if self.peers_generation != new_generation:
                self.peers_generation = new_generation
                return True
            else:
                return False
        except Exception:
            return True

    # Need to provide ip to _info_telnet and _info_cinfo as to maintain
    # unique key for cache. When we run cluster on VM and asadm on Host then
    # services returns all endpoints of server but some of them might not
    # allowed by Host and VM connection. If we do not provide IP here, then
    # we will get same result from cache for that IP to which asadm can't
    # connect. If this happens while setting ip (connection process) then node
    # will get that ip to which asadm can't connect. It will create new
    # issues in future process.

    @return_exceptions
    @util.cached
    def _info_telnet(self, command, ip=None, port=None):
        # TODO: Handle socket failures
        if ip == None:
            ip = self.ip
        if port == None:
            port = self.port
        try:
            self.sock == self.sock  # does self.sock exist?
        except Exception:
            self.sock = Telnet(ip, port)

        self.sock.write("%s\n" % command)

        starttime = time()
        result = ""
        while not result:
            result = self.sock.read_very_eager().strip()
            if starttime + self._timeout < time():
                # TODO: rasie appropriate exception
                raise IOError("Could not connect to node %s" % ip)
        return result

    def _get_connection(self, ip, port):
        sock = None
        with Node.pool_lock:
            try:
                while True:
                    sock = self.socket_pool[port].pop()
                    if sock.is_connected():
                        if not self.ssl_context:
                            sock.settimeout(5.0)
                        break
                    sock.close(force=True)
            except Exception:
                pass
        if sock:
            return sock
        sock = ASSocket(self, ip, port)
        if sock.connect():
            return sock
        return None

    def close(self):
        try:
            while True:
                sock = self.socket_pool[self.port].pop()
                sock.close(force=True)
        except Exception:
            pass

        try:
            while True:
                sock = self.socket_pool[self.xdr_port].pop()
                sock.close(force=True)
        except Exception:
            pass
        self.socket_pool = None

    @return_exceptions
    @util.cached
    def _info_cinfo(self, command, ip=None, port=None):
        # TODO: citrusleaf.py does not support passing a timeout default is
        # 0.5s
        if ip == None:
            ip = self.ip
        if port == None:
            port = self.port
        result = None
        sock = self._get_connection(ip, port)
        try:
            if sock:
                result = sock.execute(command)
                sock.close()
            if result != -1 and result is not None:
                return result
            else:
                raise IOError(
                    "Invalid command or Could not connect to node %s " % ip)
        except Exception:
            if sock:
                sock.close()
            raise IOError(
                "Invalid command or Could not connect to node %s " % ip)

    @return_exceptions
    def info(self, command):
        """
        asinfo function equivalent

        Arguments:
        command -- the info command to execute on this node
        """
        if self._use_telnet:
            return self._info_telnet(command, self.ip)
        else:
            return self._info_cinfo(command, self.ip)

    @return_exceptions
    @util.cached
    def xdr_info(self, command):
        """
        asinfo -p [xdr-port] equivalent

        Arguments:
        command -- the info command to execute on this node
        """

        try:
            return self._info_cinfo(command, self.ip, self.xdr_port)
        except Exception as e:
            self.logger.error("Couldn't get XDR info: " + str(e))
            return e

    @return_exceptions
    def info_node(self):
        """
        Get this nodes id. asinfo -v "node"

        Returns:
        string -- this node's id.
        """

        return self.info("node")

    @return_exceptions
    def _info_peers_list_helper(self, peers):
        """
        Takes an info peers list response and returns a list.
        """
        gen_port_peers = util._parse_string(peers)
        if not gen_port_peers or len(gen_port_peers) < 3:
            return []
        default_port = 3000
        # TODO not used generation = gen_port_peers[0]
        if (gen_port_peers[1]):
            default_port = int(gen_port_peers[1])

        peers_list = util._parse_string(gen_port_peers[2])
        if not peers_list or len(peers_list) < 1:
            return []
        p_list = []
        for p in peers_list:
            p_data = util._parse_string(p)
            if not p_data or len(p_data) < 3:
                continue
            # TODO - not used node_name = p_data[0]
            tls_name = None
            if p_data[1] and len(p_data[1]) > 0:
                tls_name = p_data[1]

            endpoints = util._parse_string(p_data[2])
            if not endpoints or len(endpoints) < 1:
                continue

            if not tls_name:
                tls_name = util.find_dns(endpoints)
            endpoint_list = []
            for e in endpoints:
                if "[" in e and not "]:" in e:
                    addr_port = util._parse_string(e, delim=",")
                else:
                    addr_port = util._parse_string(e, delim=":")
                addr = addr_port[0]
                if addr.startswith("["):
                    addr = addr[1:]
                if addr.endswith("]"):
                    addr = addr[:-1].strip()

                if (len(addr_port) > 1 and addr_port[1]
                        and len(addr_port[1]) > 0):
                    port = addr_port[1]
                else:
                    port = default_port
                try:
                    port = int(port)
                except Exception:
                    port = default_port
                endpoint_list.append((addr, port, tls_name))
            p_list.append(tuple(endpoint_list))
        return p_list

    @return_exceptions
    def info_peers_list(self):
        """
        Get peers this node knows of that are active

        Returns:
        list -- [(p1_ip,p1_port,p1_tls_name),((p2_ip1,p2_port1,p2_tls_name),(p2_ip2,p2_port2,p2_tls_name))...]
        """
        if self.enable_tls:
            return self._info_peers_list_helper(self.info("peers-tls-std"))

        return self._info_peers_list_helper(self.info("peers-clear-std"))

    @return_exceptions
    def info_alumni_peers_list(self):
        """
        Get peers this node has ever know of

        Returns:
        list -- [(p1_ip,p1_port,p1_tls_name),((p2_ip1,p2_port1,p2_tls_name),(p2_ip2,p2_port2,p2_tls_name))...]
        """
        if self.enable_tls:
            return self._info_peers_list_helper(self.info("alumni-tls-std"))
        return self._info_peers_list_helper(self.info("alumni-clear-std"))

    @return_exceptions
    def info_alternative_peers_list(self):
        """
        Get peers this node knows of that are active alternative addresses

        Returns:
        list -- [(p1_ip,p1_port,p1_tls_name),((p2_ip1,p2_port1,p2_tls_name),(p2_ip2,p2_port2,p2_tls_name))...]
        """
        if self.enable_tls:
            return self._info_peers_list_helper(self.info("peers-tls-alt"))

        return self._info_peers_list_helper(self.info("peers-clear-alt"))

    @return_exceptions
    def _info_services_helper(self, services):
        """
        Takes an info services response and returns a list.
        """
        if not services or isinstance(services, Exception):
            return []

        s = map(util.info_to_tuple, util.info_to_list(services))
        return map(lambda v: (v[0], int(v[1]), self.tls_name), s)

    @return_exceptions
    def info_services(self):
        """
        Get other services this node knows of that are active

        Returns:
        list -- [(ip,port),...]
        """

        return self._info_services_helper(self.info("services"))

    @return_exceptions
    def info_services_alumni(self):
        """
        Get other services this node has ever know of

        Returns:
        list -- [(ip,port),...]
        """

        try:
            return self._info_services_helper(self.info("services-alumni"))
        except IOError:
            # Possibly old asd without alumni feature
            return self.info_services()

    @return_exceptions
    def info_services_alt(self):
        """
        Get other services_alternative this node knows of that are active

        Returns:
        list -- [(ip,port),...]
        """

        return self._info_services_helper(self.info("services-alternate"))

    @return_exceptions
    def info_service(self, address, return_None=False):
        try:
            service = self.info("service")
            s = map(util.info_to_tuple, util.info_to_list(service))
            return map(lambda v: (v[0], int(v[1]), self.tls_name), s)
        except Exception:
            pass
        if return_None:
            return None
        return [(address, self.port, self.tls_name)]

    @return_exceptions
    def get_alumni_peers(self):
        if self.use_peers_list:
            alumni_peers = self.info_peers_list()
            return alumni_peers + self.info_alumni_peers_list()
        else:
            alumni_services = self.info_services_alumni()
            if alumni_services and not isinstance(alumni_services, Exception):
                return alumni_services
            return self.info_services()

    @return_exceptions
    def get_peers(self):
        if self.use_peers_list:
            if self.use_services_alt:
                return self.info_alternative_peers_list()

            return self.info_peers_list()

        else:
            if self.use_services_alt:
                return self.info_services_alt()

            return self.info_services()

    @return_exceptions
    def _find_friend_nodes(self):
        if self.consider_alumni:
            return self.get_alumni_peers()
        else:
            return self.get_peers()

    @return_exceptions
    def info_statistics(self):
        """
        Get statistics for this node. asinfo -v "statistics"

        Returns:
        dictionary -- statistic name -> value
        """

        return util.info_to_dict(self.info("statistics"))

    @return_exceptions
    def info_namespaces(self):
        """
        Get a list of namespaces for this node. asinfo -v "namespaces"

        Returns:
        list -- list of namespaces
        """

        return util.info_to_list(self.info("namespaces"))

    @return_exceptions
    def info_namespace_statistics(self, namespace):
        """
        Get statistics for a namespace.

        Returns:
        dict -- {stat_name : stat_value, ...}
        """

        return util.info_to_dict(self.info("namespace/%s" % namespace))

    @return_exceptions
    def info_all_namespace_statistics(self):
        namespaces = self.info_namespaces()

        if isinstance(namespaces, Exception):
            return namespaces

        stats = {}
        for ns in namespaces:
            stats[ns] = self.info_namespace_statistics(ns)

        return stats

    @return_exceptions
    def info_set_statistics(self):
        stats = self.info("sets")
        stats = util.info_to_list(stats)
        if not stats:
            return {}
        stats.pop()
        stats = [util.info_colon_to_dict(stat) for stat in stats]
        sets = {}
        for stat in stats:
            ns_name = util.get_value_from_dict(
                d=stat, keys=('ns_name', 'namespace', 'ns'))
            set_name = util.get_value_from_dict(
                d=stat, keys=('set_name', 'set'))

            key = (ns_name, set_name)
            if key not in sets:
                sets[key] = {}
            set_dict = sets[key]

            set_dict.update(stat)

        return sets

    @return_exceptions
    def info_bin_statistics(self):
        stats = util.info_to_list(self.info("bins"))
        if not stats:
            return {}
        stats.pop()
        stats = [value.split(':') for value in stats]
        stat_dict = {}

        for stat in stats:
            values = util.info_to_list(stat[1], ',')
            values = ";".join(filter(lambda v: '=' in v, values))
            values = util.info_to_dict(values)
            stat_dict[stat[0]] = values

        return stat_dict

    @return_exceptions
    def info_XDR_statistics(self):
        """
        Get statistics for XDR

        Returns:
        dict -- {stat_name : stat_value, ...}
        """
        # for new aerospike version (>=3.8) with
        # xdr-in-asd stats available on service port
        if self.is_feature_present('xdr'):
            return util.info_to_dict(self.info("statistics/xdr"))

        return util.info_to_dict(self.xdr_info('statistics'))

    @return_exceptions
    def info_get_config(self, stanza="", namespace="", namespace_id=""):
        """
        Get the complete config for a node. This should include the following
        stanzas: Service, Network, XDR, and Namespace
        Sadly it seems Service and Network are not seperable.

        Returns:
        dict -- stanza --> [namespace] --> param --> value
        """
        config = {}
        if stanza == 'namespace':
            if namespace != "":
                config[stanza] = {namespace: util.info_to_dict(
                    self.info("get-config:context=namespace;id=%s" % namespace))}
                if namespace_id == "":
                    namespaces = self.info_namespaces()
                    if namespaces and namespace in namespaces:
                        namespace_id = namespaces.index(namespace)
                if namespace_id != "":
                    config[stanza][namespace]["nsid"] = str(namespace_id)
            else:
                namespace_configs = {}
                namespaces = self.info_namespaces()
                for index, namespace in enumerate(namespaces):
                    namespace_config = self.info_get_config(
                        'namespace', namespace, namespace_id=index)
                    namespace_config = namespace_config['namespace'][namespace]
                    namespace_configs[namespace] = namespace_config
                config['namespace'] = namespace_configs

        elif stanza == '':
            config['service'] = util.info_to_dict(self.info("get-config:"))
        elif stanza != 'all':
            config[stanza] = util.info_to_dict(
                self.info("get-config:context=%s" % stanza))
        elif stanza == "all":
            config['namespace'] = self.info_get_config("namespace")
            config['service'] = self.info_get_config("service")
            # Server lumps this with service
            # config["network"] = self.info_get_config("network")
        return config

    def _update_total_latency(self, t_rows, row):
        if not row or not isinstance(row, list):
            return t_rows
        if not t_rows:
            t_rows = []
            t_rows.append(row)
            return t_rows

        tm_range = row[0]
        updated = False
        for t_row in t_rows:
            if t_row[0] == tm_range:
                n_sum = float(row[1])
                if n_sum > 0:
                    o_sum = float(t_row[1])
                    for i, t_p in enumerate(t_row[2:]):
                        o_t = float((o_sum * t_p) / 100.00)
                        n_t = float((n_sum * row[i + 2]) / 100.00)
                        t_row[
                            i + 2] = round(float(((o_t + n_t) * 100) / (o_sum + n_sum)), 2)
                    t_row[1] = round(o_sum + n_sum, 2)
                updated = True
                break

        if not updated:
            t_rows.append(copy.deepcopy(row))
        return t_rows

    @return_exceptions
    def info_latency(self, back=None, duration=None, slice_tm=None, ns_set=None):
        cmd = 'latency:'
        try:
            if back or back == 0:
                cmd += "back=%d" % (back) + ";"
        except Exception:
            pass

        try:
            if duration or duration == 0:
                cmd += "duration=%d" % (duration) + ";"
        except Exception:
            pass

        try:
            if slice_tm or slice_tm == 0:
                cmd += "slice=%d" % (slice_tm) + ";"
        except Exception:
            pass
        data = {}

        try:
            hist_info = self.info(cmd)
        except Exception:
            return data
        #tdata = hist_info.split(';')[:-1]
        tdata = hist_info.split(';')
        hist_name = None
        ns = None
        start_time = None
        columns = []
        ns_hist_pattern = '{([A-Za-z_\d-]+)}-([A-Za-z_-]+)'
        total_key = (" ", "total")

        while tdata != []:
            row = tdata.pop(0)
            if not row:
                continue
            row = row.split(",")
            if len(row) < 2:
                continue

            s1, s2 = row[0].split(':', 1)

            if not s1.isdigit():
                m = re.search(ns_hist_pattern, s1)
                if m:
                    ns = m.group(1)
                    hist_name = m.group(2)
                else:
                    ns = None
                    hist_name = s1
                if ns_set and (not ns or ns not in ns_set):
                    hist_name = None
                    continue
                columns = row[1:]
                start_time = s2
                start_time = util.remove_suffix(start_time, "-GMT")
                columns.insert(0, 'Time Span')
                continue

            if not hist_name or not start_time:
                continue
            try:
                end_time = row.pop(0)
                end_time = util.remove_suffix(end_time, "-GMT")
                row = [float(r) for r in row]
                row.insert(0, "%s->%s" % (start_time, end_time))
                if hist_name not in data:
                    data[hist_name] = {}
                if ns:
                    ns_key = (ns, "namespace")
                    if ns_key not in data[hist_name]:
                        data[hist_name][ns_key] = {}
                        data[hist_name][ns_key]["columns"] = columns
                        data[hist_name][ns_key]["values"] = []
                    data[hist_name][ns_key][
                        "values"].append(copy.deepcopy(row))
                if total_key not in data[hist_name]:
                    data[hist_name][total_key] = {}
                    data[hist_name][total_key]["columns"] = columns
                    data[hist_name][total_key]["values"] = []

                data[hist_name][total_key]["values"] = self._update_total_latency(
                    data[hist_name][total_key]["values"], row)
                start_time = end_time
            except Exception:
                pass
        return data

    @return_exceptions
    def info_dcs(self):
        """
        Get a list of datacenters for this node. asinfo -v "dcs" -p 3004

        Returns:
        list -- list of dcs
        """
        if self.is_feature_present('xdr'):
            return util.info_to_list(self.info("dcs"))

        return util.info_to_list(self.xdr_info("dcs"))

    @return_exceptions
    def info_dc_statistics(self, dc):
        """
        Get statistics for a datacenter.

        Returns:
        dict -- {stat_name : stat_value, ...}
        """
        if self.is_feature_present('xdr'):
            return util.info_to_dict(self.info("dc/%s" % dc))
        return util.info_to_dict(self.xdr_info("dc/%s" % dc))

    @return_exceptions
    def info_all_dc_statistics(self):
        dcs = self.info_dcs()

        if isinstance(dcs, Exception):
            return {}

        stats = {}
        for dc in dcs:
            stat = self.info_dc_statistics(dc)
            if not stat or isinstance(stat, Exception):
                stat = {}
            stats[dc] = stat

        return stats

    @return_exceptions
    def info_udf_list(self):
        """
        Get config for a udf.

        Returns:
        dict -- {file_name1:{key_name : key_value, ...}, file_name2:{key_name : key_value, ...}}
        """
        udf_data = self.info('udf-list')

        if not udf_data:
            return {}

        return util.info_to_dict_multi_level(udf_data, "filename", delimiter2=',')

    @return_exceptions
    def info_dc_get_config(self):
        """
        Get config for a datacenter.

        Returns:
        dict -- {dc_name1:{config_name : config_value, ...}, dc_name2:{config_name : config_value, ...}}
        """
        if self.is_feature_present('xdr'):
            configs = self.info("get-dc-config")
            if not configs or isinstance(configs, Exception):
                configs = self.info("get-dc-config:")
            if not configs or isinstance(configs, Exception):
                return {}
            return util.info_to_dict_multi_level(configs, ["dc-name", "DC_Name"])

        configs = self.xdr_info("get-dc-config")
        if not configs or isinstance(configs, Exception):
            return {}
        return util.info_to_dict_multi_level(configs, ["dc-name", "DC_Name"])

    @return_exceptions
    def info_XDR_get_config(self):
        xdr_configs = self.info_get_config(stanza='xdr')
        # for new aerospike version (>=3.8) with xdr-in-asd config from service
        # port is sufficient
        if self.is_feature_present('xdr'):
            return xdr_configs
        # required for old aerospike server versions (<3.8)
        xdr_configs_xdr = self.xdr_info('get-config')
        if xdr_configs_xdr and not isinstance(xdr_configs_xdr, Exception):
            xdr_configs_xdr = {'xdr': util.info_to_dict(xdr_configs_xdr)}
            if xdr_configs_xdr['xdr'] and not isinstance(xdr_configs_xdr['xdr'], Exception):
                if xdr_configs and xdr_configs['xdr'] and not isinstance(xdr_configs['xdr'], Exception):
                    xdr_configs['xdr'].update(xdr_configs_xdr['xdr'])
                else:
                    xdr_configs = {}
                    xdr_configs['xdr'] = xdr_configs_xdr['xdr']
        return xdr_configs

    @return_exceptions
    def info_histogram(self, histogram):
        namespaces = self.info_namespaces()

        data = {}
        for namespace in namespaces:
            try:
                datum = self.info("hist-dump:ns=%s;hist=%s" %
                                  (namespace, histogram))
                datum = datum.split(',')
                datum.pop(0)  # don't care about ns, hist_name, or length
                width = int(datum.pop(0))
                datum[-1] = datum[-1].split(';')[0]
                datum = map(int, datum)

                data[namespace] = {
                    'histogram': histogram, 'width': width, 'data': datum}
            except Exception:
                pass
        return data

    @return_exceptions
    def info_sindex(self):
        return [util.info_to_dict(v, ':')
                for v in util.info_to_list(self.info("sindex"))[:-1]]

    @return_exceptions
    def info_sindex_statistics(self, namespace, indexname):
        """
        Get statistics for a sindex.

        Returns:
        dict -- {stat_name : stat_value, ...}
        """
        return util.info_to_dict(self.info("sindex/%s/%s" % (namespace, indexname)))

    @return_exceptions
    def info_XDR_build_version(self):
        """
        Get Build Version for XDR

        Returns:
        string -- build version
        """
        # for new aerospike version (>=3.8) with
        # xdr-in-asd stats available on service port
        if self.is_feature_present('xdr'):
            return self.info('build')

        return self.xdr_info('build')

    def _set_default_system_credentials(self, default_user=None,
                                        default_pwd=None, default_ssh_port=None, credential_file=None):
        if default_user:
            self.sys_default_user_id = default_user

        if default_pwd:
            self.sys_default_pwd = default_pwd

        if credential_file:
            self.sys_credential_file = credential_file

        if default_ssh_port:
            self.sys_default_ssh_port = default_ssh_port

    def _set_system_credentials_from_file(self):
        if not self.sys_credential_file:
            return False
        result = False
        f = None
        try:
            try:
                f = open(self.sys_credential_file, 'r')
            except IOError as e:
                self.logger.error("Can not open credential file. error: " + str(e))
                raise

            for line in f.readlines():
                if not line or not line.strip():
                    continue

                try:
                    line = line.strip().replace('\n', ' ').strip().split()
                    if len(line) < 3:
                        continue

                    ip = None
                    port = None
                    ip_port = line[0].strip()
                    if not ip_port:
                        continue

                    if "]" in ip_port:
                        # IPv6
                        try:
                            ip_port = ip_port[1:].split("]")
                            ip = ip_port[0].strip()
                            if len(ip_port) > 1:
                                # Removing ':' from port
                                port = int(ip_port[1].strip()[1:])
                        except Exception:
                            pass

                    else:
                        # IPv4
                        try:
                            ip_port = ip_port.split(":")
                            ip = ip_port[0]
                            if len(ip_port) > 1:
                                port = int(ip_port[1].strip())
                        except Exception:
                            pass

                    if ip and self._is_any_my_ip([ip]):
                        self.sys_user_id = line[1]
                        self.sys_pwd = line[2]
                        self.sys_ssh_port = port
                        result = True
                        break

                except Exception:
                    pass
        except Exception:
            self.logger.error("Couldn't set credential from given file.")
            pass
        finally:
            if f:
                f.close()
        return result

    def _set_system_credentials(self, use_cached_credentials=False):
        if use_cached_credentials:
            if self.sys_user_id and self.sys_pwd:
                return
        set = self._set_system_credentials_from_file()
        if set:
            return
        self.sys_user_id = self.sys_default_user_id
        self.sys_pwd = self.sys_default_pwd
        self.sys_ssh_port = self.sys_default_ssh_port

    @return_exceptions
    def info_system_statistics(self, default_user=None, default_pwd=None,
                               default_ssh_port=None, credential_file=None, commands=[]):
        """
        Get statistics for a system.

        Returns:
        dict -- {stat_name : stat_value, ...}
        """
        if not commands:
            commands = [_key for _key, cmds in self.sys_cmds]

        if self.localhost:
            return self._get_localhost_system_statistics(commands)
        else:
            self._set_default_system_credentials(default_user, default_pwd,
                                                 default_ssh_port, credential_file)
            return self._get_remote_host_system_statistics(commands)

    @return_exceptions
    def _get_localhost_system_statistics(self, commands):
        sys_stats = {}

        for _key, cmds in self.sys_cmds:
            if _key not in commands:
                continue

            for cmd in cmds:
                o, e = util.shell_command([cmd])
                if e or not o:
                    continue
                else:
                    parse_system_live_command(_key, o, sys_stats)
                    break

        return sys_stats

    @return_exceptions
    def _login_remote_system(self, ip, user, pwd, port=None):
        s = pxssh.pxssh()
        s.force_password = True
        s.SSH_OPTS = "-o 'NumberOfPasswordPrompts=1'"
        s.login(ip, user, pwd, port=port)
        return s

    @return_exceptions
    def _spawn_remote_system(self, ip, user, pwd, port=None):
        global COMMAND_PROMPT
        terminal_prompt = '(?i)terminal type\?'
        terminal_type = 'vt100'

        ssh_newkey = '(?i)are you sure you want to continue connecting'
        ssh_options = "-o 'NumberOfPasswordPrompts=1' "

        if port:
            ssh_options += " -p %s"%(str(port))
        s = pexpect.spawn('ssh %s -l %s %s'%(ssh_options, str(user), str(ip)))

        i = s.expect([pexpect.TIMEOUT, ssh_newkey, COMMAND_PROMPT, '(?i)password'])
        if i == 0:
            # Timeout
            return None

        enter_pwd = False
        if i == 1:
            # In this case SSH does not have the public key cached.
            s.sendline ('yes')
            s.expect ('(?i)password')
            enter_pwd = True
        elif i == 2:
            pass
        elif i == 3:
            enter_pwd = True

        if enter_pwd:
            s.sendline(pwd)
            i = s.expect ([COMMAND_PROMPT, terminal_prompt])
            if i == 1:
                s.sendline (terminal_type)
                s.expect (COMMAND_PROMPT)

        COMMAND_PROMPT = "\[PEXPECT\]\$ "
        # sh style
        s.sendline ("PS1='[PEXPECT]\$ '")
        i = s.expect ([pexpect.TIMEOUT, COMMAND_PROMPT], timeout=10)
        if i == 0:
            # csh-style.
            s.sendline ("set prompt='[PEXPECT]\$ '")
            i = s.expect ([pexpect.TIMEOUT, COMMAND_PROMPT], timeout=10)
            if i == 0:
                return None
        return s

    @return_exceptions
    def _create_ssh_connection(self, ip, user, pwd, port=None):
        if PEXPECT_VERSION == NEW_MODULE:
            return self._login_remote_system(ip, user, pwd, port)

        if PEXPECT_VERSION == OLD_MODULE:
            return self._spawn_remote_system(ip, user, pwd, port)

        return None

    @return_exceptions
    def _execute_system_command(self, conn, cmd):
        if not conn or not cmd or PEXPECT_VERSION == NO_MODULE:
            return None

        conn.sendline(cmd)
        if PEXPECT_VERSION == NEW_MODULE:
            conn.prompt()
        elif PEXPECT_VERSION == OLD_MODULE:
            conn.expect (COMMAND_PROMPT)
        else:
            return None
        return conn.before

    @return_exceptions
    def _stop_ssh_connection(self, conn):
        if not conn or PEXPECT_VERSION == NO_MODULE:
            return

        if PEXPECT_VERSION == NEW_MODULE:
            conn.logout()
            if conn:
                conn.close()
        elif PEXPECT_VERSION == OLD_MODULE:
            conn.sendline ('exit')
            i = conn.expect([pexpect.EOF, "(?i)there are stopped jobs"])
            if i==1:
                conn.sendline("exit")
                conn.expect(pexpect.EOF)
            if conn:
                conn.close()

    @return_exceptions
    def _get_remote_host_system_statistics(self, commands):
        sys_stats = {}

        if PEXPECT_VERSION == NO_MODULE:
            self.logger.error("No module named pexpect. Please install it to collect remote server system statistics.")
            return sys_stats

        sys_stats_collected = False
        self._set_system_credentials(use_cached_credentials=True)
        # 1 for previous saved credential and one from new inputs
        max_credential_set_tries = 2
        tries = 0

        while(tries < max_credential_set_tries and not sys_stats_collected):
            tries += 1
            s = None

            try:
                s = self._create_ssh_connection(self.ip, self.sys_user_id, self.sys_pwd, self.sys_ssh_port)
                if not s or isinstance(s, Exception):
                    s = None
                    raise
            except Exception:
                if tries < max_credential_set_tries:
                    self._set_system_credentials()
                self.logger.error("Couldn't make SSH login to remote server %s:%s, please provide correct credentials."%(str(self.ip), "22" if self.sys_ssh_port is None else str(self.sys_ssh_port)))
                if s:
                    s.close()
                continue

            try:
                for _key, cmds in self.sys_cmds:
                    if _key not in commands:
                        continue

                    for cmd in cmds:
                        try:
                            o = self._execute_system_command(s, cmd)
                            if not o or isinstance(o, Exception):
                                continue
                            parse_system_live_command(_key, o, sys_stats)
                            break

                        except Exception:
                            pass

                sys_stats_collected = True
                self._stop_ssh_connection(s)

            except Exception:
                if tries < max_credential_set_tries:
                    self._set_system_credentials()
                self.logger.error("Couldn't get or parse remote system stats.")
                pass

            finally:
                if s and not isinstance(s, Exception):
                    s.close()

        return sys_stats
