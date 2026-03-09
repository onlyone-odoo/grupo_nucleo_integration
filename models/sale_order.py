# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import _, fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    gn_create_po_on_confirm = fields.Boolean(
        string="Crear OC Grupo Núcleo al confirmar",
        default=False,
        copy=False,
        help="When checked, confirming the SO creates a purchase order for Grupo Núcleo "
             "with the lines that have gn_item_id. The PO must then be sent to GN via the "
             "button in the purchase order.",
    )
    purchase_order_id = fields.Many2one(
        "purchase.order",
        string="Orden de compra Grupo Núcleo",
        copy=False,
        readonly=True,
        help="Purchase order created for Grupo Núcleo when this SO was confirmed.",
    )
    gn_order_ref = fields.Char(
        string="Referencia Grupo Núcleo",
        related="purchase_order_id.gn_order_ref",
        readonly=True,
    )
    gn_sync_error = fields.Text(
        string="Error envío Grupo Núcleo",
        related="purchase_order_id.gn_sync_error",
        readonly=True,
    )

    def action_confirm(self):
        """Override: create GN purchase order when gn_create_po_on_confirm is set."""
        res = super().action_confirm()
        for order in self:
            if order.gn_create_po_on_confirm and not order.purchase_order_id:
                order._create_gruponucleo_purchase_order()
        return res

    def _create_gruponucleo_purchase_order(self):
        """
        Create a purchase order for this sale order with lines that have gn_item_id.

        The PO is created confirmed so pickings are generated. Sending to GN API
        is done from the PO (button), not from the SO.
        """
        self.ensure_one()
        if self.purchase_order_id:
            return
        partner = self.env["purchase.order"]._get_gruponucleo_partner()
        order_lines = []
        for line in self.order_line:
            if (
                not line.product_id
                or not line.product_uom_qty
                or not line.product_id.product_tmpl_id.gn_item_id
            ):
                continue
            price = line.price_unit if line.price_unit else line.product_id.list_price
            order_lines.append(
                (
                    0,
                    0,
                    {
                        "product_id": line.product_id.id,
                        "product_qty": line.product_uom_qty,
                        "product_uom": line.product_uom.id,
                        "price_unit": price,
                        "name": line.name or line.product_id.display_name,
                    },
                )
            )
        if not order_lines:
            return
        po = self.env["purchase.order"].create({
            "partner_id": partner.id,
            "origin": self.name,
            "order_line": order_lines,
            "gn_sale_order_id": self.id,
        })
        po.button_confirm()
        self.purchase_order_id = po.id

    def action_view_purchase_order_gn(self):
        """Open the linked Grupo Núcleo purchase order."""
        self.ensure_one()
        if not self.purchase_order_id:
            return
        return {
            "type": "ir.actions.act_window",
            "res_model": "purchase.order",
            "res_id": self.purchase_order_id.id,
            "views": [(False, "form")],
            "context": {"create": False},
        }
