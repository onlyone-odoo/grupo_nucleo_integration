# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

import logging

from odoo import _, fields, models

from .gruponucleo_api import GrupNucleoAPIError

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    gn_sale_order_id = fields.Many2one(
        "sale.order",
        string="Orden de venta Grupo Núcleo",
        copy=False,
        readonly=True,
        help="Sale order that originated this purchase order for Grupo Núcleo.",
    )
    gn_order_ref = fields.Char(
        string="Referencia Grupo Núcleo",
        copy=False,
        readonly=True,
        help="Order reference returned by Grupo Núcleo API after successful submission (e.g. 58-189773).",
    )
    gn_sync_error = fields.Text(
        string="Error envío Grupo Núcleo",
        copy=False,
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_gruponucleo_partner(self):
        """Return the res.partner configured as Grupo Núcleo supplier."""
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

    def _is_gruponucleo_purchase(self):
        """Return True if this PO's vendor is the configured Grupo Núcleo partner."""
        self.ensure_one()
        gn_partner = self._get_gruponucleo_partner()
        return self.partner_id == gn_partner

    # ------------------------------------------------------------------
    # API submission
    # ------------------------------------------------------------------

    def action_send_to_gruponucleo(self):
        """Button action: send this purchase order to Grupo Núcleo via API."""
        self.ensure_one()
        self._send_to_gruponucleo()
        if self.gn_sync_error:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Error al enviar a Grupo Núcleo"),
                    "message": self.gn_sync_error,
                    "type": "danger",
                    "sticky": True,
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Pedido enviado a Grupo Núcleo"),
                "message": _("Referencia: %s") % (self.gn_order_ref or "-"),
                "type": "success",
                "sticky": True,
            },
        }

    def _send_to_gruponucleo(self):
        """
        Send this purchase order to Grupo Núcleo (CheckoutConfirm + NewSelfSaleOrder).

        Sets gn_order_ref on success; gn_sync_error on failure. Skips if
        already sent (gn_order_ref present).
        """
        self.ensure_one()
        self.gn_sync_error = False

        if self.gn_order_ref:
            _logger.info(
                "GN send: PO %s already sent (ref=%s), skipping.",
                self.name, self.gn_order_ref,
            )
            return

        api_client = self.env["res.config.settings"].get_gruponucleo_api()
        if not api_client:
            self.gn_sync_error = _("Grupo Núcleo API is not configured (Settings).")
            return

        lines_with_gn = []
        for line in self.order_line:
            if not line.product_id or not line.product_qty:
                continue
            item_id = line.product_id.product_tmpl_id.gn_item_id
            if not item_id:
                self.gn_sync_error = _(
                    "Product '%s' has no Grupo Núcleo Item ID. Sync catalog or set it manually."
                ) % (line.product_id.display_name,)
                return
            lines_with_gn.append((item_id, int(line.product_qty)))

        if not lines_with_gn:
            self.gn_sync_error = _("No lines with Grupo Núcleo products to send.")
            return

        item_ids = [iid for iid, _qty in lines_with_gn]
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

        nota_parts = []
        if self.origin:
            nota_parts.append(self.origin)
        nota_parts.append(self.name or "")
        nota = " | ".join(p for p in nota_parts if p)[:350]

        items_payload = [{"item_id": iid, "item_qty": qty} for iid, qty in lines_with_gn]
        try:
            result = api_client.new_self_sale_order(nota=nota, items=items_payload)
        except GrupNucleoAPIError as e:
            self.gn_sync_error = _("NewSelfSaleOrder failed: %s") % str(e)
            return

        order_ref = None
        if isinstance(result, dict):
            pedidos = result.get("pedidos") or []
            if pedidos and isinstance(pedidos, list):
                order_ref = pedidos[0].get("pedido")
            if not order_ref:
                order_ref = (
                    result.get("pedido")
                    or result.get("order_id")
                    or result.get("order_ref")
                    or result.get("id")
                )
            if order_ref is not None:
                order_ref = str(order_ref)

        self.write({
            "gn_order_ref": order_ref or False,
            "gn_sync_error": False,
        })
        _logger.info(
            "GN send: PO %s sent successfully, ref=%s",
            self.name, order_ref,
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def action_view_sale_order_gn(self):
        """Open the linked sale order that originated this purchase order."""
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
