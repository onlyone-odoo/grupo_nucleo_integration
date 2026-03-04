# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import _, fields, models

from .gruponucleo_api import GrupNucleoAPIError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    gn_send_on_confirm = fields.Boolean(
        string="Enviar a Grupo Núcleo",
        default=False,
        copy=False,
        help="When checked, this order will be sent to Grupo Núcleo when confirmed.",
    )
    gn_order_sent = fields.Boolean(
        string="Enviado a Grupo Núcleo",
        default=False,
        copy=False,
        readonly=True,
    )
    gn_order_ref = fields.Char(
        string="Referencia Grupo Núcleo",
        copy=False,
        readonly=True,
    )
    gn_sync_error = fields.Text(
        string="Error envío Grupo Núcleo",
        copy=False,
        readonly=True,
    )
    purchase_order_id = fields.Many2one(
        "purchase.order",
        string="Orden de compra Grupo Núcleo",
        copy=False,
        readonly=True,
        help="Purchase order created for receiving when this order was sent to GN.",
    )

    def action_confirm(self):
        """Override: send to Grupo Núcleo when gn_send_on_confirm is set (after confirm)."""
        res = super().action_confirm()
        for order in self:
            if order.gn_send_on_confirm and not order.gn_order_sent:
                order._send_to_gruponucleo()
        return res

    def _send_to_gruponucleo(self):
        """
        Send this sale order to Grupo Núcleo (NewSelfSaleOrder).
        Uses CheckoutConfirm for first 15 lines, then NewSelfSaleOrder.
        Sets gn_order_sent, gn_order_ref on success; gn_sync_error on failure.
        """
        self.ensure_one()
        self.gn_sync_error = False
        api_client = self.env["res.config.settings"].get_gruponucleo_api()
        if not api_client:
            self.gn_sync_error = _("Grupo Núcleo API is not configured (Settings).")
            return
        # Build lines with gn_item_id (from product_id.product_tmpl_id.gn_item_id or product_id)
        lines_with_gn = []
        for line in self.order_line:
            if line.product_id and line.product_uom_qty:
                product = line.product_id
                # gn_item_id on product.template
                item_id = product.product_tmpl_id.gn_item_id
                if not item_id:
                    self.gn_sync_error = _(
                        "Product '%s' has no Grupo Núcleo Item ID. Sync catalog or set it manually."
                    ) % (product.display_name,)
                    return
                lines_with_gn.append((item_id, int(line.product_uom_qty)))
        if not lines_with_gn:
            self.gn_sync_error = _("No lines with quantity to send.")
            return
        # CheckoutConfirm accepts max 15 items
        item_ids = [item_id for item_id, _qty in lines_with_gn]
        if len(item_ids) > 15:
            self.gn_sync_error = _(
                "Grupo Núcleo allows at most 15 items per order. This order has %s lines."
            ) % len(item_ids)
            return
        try:
            api_client.checkout_confirm(item_ids)
        except GrupNucleoAPIError as e:
            self.gn_sync_error = _("CheckoutConfirm failed: %s") % str(e)
            return
        # Build nota (max 350 chars)
        nota_parts = []
        if self.note:
            nota_parts.append(self.note)
        if self.client_order_ref:
            nota_parts.append(str(self.client_order_ref))
        nota = " | ".join(nota_parts)[:350] if nota_parts else ""
        items_payload = [{"item_id": iid, "item_qty": qty} for iid, qty in lines_with_gn]
        try:
            result = api_client.new_self_sale_order(nota=nota, items=items_payload)
        except GrupNucleoAPIError as e:
            self.gn_sync_error = _("NewSelfSaleOrder failed: %s") % str(e)
            return
        # Success: store reference if API returns one
        order_ref = None
        if isinstance(result, dict):
            order_ref = (
                result.get("pedido")
                or result.get("order_id")
                or result.get("order_ref")
                or result.get("id")
            )
            if order_ref is not None:
                order_ref = str(order_ref)
        self.write({
            "gn_order_sent": True,
            "gn_order_ref": order_ref or False,
            "gn_sync_error": False,
        })
        # Create purchase order for native receiving flow (picking) linked to this SO
        self._create_gruponucleo_purchase_order(order_ref)

    def _get_gruponucleo_partner(self):
        """Return the partner to use as supplier for GN purchase orders."""
        ICP = self.env["ir.config_parameter"].sudo()
        partner_id = ICP.get_param("grupo_nucleo_integration.gn_partner_id")
        if partner_id:
            partner = self.env["res.partner"].browse(int(partner_id))
            if partner.exists():
                return partner
        partner = self.env["res.partner"].search(
            [("name", "ilike", "Grupo Núcleo"), ("supplier_rank", ">", 0)],
            limit=1,
        )
        if partner:
            return partner
        return self.env["res.partner"].create({
            "name": "Grupo Núcleo S.A.",
            "supplier_rank": 1,
            "is_company": True,
        })

    def _create_gruponucleo_purchase_order(self, gn_order_ref=None):
        """
        Create a purchase order for this sale order so the client has the native
        flow: picking to receive, linked to the order sent to GN via API.
        PO is confirmed so pickings are created.
        """
        self.ensure_one()
        if self.purchase_order_id:
            return
        partner = self._get_gruponucleo_partner()
        origin = self.name
        if gn_order_ref:
            origin = _("%s (GN: %s)") % (self.name, gn_order_ref)
        order_lines = []
        for line in self.order_line:
            if not line.product_id or not line.product_uom_qty or not line.product_id.product_tmpl_id.gn_item_id:
                continue
            # Use sale line price or product list price for PO line
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
            "origin": origin,
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
