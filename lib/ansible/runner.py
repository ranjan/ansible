# Copyright (c) 2012 Michael DeHaan <michael.dehaan@gmail.com>
#
# Permission is hereby granted, free of charge, to any person 
# obtaining a copy of this software and associated documentation 
# files (the "Software"), to deal in the Software without restriction, 
# including without limitation the rights to use, copy, modify, merge, 
# publish, distribute, sublicense, and/or sell copies of the Software, 
# and to permit persons to whom the Software is furnished to do so, 
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be 
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, 
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF 
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. 
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR 
# ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF 
# CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION 
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import fnmatch
import multiprocessing
import os
import json
import traceback

# non-core 
import paramiko

import constants as C

def _executor_hook(x):
    ''' callback used by multiprocessing pool '''
    (runner, host) = x
    return runner._executor(host)

class Runner(object):

   def __init__(self, 
       host_list=C.DEFAULT_HOST_LIST, 
       module_path=C.DEFAULT_MODULE_PATH,
       module_name=C.DEFAULT_MODULE_NAME, 
       module_args=C.DEFAULT_MODULE_ARGS, 
       forks=C.DEFAULT_FORKS, 
       timeout=C.DEFAULT_TIMEOUT, 
       pattern=C.DEFAULT_PATTERN,
       remote_user=C.DEFAULT_REMOTE_USER,
       remote_pass=C.DEFAULT_REMOTE_PASS,
       verbose=False):
      

       '''
       Constructor.
       '''

       self.host_list   = self._parse_hosts(host_list)
       self.module_path = module_path
       self.module_name = module_name
       self.forks       = forks
       self.pattern     = pattern
       self.module_args = module_args
       self.timeout     = timeout
       self.verbose     = verbose
       self.remote_user = remote_user
       self.remote_pass = remote_pass

   def _parse_hosts(self, host_list):
        ''' parse the host inventory file if not sent as an array '''
        if type(host_list) != list:
            host_list = os.path.expanduser(host_list)
            return file(host_list).read().split("\n")
        return host_list


   def _matches(self, host_name, pattern=None):
       ''' returns if a hostname is matched by the pattern '''
       if host_name == '':
           return False
       if not pattern:
           pattern = self.pattern
       if fnmatch.fnmatch(host_name, pattern):
           return True
       return False

   def _connect(self, host):
       ''' 
       obtains a paramiko connection to the host.
       on success, returns (True, connection) 
       on failure, returns (False, traceback str)
       '''
       ssh = paramiko.SSHClient()
       ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
       try:
          ssh.connect(host, username=self.remote_user, allow_agent=True, 
              look_for_keys=True, password=self.remote_pass)
          return [ True, ssh ]
       except:
          return [ False, traceback.format_exc() ]

   def _executor(self, host):
       ''' 
       callback executed in parallel for each host.
       returns (hostname, connected_ok, extra)
       where extra is the result of a successful connect
       or a traceback string
       '''
       # TODO: try/catch around JSON handling

       ok, conn = self._connect(host)
       if not ok:
           return [ host, False, conn ]

       if self.module_name not in [ 'copy', 'template' ]:
           # transfer a module, set it executable, and run it
           outpath = self._copy_module(conn)
           self._exec_command(conn, "chmod +x %s" % outpath)
           cmd = self._command(outpath)
           result = self._exec_command(conn, cmd)
           self._exec_command(conn, "rm -f %s" % outpath)
           conn.close()
           try:
               return [ host, True, json.loads(result) ]
           except:
               return [ host, False, result ]

       elif self.module_name == 'copy':

           # TODO: major refactoring pending
           # do sftp then run actual copy module to get change info

           self.remote_log(conn, 'COPY remote:%s local:%s' % (self.module_args[0], self.module_args[1]))
           source = self.module_args[0]
           dest   = self.module_args[1]
           tmp_dest = self._get_tmp_path(conn, dest.split("/")[-1])

           ftp = conn.open_sftp()
           ftp.put(source, tmp_dest)
           ftp.close()

           # install the copy  module

           self.module_name = 'copy'
           outpath = self._copy_module(conn)
           self._exec_command(conn, "chmod +x %s" % outpath)

           # run the copy module

           self.module_args = [ tmp_dest, dest ]
           cmd = self._command(outpath)
           result = self._exec_command(conn, cmd)
 
           # remove the module 
           self._exec_command(conn, "rm -f %s" % outpath)
           # remove the temp file
           self._exec_command(conn, "rm -f %s" % tmp_dest)

           conn.close()
           try:
               return [ host, True, json.loads(result) ]
           except:
               traceback.print_exc()
               return [ host, False, result ]

           return [ host, True, 1 ]

       elif self.module_name == 'template':
           # template runs COPY then the template module
           # TODO: DRY/refactor these
           # TODO: things like _copy_module should take the name as a param
           # TODO: make it possible to override the /etc/ansible/setup file
           #       location for templating files as non-root

           source   = self.module_args[0]
           dest     = self.module_args[1]
           metadata = '/etc/ansible/setup'

           # first copy the source template over
           tempname = os.path.split(source)[-1]
           temppath = self._get_tmp_path(conn, tempname)
           self.remote_log(conn, 'COPY remote:%s local:%s' % (source, temppath))
           ftp = conn.open_sftp()
           ftp.put(source, temppath)
           ftp.close()

           # install the template module
           self.module_name = 'template'
           outpath = self._copy_module(conn)
           self._exec_command(conn, "chmod +x %s" % outpath)

           # run the template module
           self.module_args = [ temppath, dest, metadata ]
           result = self._exec_command(conn, self._command(outpath))
           # clean up
           self._exec_command(conn, "rm -f %s" % outpath)
           self._exec_command(conn, "rm -f %s" % temppath)

           conn.close()
           try:
               return [ host, True, json.loads(result) ]
           except:
               traceback.print_exc()
               return [ host, False, result ]

           return [ host, False, 1 ]

   def _command(self, outpath):
       ''' form up a command string '''
       cmd = "%s %s" % (outpath, " ".join(self.module_args))
       return cmd

   
   def remote_log(self, conn, msg):
       stdin, stdout, stderr = conn.exec_command('/usr/bin/logger -t ansible -p auth.info %r' % msg)

   def _exec_command(self, conn, cmd):
       ''' execute a command over SSH '''
       msg = '%s: %s' % (self.module_name, cmd)
       self.remote_log(conn, msg)
       stdin, stdout, stderr = conn.exec_command(cmd)
       results = "\n".join(stdout.readlines())
       return results

   def _get_tmp_path(self, conn, file_name):
       output = self._exec_command(conn, "mktemp /tmp/%s.XXXXXX" % file_name)
       return output.split("\n")[0]

   def _copy_module(self, conn):
       ''' transfer a module over SFTP '''
       in_path = os.path.expanduser(
           os.path.join(self.module_path, self.module_name)
       )
       out_path = self._get_tmp_path(conn, "ansible_%s" % self.module_name)

       sftp = conn.open_sftp()
       sftp.put(in_path, out_path)
       sftp.close()
       return out_path

   def match_hosts(self, pattern=None):
       ''' return all matched hosts '''
       return [ h for h in self.host_list if self._matches(h, pattern) ]

   def run(self):
       ''' xfer & run module on all matched hosts '''

       # find hosts that match the pattern
       hosts = self.match_hosts()

       # attack pool of hosts in N forks
       hosts = [ (self,x) for x in hosts ]
       if self.forks > 1:
          pool = multiprocessing.Pool(self.forks)
          results = pool.map(_executor_hook, hosts)
       else:
          results = [ _executor_hook(x) for x in hosts ]

       # sort hosts by ones we successfully contacted
       # and ones we did not
       results2 = {
          "contacted" : {},
          "dark"      : {}
       }
       for x in results:
           (host, is_ok, result) = x
           if not is_ok:
               results2["dark"][host] = result
           else:
               results2["contacted"][host] = result

       return results2


if __name__ == '__main__':

    # test code...

    r = Runner(
       host_list = DEFAULT_HOST_LIST,
       module_name='ping',
       module_args='',
       pattern='*',
       forks=3
    )   
    print r.run()

 

