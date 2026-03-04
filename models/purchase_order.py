# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    gn_sale_order_id = fields.Many2one(
        "sale.order",
        string="Orden de venta Grupo Núcleo",
        copy=False,
        readonly=True,
        help="Sale order that was sent to Grupo Núcleo and generated this purchase order.",
    )

    def action_view_sale_order_gn(self):
        """Open the linked sale order that was sent to Grupo Núcleo."""
        self.ensure_one()
        if not self.gn_sale_order_id:
            return
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": self.gn_sale_order_id.id,
            "views": [(False, "form")],
            "context": {"create": False},
        }
