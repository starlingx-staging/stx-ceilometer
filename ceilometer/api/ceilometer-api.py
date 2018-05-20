from ceilometer.cmd import api as build_wsgi_app
import sys
from tsconfig.tsconfig import SW_VERSION

sys.argv = sys.argv[:1]
args = {
    'config_file': 'etc/ceilometer/ceilometer.conf',
    'pipeline_cfg_file':
    '/opt/cgcs/ceilometer/' + str(SW_VERSION) + '/pipeline.yaml', }
application = build_wsgi_app.build_wsgi_app(args=args)
