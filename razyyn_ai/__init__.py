from . import models
from . import controllers


def post_init_hook(env):
    """Fetch what reading a scanned invoice needs, so nobody installs it by hand.

    Runs once, when the module is installed. An upgrade takes
    migrations/1.2.0/post-migration.py instead, because Odoo runs this hook
    only on a NEW install -- see `if new_install:` in odoo/modules/loading.py.

    It cannot fail the install: see services/dependencies.py.
    """
    from .services import dependencies

    dependencies.ensure()
