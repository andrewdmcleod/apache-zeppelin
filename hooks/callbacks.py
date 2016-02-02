import os
from path import Path
from subprocess import call

import jujuresources
from charmhelpers.core import hookenv
from charmhelpers.core import host
from charmhelpers.core import unitdata
from jujubigdata import utils
from jujubigdata.relations import Spark


# Extended status support
# We call update_blocked_status from the "requires" section of our service
# block, so be sure to return True. Otherwise, we'll block the "requires"
# and never move on to callbacks. The other status update methods are called
# from the "callbacks" section and therefore don't need to return True.
def update_blocked_status():
    if unitdata.kv().get('charm.active', False):
        return True
    spark = Spark()
    if not spark.is_ready():
        hookenv.status_set('waiting', 'Waiting for Spark to become ready')
    return True


def update_working_status():
    if unitdata.kv().get('charm.active', False):
        hookenv.status_set('maintenance', 'Updating configuration')
        return
    hookenv.status_set('maintenance', 'Setting up Apache Zeppelin')


def update_active_status():
    unitdata.kv().set('charm.active', True)
    hookenv.status_set('active', 'Ready')


# Main Zeppelin class for callbacks
class Zeppelin(object):
    def __init__(self, dist_config):
        self.dist_config = dist_config
        self.resources = {
            'zeppelin': 'zeppelin-%s' % host.cpu_arch(),
        }
        self.verify_resources = utils.verify_resources(*self.resources.values())

    def is_installed(self):
        return unitdata.kv().get('zeppelin.installed')

    def install(self, force=False):
        if not force and self.is_installed():
            return
        self.dist_config.add_dirs()
        self.dist_config.add_packages()
        jujuresources.install(self.resources['zeppelin'],
                              destination=self.dist_config.path('zeppelin'),
                              skip_top_level=True)
        self.setup_zeppelin_config()
        self.setup_zeppelin_tutorial()

        unitdata.kv().set('zeppelin.installed', True)
        unitdata.kv().flush(True)

    def setup_zeppelin_config(self):
        '''
        copy the default configuration files to zeppelin_conf property
        defined in dist.yaml
        '''
        default_conf = self.dist_config.path('zeppelin') / 'conf'
        zeppelin_conf = self.dist_config.path('zeppelin_conf')
        zeppelin_conf.rmtree_p()
        default_conf.copytree(zeppelin_conf)
        # Now remove the conf included in the tarball and symlink our real conf
        # dir. we've seen issues where zepp doesn't honor ZEPPELIN_CONF_DIR
        # and instead looks for config in ZEPPELIN_HOME/conf.
        default_conf.rmtree_p()
        zeppelin_conf.symlink(default_conf)

        zeppelin_env = self.dist_config.path('zeppelin_conf') / 'zeppelin-env.sh'
        if not zeppelin_env.exists():
            (self.dist_config.path('zeppelin_conf') / 'zeppelin-env.sh.template').copy(zeppelin_env)
        zeppelin_site = self.dist_config.path('zeppelin_conf') / 'zeppelin-site.xml'
        if not zeppelin_site.exists():
            (self.dist_config.path('zeppelin_conf') / 'zeppelin-site.xml.template').copy(zeppelin_site)

    def setup_zeppelin_tutorial(self):
        # The default zepp tutorial doesn't work with spark+hdfs (which is our
        # default env). Include our own tutorial, which does work in a
        # spark+hdfs env. Inspiration for this notebook came from here:
        #   https://github.com/apache/incubator-zeppelin/pull/46
        notebook_dir = self.dist_config.path('zeppelin_notebooks')
        dist_notebook_dir = self.dist_config.path('zeppelin') / 'notebook'
        dist_tutorial_dir = dist_notebook_dir.dirs()[0]
        dist_tutorial_dir.move(notebook_dir)
        self.copy_tutorial("hdfs-tutorial")
        self.copy_tutorial("flume-tutorial")
        self.copy_tutorial("ngram-tutorial")
        dist_notebook_dir.rmtree_p()
        # move the tutorial dir included in the tarball to our notebook dir and
        # symlink that dir under our zeppelin home. we've seen issues where
        # zepp doesn't honor ZEPPELIN_NOTEBOOK_DIR and instead looks for
        # notebooks in ZEPPELIN_HOME/notebook.
        notebook_dir.symlink(dist_notebook_dir)

        # make sure the notebook dir's contents are owned by our user
        cmd = "chown -R ubuntu:hadoop {}".format(notebook_dir)
        call(cmd.split())
        
        
    def copy_tutorial(self, tutorial_name):
        tutorial_source = Path('resources/{}'.format(tutorial_name))
        tutorial_source.copytree(self.dist_config.path('zeppelin_notebooks') / tutorial_name)

        

    def configure_zeppelin(self):
        '''
        Configure zeppelin environment for all users
        '''
        zeppelin_bin = self.dist_config.path('zeppelin') / 'bin'
        with utils.environment_edit_in_place('/etc/environment') as env:
            if zeppelin_bin not in env['PATH']:
                env['PATH'] = ':'.join([env['PATH'], zeppelin_bin])
            env['ZEPPELIN_CONF_DIR'] = self.dist_config.path('zeppelin_conf')

        zeppelin_site = self.dist_config.path('zeppelin_conf') / 'zeppelin-site.xml'
        with utils.xmlpropmap_edit_in_place(zeppelin_site) as xml:
            xml['zeppelin.server.port'] = self.dist_config.port('zeppelin')
            xml['zeppelin.websocket.port'] = self.dist_config.port('zeppelin_web')
            xml['zeppelin.notebook.dir'] = self.dist_config.path('zeppelin_notebooks')

        hadoop_conf_dir = os.environ.get('HADOOP_CONF_DIR', '/etc/hadoop/conf')
        spark_home = os.environ.get('SPARK_HOME', '/usr/lib/spark')
        spark_exe_mode = os.environ.get('MASTER', 'yarn-client')
        zeppelin_env = self.dist_config.path('zeppelin_conf') / 'zeppelin-env.sh'
        self.re_edit_in_place(zeppelin_env, {
            r'.*export ZEPPELIN_HOME.*': 'export ZEPPELIN_HOME={}'.format(self.dist_config.path('zeppelin')),
            r'.*export ZEPPELIN_LOG_DIR.*': 'export ZEPPELIN_LOG_DIR={}'.format(self.dist_config.path('zeppelin_logs')),
            r'.*export ZEPPELIN_NOTEBOOK_DIR.*': 'export ZEPPELIN_NOTEBOOK_DIR={}'.format(self.dist_config.path('zeppelin_notebooks')),
            r'.*export SPARK_HOME.*': 'export SPARK_HOME={}'.format(spark_home),
            r'.*export HADOOP_CONF_DIR.*': 'export HADOOP_CONF_DIR={}'.format(hadoop_conf_dir),
            r'.*export PYTHONPATH.*': 'export PYTHONPATH={s}/python:{s}/python/lib/py4j-0.8.2.1-src.zip'.format(
                s=spark_home),
            r'.*export MASTER.*': 'export MASTER={}'.format(spark_exe_mode),
            r'.*export SPARK_YARN_USER_ENV.*': 'export SPARK_YARN_USER_ENV="PYTHONPATH=${PYTHONPATH}"',
        }, add_if_not_found=True)

        # User needs write access to zepp's conf to write interpreter.json
        # on server start. chown the whole conf dir, though we could probably
        # touch that file and chown it, leaving the rest owned as root:root.
        # TODO: weigh implications of have zepp's conf dir owned by non-root.
        cmd = "chown -R ubuntu:hadoop {}".format(self.dist_config.path('zeppelin_conf'))
        call(cmd.split())

    def start(self):
        # Start if we're not already running. We currently dont have any
        # runtime config options, so no need to restart when hooks fire.
        if not utils.jps("zeppelin"):
            zeppelin_conf = self.dist_config.path('zeppelin_conf')
            zeppelin_home = self.dist_config.path('zeppelin')
            # chdir here because things like zepp tutorial think ZEPPELIN_HOME
            # is wherever the daemon was started from.
            os.chdir(zeppelin_home)
            utils.run_as('ubuntu', '{}/bin/zeppelin-daemon.sh'.format(zeppelin_home), '--config', zeppelin_conf, 'start')

    def stop(self):
        zeppelin_conf = self.dist_config.path('zeppelin_conf')
        zeppelin_home = self.dist_config.path('zeppelin')
        # TODO: try/catch existence of zeppelin-daemon.sh. Stop hook will fail
        # if we try to destroy a deployment that didn't finish installing.
        utils.run_as('ubuntu', '{}/bin/zeppelin-daemon.sh'.format(zeppelin_home), '--config', zeppelin_conf, 'stop')

    def cleanup(self):
        self.dist_config.remove_dirs()
        unitdata.kv().set('zeppelin.installed', False)

    '''
    Need to move to charmhelper
    '''
    def re_edit_in_place(self, filename, subs, add_if_not_found=False):
        """
        Perform a set of in-place edits to a file.

        :param str filename: Name of file to edit
        :param dict subs: Mapping of patterns to replacement strings
        :param add_if_not_found: Append the replacement strings if patterns not found
        """
        from path import Path
        import re
        with Path(filename).in_place() as (reader, writer):
            for line in reader:
                for pat, repl in subs.iteritems():
                    line, n = re.subn(pat, repl, line)
                    if n:
                        '''
                        Found a match, remove the string from the search list,
                        update the file with the new string, then iterate
                        to the next line in the file
                        '''
                        subs.pop(pat)
                        break
                writer.write(line)
            if add_if_not_found:
                '''
                Append the remaining elements in the search list.
                '''
                for pat, repl in subs.iteritems():
                    writer.write(unicode(repl+"\n"))
