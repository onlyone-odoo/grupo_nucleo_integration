# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from . import models


def post_init_hook(env):
    """Migrate existing default_code to gn_product_code for GN products."""
    env.cr.execute("""
        UPDATE product_template
        SET gn_product_code = default_code
        WHERE gn_item_id IS NOT NULL
          AND gn_item_id != 0
          AND default_code IS NOT NULL
          AND default_code != ''
          AND (gn_product_code IS NULL OR gn_product_code = '')
    """)
