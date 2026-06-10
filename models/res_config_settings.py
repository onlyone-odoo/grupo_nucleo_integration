# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

import logging

from odoo import _, api, fields, models

from .gruponucleo_api import GrupNucleoAPI, GrupNucleoAPIError

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    gn_api_id = fields.Integer(
        string="Grupo Núcleo API ID",
        config_parameter="grupo_nucleo_integration.gn_api_id",
        help="Numeric ID provided by Grupo Núcleo for API access.",
    )
    gn_username = fields.Char(
        string="Grupo Núcleo Username",
        config_parameter="grupo_nucleo_integration.gn_username",
    )
    gn_password = fields.Char(
        string="Grupo Núcleo Password",
        config_parameter="grupo_nucleo_integration.gn_password",
    )
    gn_base_url = fields.Char(
        string="Grupo Núcleo API URL",
        default="https://api.gruponucleosa.com",
        config_parameter="grupo_nucleo_integration.gn_base_url",
        help="Base URL of the API (production: https://api.gruponucleosa.com).",
    )
    gn_send_orders_on_confirm = fields.Boolean(
        string="Enviar pedidos a Grupo Núcleo al confirmar",
        config_parameter="grupo_nucleo_integration.gn_send_orders_on_confirm",
        help="When enabled, sale orders marked for Grupo Núcleo will be sent on confirm.",
    )
    gn_partner_id = fields.Many2one(
        "res.partner",
        string="Proveedor Grupo Núcleo",
        config_parameter="grupo_nucleo_integration.gn_partner_id",
        domain="[('supplier_rank', '>', 0)]",
        help="Partner used as supplier on purchase orders created when sending orders to GN.",
    )
    gn_company_id = fields.Many2one(
        "res.company",
        string="Compañía datos GN",
        config_parameter="grupo_nucleo_integration.gn_company_id",
        help="Compañía para los datos de proveedor sincronizados desde Grupo Núcleo "
        "(listas de precios de proveedor, impuestos y costos). "
        "Vacío = compartido entre todas las compañías.",
    )
    gn_stock_source = fields.Selection(
        [
            ("stock_mdp", "Mar del Plata (stock_mdp)"),
            ("stock_caba", "Buenos Aires (stock_caba)"),
            ("sum", "Suma de ambos (stock_mdp + stock_caba)"),
        ],
        string="Stock del proveedor a usar",
        default="sum",
        config_parameter="grupo_nucleo_integration.gn_stock_source",
        help="Which supplier stock to use for stock_gn: Mar del Plata, Buenos Aires, or sum of both.",
    )
    gn_public_categ_parent_id = fields.Many2one(
        "product.public.category",
        string="Categoría padre ecommerce",
        config_parameter="grupo_nucleo_integration.gn_public_categ_parent_id",
        help="Categoría raíz bajo la cual se crean categoría/subcategoría de GN para publicar en la tienda.",
    )
    gn_allow_out_of_stock_order = fields.Boolean(
        string="Habilitar venta sin stock (productos GN)",
        config_parameter="grupo_nucleo_integration.gn_allow_out_of_stock_order",
        help="Si está activo, los productos sincronizados de Grupo Núcleo tendrán: "
        "1) 'Permitir pedido sin stock' en la tienda (allow_out_of_stock_order) y "
        "2) las rutas MTO (id=1) y Comprar (id=5) para reabastecer bajo pedido vía "
        "orden de compra. Se aplica al importar y al actualizar.",
    )
    gn_publish_stock_threshold = fields.Integer(
        string="Umbral de stock para publicar (GN)",
        config_parameter="grupo_nucleo_integration.gn_publish_stock_threshold",
        default=3,
        help="Productos GN con stock_gn mayor que este valor se publican en la tienda; con stock_gn <= umbral se despublican. Usado por la acción planificada 'Publicar/despublicar productos GN por stock'.",
    )

    # API health check
    gn_api_notify_user_id = fields.Many2one(
        "res.users",
        string="Usuario a notificar (API GN)",
        config_parameter="grupo_nucleo_integration.gn_api_notify_user_id",
        help="Recibe mensaje interno en Discuss si la API de Grupo Núcleo no responde.",
    )
    gn_api_status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("unknown", "Sin verificar")],
        string="Estado API GN",
        compute="_compute_gn_api_status",
    )
    gn_api_last_check = fields.Datetime(
        string="Último chequeo API GN",
        compute="_compute_gn_api_status",
    )
    gn_api_last_error = fields.Char(
        string="Último error API GN",
        compute="_compute_gn_api_status",
    )

    # Sync watchdog (last completed cycle per sync type)
    gn_catalog_last_done = fields.Datetime(
        string="Última sync catálogo GN completada",
        compute="_compute_gn_sync_status",
    )
    gn_price_stock_last_done = fields.Datetime(
        string="Última sync precio/stock GN completada",
        compute="_compute_gn_sync_status",
    )
    gn_sync_stale_info = fields.Char(
        string="Syncs GN vencidas",
        compute="_compute_gn_sync_status",
        help="Sincronizaciones que superaron el umbral máximo sin completarse.",
    )

    @api.depends("gn_api_id")
    def _compute_gn_api_status(self):
        # sudo() for ir.config_parameter: required to read system params from settings/compute context
        # (safe: no user data, only module config).
        ICP = self.env["ir.config_parameter"].sudo()
        status = ICP.get_param("grupo_nucleo_integration.api_status", "unknown")
        last_check = ICP.get_param("grupo_nucleo_integration.api_last_check", False)
        last_error = ICP.get_param("grupo_nucleo_integration.api_last_error", False)
        for rec in self:
            rec.gn_api_status = status if status in ("ok", "error") else "unknown"
            rec.gn_api_last_check = last_check or False
            rec.gn_api_last_error = last_error or False

    @api.depends("gn_api_id")
    def _compute_gn_sync_status(self):
        # sudo() for ir.config_parameter: safe, only module config flags.
        ICP = self.env["ir.config_parameter"].sudo()
        catalog_last = ICP.get_param("grupo_nucleo_integration.catalog_last_done", False)
        price_stock_last = ICP.get_param("grupo_nucleo_integration.price_stock_last_done", False)
        stale = self.env["product.template"]._gn_get_stale_syncs(ICP)
        stale_info = ", ".join(label for label, _last in stale) if stale else False
        for rec in self:
            rec.gn_catalog_last_done = catalog_last or False
            rec.gn_price_stock_last_done = price_stock_last or False
            rec.gn_sync_stale_info = stale_info

    def get_gruponucleo_api(self) -> GrupNucleoAPI | None:
        """
        Build API client from current config. Returns None if credentials are missing.
        """
        # sudo() for ir.config_parameter: safe for reading module configuration parameters.
        ICP = self.env["ir.config_parameter"].sudo()
        api_id = int(ICP.get_param("grupo_nucleo_integration.gn_api_id", "0") or "0")
        username = ICP.get_param("grupo_nucleo_integration.gn_username", "").strip()
        password = ICP.get_param("grupo_nucleo_integration.gn_password", "")
        base_url = (
            ICP.get_param("grupo_nucleo_integration.gn_base_url", "").strip()
            or "https://api.gruponucleosa.com"
        )
        if not api_id or not username or not password:
            return None
        return GrupNucleoAPI(base_url=base_url, api_id=api_id, username=username, password=password)

    def action_test_gruponucleo_connection(self):
        """Test API connection (login + optional GetCatalog)."""
        self.ensure_one()
        api_client = self.get_gruponucleo_api()
        if not api_client:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Error"),
                    "message": _("Configure API ID, username and password first."),
                    "type": "danger",
                    "sticky": False,
                },
            }
        try:
            api_client._get_token()
            api_client.get_catalog()
        except GrupNucleoAPIError as e:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection failed"),
                    "message": str(e),
                    "type": "danger",
                    "sticky": True,
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Success"),
                "message": _("Connection to Grupo Núcleo API successful."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_gruponucleo_catalog(self):
        """Trigger catalog sync from Settings."""
        return self.env["product.template"].action_sync_gruponucleo_catalog()
