import sys
sys.path.insert(0, '/Users/brassfieldventuresllc/.hermes/hermes-agent')
from hermes_cli.plugins import PluginManager
PluginManager().discover_and_load(force=False)
from gateway.config import load_gateway_config
load_gateway_config()
load_gateway_config()
