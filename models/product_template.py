# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .gruponucleo_api import GrupNucleoAPIError

_logger = logging.getLogger(__name__)

# Batch size and config keys for resumable sync (avoids worker CPU time limit)
GN_SYNC_BATCH_SIZE = 80
GN_SYNC_OFFSET_KEY = "grupo_nucleo_integration.catalog_sync_offset"
GN_SYNC_LAST_TOTAL_KEY = "grupo_nucleo_integration.catalog_sync_last_total"
GN_SYNC_REQUESTED_DATE_KEY = "grupo_nucleo_integration.catalog_sync_requested_date"
GN_SYNC_DEACTIVATE_PENDING_KEY = "grupo_nucleo_integration.catalog_sync_deactivate_pending"
GN_PRICE_STOCK_OFFSET_KEY = "grupo_nucleo_integration.price_stock_sync_offset"
GN_PRICE_STOCK_REQUESTED_DATE_KEY = "grupo_nucleo_integration.price_stock_sync_requested_date"
GN_PRICE_STOCK_DEACTIVATE_PENDING_KEY = "grupo_nucleo_integration.price_stock_sync_deactivate_pending"
CRON_CATALOG_XML_ID = "grupo_nucleo_integration.ir_cron_sync_gruponucleo_catalog"
CRON_PRICE_STOCK_XML_ID = "grupo_nucleo_integration.ir_cron_sync_gruponucleo_price_stock"

# One-time logger for first catalog row (impuesto interno debug)
_gn_logged_first_row = False

# MTO route id (Make-to-Order). In standard Odoo databases this is always id=1.
GN_MTO_ROUTE_ID = 1

# Keys written by "price/stock only" cron (no name, image, category, no create)
GN_PRICE_STOCK_ONLY_KEYS = frozenset({
    "stock_gn", "volume", "gn_last_sync", "gn_product_code",
    "zippin_product_length", "zippin_product_width", "zippin_product_height",
    "allow_out_of_stock_order", "route_ids", "replenishment_cost_type",
})


class ProductTemplate(models.Model):
    _inherit = "product.template"

    gn_item_id = fields.Integer(
        string="Grupo Núcleo Item ID",
        index="btree_not_null",
        copy=False,
        help="ID of this product in Grupo Núcleo catalog (used for orders and sync).",
    )
    gn_product_code = fields.Char(
        string="Código Grupo Núcleo",
        index=True,
        copy=False,
        help="Código alfanumérico del producto en el catálogo de Grupo Núcleo (campo 'codigo' de la API).",
    )
    gn_last_sync = fields.Datetime(
        string="Last Grupo Núcleo Sync",
        readonly=True,
        copy=False,
    )
    is_gruponucleo_product = fields.Boolean(
        string="Vinculado a Grupo Núcleo",
        compute="_compute_is_gruponucleo_product",
        store=True,
        index=True,
        help="True if this product is linked to Grupo Núcleo catalog (gn_item_id set).",
    )
    stock_gn = fields.Float(
        string="Stock Grupo Núcleo",
        readonly=True,
        help="Supplier stock from Grupo Núcleo (source depends on Settings: MDP, CABA or sum). Used for filters/crons, not inventory moves.",
    )

    @api.depends("gn_item_id")
    def _compute_is_gruponucleo_product(self):
        """True when product is linked to GN (gn_item_id set). Recomputes for existing records on upgrade."""
        for rec in self:
            rec.is_gruponucleo_product = bool(rec.gn_item_id)

    def _action_request_full_sync(self):
        """
        Called by the daily trigger cron. Sets sync-requested date to today and activates
        the batch catalog cron so it runs every few minutes until the full catalog is done.
        """
        today_str = fields.Date.today().isoformat()
        self.env["ir.config_parameter"].sudo().set_param(
            GN_SYNC_REQUESTED_DATE_KEY,
            today_str,
        )
        try:
            self.env.ref(CRON_CATALOG_XML_ID).sudo().write({"active": True})
            _logger.info(
                "Grupo Núcleo sync: daily trigger set requested_date=%s, batch cron activated.",
                today_str,
            )
        except Exception as e:
            _logger.warning("Grupo Núcleo sync: could not activate batch cron: %s", e)

    def _cron_sync_gruponucleo_catalog(self):
        """
        Called by ir.cron every few minutes when sync is requested. Processes one batch per run.
        Only runs if GN_SYNC_REQUESTED_DATE_KEY is set to today (set by daily trigger cron).
        When the full catalog is done (next_offset == 0), clears the flag and deactivates this cron.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        requested = (ICP.get_param(GN_SYNC_REQUESTED_DATE_KEY) or "").strip()
        today_str = fields.Date.today().isoformat()
        if requested != today_str:
            return
        api_client = self.env["res.config.settings"].get_gruponucleo_api()
        if not api_client:
            _logger.debug("Grupo Núcleo sync skipped: API not configured.")
            return
        try:
            catalog = api_client.get_catalog()
            _logger.info("Grupo Núcleo GetCatalog received, running one batch.")
            # Sync sale taxes from API (21% or 10.5% per row) only at start of cycle to avoid heavy runs every 5 min.
            items = catalog if isinstance(catalog, list) else (catalog.get("items") if isinstance(catalog, dict) else [])
            total = len(items) if isinstance(items, list) else 0
            offset = int(ICP.get_param(GN_SYNC_OFFSET_KEY, "0") or "0")
            if total and (offset == 0 or offset >= total):
                self._gruponucleo_sync_sale_taxes_from_api(catalog)
        except GrupNucleoAPIError as e:
            _logger.warning(
                "Grupo Núcleo sync failed (API error): %s",
                e,
                exc_info=True,
            )
            return
        except Exception as e:
            _logger.error(
                "Grupo Núcleo sync failed (unexpected error): %s",
                e,
                exc_info=True,
            )
            return
        result = self._sync_gruponucleo_catalog_data(catalog)
        # When the last batch finishes, next_offset is 0. We cannot write this cron from inside
        # itself (Odoo locks the ir.cron row). So we set a "deactivate pending" flag; a separate
        # cron (_cron_gruponucleo_deactivate_catalog_if_pending) runs later and performs the
        # deactivation when this execution has finished.
        if result is not None and (result.get("next_offset") == 0 or result.get("next_offset") == "0"):
            ICP.set_param(GN_SYNC_REQUESTED_DATE_KEY, "")
            ICP.set_param(GN_SYNC_DEACTIVATE_PENDING_KEY, "1")
            _logger.info(
                "Grupo Núcleo sync: catalog complete (total=%s). Deactivation requested; cleanup cron will deactivate batch cron shortly.",
                result.get("total"),
            )
        return result

    def _cron_gruponucleo_deactivate_catalog_if_pending(self):
        """
        Called by a separate ir.cron. When a batch cron (catalog or price/stock) finishes the
        full sync it cannot write itself (row lock). It sets a deactivate-pending flag. This
        method runs every few minutes; for each flag set, it deactivates the corresponding
        batch cron and clears the flag. No lock conflict because this is a different cron.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        committed = False

        # Catalog batch cron deactivation
        pending_catalog = (ICP.get_param(GN_SYNC_DEACTIVATE_PENDING_KEY) or "").strip()
        if pending_catalog == "1":
            cron = None
            try:
                cron = self.env.ref(CRON_CATALOG_XML_ID, raise_if_not_found=False)
            except Exception:
                pass
            if not cron:
                cron = (
                    self.env["ir.cron"]
                    .sudo()
                    .search(
                        [
                            ("code", "=", "model._cron_sync_gruponucleo_catalog()"),
                            ("model_id.model", "=", "product.template"),
                        ],
                        limit=1,
                    )
                )
            if cron:
                cron.write({"active": False})
                _logger.debug(
                    "Grupo Núcleo sync: batch catalog cron id=%s deactivated (was pending).",
                    cron.id,
                )
            else:
                _logger.warning(
                    "Grupo Núcleo sync: catalog deactivate pending but cron not found (xml_id=%s).",
                    CRON_CATALOG_XML_ID,
                )
            ICP.set_param(GN_SYNC_DEACTIVATE_PENDING_KEY, "")
            committed = True

        # Price/stock batch cron deactivation
        pending_ps = (ICP.get_param(GN_PRICE_STOCK_DEACTIVATE_PENDING_KEY) or "").strip()
        if pending_ps == "1":
            cron_ps = None
            try:
                cron_ps = self.env.ref(CRON_PRICE_STOCK_XML_ID, raise_if_not_found=False)
            except Exception:
                pass
            if not cron_ps:
                cron_ps = (
                    self.env["ir.cron"]
                    .sudo()
                    .search(
                        [
                            ("code", "=", "model._cron_sync_gruponucleo_price_stock()"),
                            ("model_id.model", "=", "product.template"),
                        ],
                        limit=1,
                    )
                )
            if cron_ps:
                cron_ps.write({"active": False})
                _logger.debug(
                    "Grupo Núcleo price/stock sync: batch cron id=%s deactivated (was pending).",
                    cron_ps.id,
                )
            else:
                _logger.warning(
                    "Grupo Núcleo price/stock sync: deactivate pending but cron not found (xml_id=%s).",
                    CRON_PRICE_STOCK_XML_ID,
                )
            ICP.set_param(GN_PRICE_STOCK_DEACTIVATE_PENDING_KEY, "")
            committed = True

        if committed:
            self.env.cr.commit()

    def _action_request_price_stock_sync(self):
        """
        Called by the price/stock trigger cron (e.g. every 12h). Sets sync-requested date to today
        and activates the price/stock batch cron so it runs every few minutes until the full
        catalog is done (then cleanup cron deactivates it).
        """
        today_str = fields.Date.today().isoformat()
        self.env["ir.config_parameter"].sudo().set_param(
            GN_PRICE_STOCK_REQUESTED_DATE_KEY,
            today_str,
        )
        try:
            self.env.ref(CRON_PRICE_STOCK_XML_ID).sudo().write({"active": True})
            _logger.info(
                "Grupo Núcleo price/stock sync: trigger set requested_date=%s, batch cron activated.",
                today_str,
            )
        except Exception as e:
            _logger.warning(
                "Grupo Núcleo price/stock sync: could not activate batch cron: %s",
                e,
            )

    def _cron_sync_gruponucleo_price_stock(self):
        """
        Called by ir.cron every few minutes when price/stock sync is requested. Only updates
        existing GN products: price (supplierinfo), stock_gn, dimensions. No creates.
        Only runs if GN_PRICE_STOCK_REQUESTED_DATE_KEY is set to today. When the full catalog
        is done (next_offset == 0), clears the flag and sets deactivate pending; cleanup cron
        performs the actual deactivation (same lock issue as catalog cron).
        """
        ICP = self.env["ir.config_parameter"].sudo()
        requested = (ICP.get_param(GN_PRICE_STOCK_REQUESTED_DATE_KEY) or "").strip()
        today_str = fields.Date.today().isoformat()
        if requested != today_str:
            return
        api_client = self.env["res.config.settings"].get_gruponucleo_api()
        if not api_client:
            _logger.debug("Grupo Núcleo price/stock sync skipped: API not configured.")
            return
        try:
            catalog = api_client.get_catalog()
            _logger.info("Grupo Núcleo price/stock sync: GetCatalog received, running one batch.")
        except GrupNucleoAPIError as e:
            _logger.warning(
                "Grupo Núcleo price/stock sync failed (API error): %s",
                e,
                exc_info=True,
            )
            return
        except Exception as e:
            _logger.error(
                "Grupo Núcleo price/stock sync failed (unexpected error): %s",
                e,
                exc_info=True,
            )
            return
        result = self._sync_gruponucleo_price_stock_batch(catalog)
        if result is not None and (result.get("next_offset") == 0 or result.get("next_offset") == "0"):
            ICP.set_param(GN_PRICE_STOCK_REQUESTED_DATE_KEY, "")
            ICP.set_param(GN_PRICE_STOCK_DEACTIVATE_PENDING_KEY, "1")
            _logger.info(
                "Grupo Núcleo price/stock sync: complete (total=%s). Deactivation requested.",
                result.get("total"),
            )
        return result

    def _sync_gruponucleo_price_stock_batch(self, catalog):
        """
        Process one batch of catalog: only update existing products (by gn_item_id) with
        price (supplierinfo), stock_gn, volume, zippin dimensions, gn_last_sync. No creates.
        Uses its own offset (GN_PRICE_STOCK_OFFSET_KEY) so it does not interfere with full sync.
        """
        if not catalog:
            _logger.debug("Grupo Núcleo price/stock sync: empty catalog.")
            return None
        items = catalog if isinstance(catalog, list) else (catalog if isinstance(catalog, dict) else [])
        if isinstance(catalog, dict) and "items" in catalog:
            items = catalog["items"]
        if not isinstance(items, list):
            _logger.debug("Grupo Núcleo price/stock sync: catalog format not recognized.")
            return None
        total = len(items)
        ICP = self.env["ir.config_parameter"].sudo()
        offset = int(ICP.get_param(GN_PRICE_STOCK_OFFSET_KEY, "0") or "0")
        if offset >= total or offset < 0:
            offset = 0
        batch = items[offset : offset + GN_SYNC_BATCH_SIZE]
        batch_size = len(batch)
        if not batch:
            _logger.debug(
                "Grupo Núcleo price/stock sync: no items in batch (offset=%d, total=%d).",
                offset,
                total,
            )
            return None
        _logger.debug(
            "Grupo Núcleo price/stock sync: batch offset=%d to %d of %d (%d items).",
            offset,
            offset + batch_size,
            total,
            batch_size,
        )
        _logger.debug(
            "GN [PRICE_STOCK] batch start offset=%d batch_size=%d total=%d (first row keys: %s)",
            offset,
            batch_size,
            total,
            list(batch[0].keys()) if batch and isinstance(batch[0], dict) else [],
        )
        usd_currency = self.env.ref("base.USD", raise_if_not_found=False)
        stock_source = (ICP.get_param("grupo_nucleo_integration.gn_stock_source", "sum") or "sum").strip()
        if stock_source not in ("stock_mdp", "stock_caba", "sum"):
            stock_source = "sum"
        gn_partner_id = ICP.get_param("grupo_nucleo_integration.gn_partner_id", "").strip()
        try:
            gn_partner_id = int(gn_partner_id) if gn_partner_id else None
        except (TypeError, ValueError):
            gn_partner_id = None
        ProductTemplate = self.env["product.template"].with_context(active_test=False)
        stats = {"updated": 0, "skipped": 0, "errors": 0}
        products_to_update_cost = self.env["product.template"]

        for row in batch:
            if not isinstance(row, dict):
                stats["skipped"] += 1
                continue
            item_id = row.get("id") or row.get("item_id") or row.get("Id")
            if item_id is None:
                stats["skipped"] += 1
                continue
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                stats["skipped"] += 1
                continue
            product = ProductTemplate.search([("gn_item_id", "=", item_id)], limit=1)
            if not product:
                _logger.debug(
                    "GN [PRICE_STOCK] SKIP no product item_id=%s",
                    item_id,
                )
                stats["skipped"] += 1
                continue
            try:
                full_vals = self._gruponucleo_catalog_row_to_vals(
                    row,
                    item_id,
                    usd_currency=usd_currency,
                    stock_source=stock_source,
                    parent_public_categ_id=None,
                )
                price_gn = full_vals.pop("_price_gn", None)
                raw_precio = row.get("precioNeto_USD") or row.get("precio_neto") or row.get("precioNeto") or row.get("price") or row.get("precio")
                _logger.debug(
                    "GN [PRICE_STOCK] row item_id=%s codigo=%s | API precioNeto_USD/raw=%s → price_gn_supplierinfo=%s",
                    item_id,
                    full_vals.get("gn_product_code") or product.gn_product_code or "-",
                    raw_precio,
                    price_gn,
                )
                vals_light = {
                    k: v for k, v in full_vals.items()
                    if k in GN_PRICE_STOCK_ONLY_KEYS
                }
                if vals_light:
                    product.write(vals_light)
                if gn_partner_id and price_gn is not None:
                    self._gruponucleo_update_supplierinfo(
                        product,
                        gn_partner_id,
                        price_gn,
                        usd_currency.id if usd_currency else None,
                        product_code=full_vals.get("gn_product_code") or product.gn_product_code,
                    )
                    products_to_update_cost |= product
                    _logger.debug(
                        "GN [PRICE_STOCK] UPDATED item_id=%s product_id=%s default_code=%s price_gn=%s standard_price(before)=%s",
                        item_id,
                        product.id,
                        product.default_code or "-",
                        price_gn,
                        product.standard_price,
                    )
                else:
                    _logger.debug(
                        "GN [PRICE_STOCK] item_id=%s product_id=%s | NO supplierinfo: gn_partner_id=%s price_gn=%s",
                        item_id,
                        product.id,
                        gn_partner_id,
                        price_gn,
                    )
                stats["updated"] += 1
            except Exception as e:
                stats["errors"] += 1
                _logger.error(
                    "Grupo Núcleo price/stock sync: error item_id=%s: %s",
                    item_id,
                    e,
                    exc_info=True,
                )
                self.env.cr.rollback()
                continue

        if products_to_update_cost:
            _logger.debug(
                "GN [PRICE_STOCK] Calling _update_cost_from_replenishment_cost for %d products (ids=%s)",
                len(products_to_update_cost),
                products_to_update_cost.ids[:20] if len(products_to_update_cost) > 20 else products_to_update_cost.ids,
            )
            products_to_update_cost._update_cost_from_replenishment_cost()
            for p in products_to_update_cost[:5]:
                _logger.debug(
                    "GN [PRICE_STOCK] after cost update product_id=%s default_code=%s standard_price=%s",
                    p.id,
                    p.default_code or "-",
                    p.standard_price,
                )
            if len(products_to_update_cost) > 5:
                _logger.debug(
                    "GN [PRICE_STOCK] ... and %d more products updated",
                    len(products_to_update_cost) - 5,
                )

        new_offset = offset + batch_size
        if new_offset >= total:
            new_offset = 0
            _logger.debug(
                "Grupo Núcleo price/stock sync batch complete. updated=%d skipped=%d errors=%d.",
                stats["updated"],
                stats["skipped"],
                stats["errors"],
            )
        else:
            _logger.debug(
                "Grupo Núcleo price/stock sync batch done. updated=%d skipped=%d errors=%d. Next offset=%d/%d.",
                stats["updated"],
                stats["skipped"],
                stats["errors"],
                new_offset,
                total,
            )
        ICP.set_param(GN_PRICE_STOCK_OFFSET_KEY, str(new_offset))
        self.env.cr.commit()
        return {
            "stats": stats,
            "offset": offset,
            "batch_size": batch_size,
            "total": total,
            "next_offset": new_offset,
        }

    def _sync_gruponucleo_catalog_data(self, catalog):
        """
        Process one batch of catalog: read offset from config, process items[offset:offset+BATCH],
        save new offset (or 0 if batch completed). Commit after batch to allow resume on next run.
        """
        if not catalog:
            _logger.debug("Grupo Núcleo sync: empty catalog, nothing to do.")
            return None
        items = catalog if isinstance(catalog, list) else (catalog if isinstance(catalog, dict) else [])
        if isinstance(catalog, dict) and "items" in catalog:
            items = catalog["items"]
        if not isinstance(items, list):
            _logger.debug("Grupo Núcleo sync: catalog format not recognized, skipping.")
            return None
        total = len(items)
        ICP = self.env["ir.config_parameter"].sudo()
        offset = int(ICP.get_param(GN_SYNC_OFFSET_KEY, "0") or "0")
        # If catalog grew or offset is past end, start from 0
        if offset >= total or offset < 0:
            offset = 0
        batch = items[offset : offset + GN_SYNC_BATCH_SIZE]
        batch_size = len(batch)
        if not batch:
            _logger.debug("Grupo Núcleo sync: no items in batch (offset=%d, total=%d).", offset, total)
            return None
        _logger.debug(
            "Grupo Núcleo sync: batch offset=%d to %d of %d (%d items).",
            offset,
            offset + batch_size,
            total,
            batch_size,
        )
        _logger.debug(
            "GN [CATALOG] batch start offset=%d batch_size=%d total=%d (first row keys: %s)",
            offset,
            batch_size,
            total,
            list(batch[0].keys()) if batch and isinstance(batch[0], dict) else [],
        )
        usd_currency = self.env.ref("base.USD", raise_if_not_found=False)
        stock_source = (ICP.get_param("grupo_nucleo_integration.gn_stock_source", "sum") or "sum").strip()
        if stock_source not in ("stock_mdp", "stock_caba", "sum"):
            stock_source = "sum"
        gn_partner_id = ICP.get_param("grupo_nucleo_integration.gn_partner_id", "").strip()
        try:
            gn_partner_id = int(gn_partner_id) if gn_partner_id else None
        except (TypeError, ValueError):
            gn_partner_id = None
        gn_public_categ_parent_id = ICP.get_param("grupo_nucleo_integration.gn_public_categ_parent_id", "").strip()
        try:
            gn_public_categ_parent_id = int(gn_public_categ_parent_id) if gn_public_categ_parent_id else None
        except (TypeError, ValueError):
            gn_public_categ_parent_id = None
        ProductTemplate = self.env["product.template"].with_context(active_test=False)
        stats = {"updated": 0, "created": 0, "skipped": 0, "barcode_fallback": 0, "errors": 0}

        for row in batch:
            if not isinstance(row, dict):
                stats["skipped"] += 1
                continue
            item_id = row.get("id") or row.get("item_id") or row.get("Id")
            if item_id is None:
                stats["skipped"] += 1
                continue
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                stats["skipped"] += 1
                continue

            try:
                vals = self._gruponucleo_catalog_row_to_vals(
                    row,
                    item_id,
                    usd_currency=usd_currency,
                    stock_source=stock_source,
                    parent_public_categ_id=gn_public_categ_parent_id,
                )
                vals["gn_item_id"] = item_id
                price_gn = vals.pop("_price_gn", None)
                product = self._find_product_for_gruponucleo_row(ProductTemplate, row, item_id)
                if product:
                    product.write(vals)
                    if gn_partner_id and price_gn is not None:
                        self._gruponucleo_update_supplierinfo(
                            product, gn_partner_id, price_gn,
                            usd_currency.id if usd_currency else None,
                            product_code=vals.get("gn_product_code"),
                        )
                    else:
                        _logger.debug(
                            "GN [CATALOG] item_id=%s product_id=%s default_code=%s | NO supplierinfo: gn_partner_id=%s price_gn=%s",
                            item_id,
                            product.id,
                            product.default_code or "-",
                            gn_partner_id,
                            price_gn,
                        )
                    stats["updated"] += 1
                    _logger.debug(
                        "GN [CATALOG] UPDATED item_id=%s product_id=%s default_code=%s price_gn=%s supplierinfo=%s standard_price(before)=%s",
                        item_id,
                        product.id,
                        product.default_code or "-",
                        price_gn,
                        "yes" if gn_partner_id and price_gn is not None else "no",
                        product.standard_price,
                    )
                    _logger.debug(
                        "Grupo Núcleo sync: updated gn_item_id=%s [%s]",
                        item_id,
                        product.gn_product_code or product.name or "-",
                    )
                else:
                    try:
                        product = ProductTemplate.create(vals)
                        if gn_partner_id and price_gn is not None:
                            self._gruponucleo_update_supplierinfo(
                                product, gn_partner_id, price_gn,
                                usd_currency.id if usd_currency else None,
                                product_code=vals.get("gn_product_code"),
                            )
                        stats["created"] += 1
                        _logger.debug(
                            "GN [CATALOG] CREATED item_id=%s product_id=%s default_code=%s price_gn=%s supplierinfo=%s",
                            item_id,
                            product.id,
                            product.default_code or "-",
                            price_gn,
                            "yes" if gn_partner_id and price_gn is not None else "no",
                        )
                        _logger.debug(
                            "Grupo Núcleo sync: created gn_item_id=%s",
                            item_id,
                        )
                    except ValidationError as e:
                        if "barcode" in str(e).lower() or "códigos de barras" in str(e).lower():
                            product = self._find_product_by_barcode(
                                ProductTemplate, self._gruponucleo_row_barcode(row)
                            )
                            if product:
                                code_str = (
                                    str(
                                        row.get("codigo")
                                        or row.get("code")
                                        or row.get("sku")
                                        or row.get("default_code")
                                        or item_id
                                    ).strip()
                                )
                                default_code = (product.default_code or "").strip()
                                skip = (
                                    product.gn_item_id and product.gn_item_id != item_id
                                ) or (
                                    default_code
                                    and code_str
                                    and default_code != code_str
                                )
                                if skip:
                                    _logger.warning(
                                        "Grupo Núcleo sync: barcode conflict but skip update item_id=%s product id=%s default_code=%s codigo=%s (avoid overwrite).",
                                        item_id,
                                        product.id,
                                        default_code or "-",
                                        code_str or "-",
                                    )
                                    stats["skipped"] += 1
                                else:
                                    product.write(vals)
                                    if gn_partner_id and price_gn is not None:
                                        self._gruponucleo_update_supplierinfo(
                                            product, gn_partner_id, price_gn,
                                            usd_currency.id if usd_currency else None,
                                            product_code=vals.get("gn_product_code"),
                                        )
                                    stats["barcode_fallback"] += 1
                                    _logger.debug(
                                        "GN [CATALOG] BARCODE_FALLBACK item_id=%s product_id=%s default_code=%s price_gn=%s supplierinfo=%s",
                                        item_id,
                                        product.id,
                                        product.default_code or "-",
                                        price_gn,
                                        "yes" if gn_partner_id and price_gn is not None else "no",
                                    )
                                    _logger.debug(
                                        "Grupo Núcleo sync: barcode conflict gn_item_id=%s -> product id=%s",
                                        item_id,
                                        product.id,
                                    )
                            else:
                                raise
                        else:
                            raise
            except Exception as e:
                stats["errors"] += 1
                _logger.error(
                    "Grupo Núcleo sync: error processing item_id=%s: %s",
                    item_id,
                    e,
                    exc_info=True,
                )
                self.env.cr.rollback()
                continue

        new_offset = offset + batch_size
        if new_offset >= total:
            new_offset = 0
            _logger.debug(
                "Grupo Núcleo sync batch complete (sync finished). updated=%d created=%d barcode_fallback=%d skipped=%d errors=%d.",
                stats["updated"],
                stats["created"],
                stats["barcode_fallback"],
                stats["skipped"],
                stats["errors"],
            )
        else:
            _logger.debug(
                "Grupo Núcleo sync batch done. updated=%d created=%d barcode_fallback=%d skipped=%d errors=%d. Next offset=%d/%d.",
                stats["updated"],
                stats["created"],
                stats["barcode_fallback"],
                stats["skipped"],
                stats["errors"],
                new_offset,
                total,
            )
        ICP.set_param(GN_SYNC_OFFSET_KEY, str(new_offset))
        ICP.set_param(GN_SYNC_LAST_TOTAL_KEY, str(total))
        self.env.cr.commit()
        return {
            "stats": stats,
            "offset": offset,
            "batch_size": batch_size,
            "total": total,
            "next_offset": new_offset,
        }

    def _gruponucleo_row_barcode(self, row):
        """Return normalized barcode from catalog row, or None."""
        ean = row.get("ean")
        if ean is None:
            return None
        s = str(ean).strip()
        if not s or s == "0":
            return None
        return s

    def _gruponucleo_row_stock_float(self, row, *keys):
        """Return stock value as float from catalog row (first matching key). Returns 0.0 if missing/invalid."""
        for key in keys:
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _gruponucleo_row_dimension(self, row, *keys):
        """Return dimension in cm as float from catalog row (first matching key). Returns 0.0 if missing/invalid."""
        for key in keys:
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _find_product_for_gruponucleo_row(self, product_model, row, item_id):
        """
        Find existing product.template to update: only by gn_item_id or by barcode (with safeguard).

        We do NOT match by gn_product_code/codigo to avoid overwriting a local product that
        happens to share the same internal reference as a different GN article (e.g. JCK camera
        ref 8310 overwritten by GN Brother tape codigo 8310). Matching by code was removed.

        - gn_item_id: product already linked to this GN item → safe to update.
        - barcode: only consider a match when the product's default_code matches the API code,
          to avoid rare coincidences (same barcode, different article). If the product already
          has a different gn_item_id, we do not use it.
        """
        product = product_model.search([("gn_item_id", "=", item_id)], limit=1)
        if product:
            return product
        code = (
            row.get("codigo")
            or row.get("code")
            or row.get("sku")
            or row.get("default_code")
            or str(item_id)
        )
        code_str = str(code).strip() if code is not None else ""
        barcode = self._gruponucleo_row_barcode(row)
        if barcode:
            product = self._find_product_by_barcode(product_model, barcode)
            if product:
                # Already linked to another GN item: do not reassign (avoid overwriting).
                if product.gn_item_id and product.gn_item_id != item_id:
                    return product_model
                # Safeguard: when default_code is set, it must match API code to avoid rare
                # coincidences (same barcode, different product; or wrong link).
                default_code = (product.default_code or "").strip()
                if default_code and code_str and default_code != code_str:
                    _logger.debug(
                        "Grupo Núcleo sync: skip barcode match item_id=%s codigo=%s product default_code=%s (avoid overwrite).",
                        item_id,
                        code_str,
                        default_code,
                    )
                    return product_model
                return product
        return product_model

    def _find_product_by_barcode(self, product_model, barcode):
        """Find product.template or product.product by barcode. Returns product.template."""
        if not barcode:
            return product_model
        product = product_model.search([("barcode", "=", barcode)], limit=1)
        if product:
            return product
        product_product = self.env["product.product"].with_context(active_test=False).search(
            [("barcode", "=", barcode)], limit=1
        )
        if product_product:
            return product_product.product_tmpl_id
        return product_model

    def _gruponucleo_update_supplierinfo(
        self, product, partner_id, price, currency_id=None, product_code=None
    ):
        """
        Create or update product.supplierinfo for the GN partner so purchase and
        replenishment cost can use vendor price (e.g. cheapest or most updated).
        """
        if not product or not partner_id or price is None:
            _logger.debug(
                "GN [supplierinfo] skip: product=%s partner_id=%s price=%s",
                product.id if product else None,
                partner_id,
                price,
            )
            return
        Supplierinfo = self.env["product.supplierinfo"].sudo()
        domain = [
            ("product_tmpl_id", "=", product.id),
            ("partner_id", "=", partner_id),
        ]
        line = Supplierinfo.search(domain, limit=1)
        vals = {
            "price": price,
            "min_qty": 1.0,
        }
        if currency_id:
            vals["currency_id"] = currency_id
        if product_code is not None:
            vals["product_code"] = product_code
        if line:
            line.write(vals)
            _logger.debug(
                "GN [supplierinfo] UPDATED product_id=%s default_code=%s partner_id=%s price=%s currency_id=%s product_code=%s (supplierinfo_id=%s)",
                product.id,
                product.default_code or "-",
                partner_id,
                price,
                currency_id,
                product_code,
                line.id,
            )
        else:
            vals["product_tmpl_id"] = product.id
            vals["partner_id"] = partner_id
            Supplierinfo.create(vals)
            _logger.debug(
                "GN [supplierinfo] CREATED product_id=%s default_code=%s partner_id=%s price=%s currency_id=%s product_code=%s",
                product.id,
                product.default_code or "-",
                partner_id,
                price,
                currency_id,
                product_code,
            )

    def _gruponucleo_get_or_create_public_categ(self, name, parent_id=False):
        """Create or return product.public.category for ecommerce (like ELIT)."""
        if not name or not name.strip():
            return self.env["product.public.category"]
        name = name.strip()
        domain = [("name", "=", name)]
        if parent_id:
            domain.append(("parent_id", "=", parent_id))
        else:
            domain.append(("parent_id", "=", False))
        categ = self.env["product.public.category"].search(domain, limit=1)
        if not categ:
            categ = self.env["product.public.category"].create({
                "name": name,
                "parent_id": parent_id,
            })
            _logger.debug("Grupo Núcleo sync: created ecommerce category %s", name)
        return categ

    def _gruponucleo_internal_categ_from_row(self, row):
        """
        Return product.category id from row categoria/subcategoria (create if missing).
        API: categoria, subcategoria (e.g. 'Informatica Accesorios', 'Cables y Adaptadores').
        """
        categoria = (row.get("categoria") or row.get("Categoria") or "").strip()
        subcategoria = (row.get("subcategoria") or row.get("Subcategoria") or "").strip()
        if not categoria:
            return None
        ProductCategory = self.env["product.category"]
        parent = ProductCategory.search([("name", "=", categoria)], limit=1)
        if not parent:
            parent = ProductCategory.create({"name": categoria})
        if not subcategoria:
            return parent.id
        categ = ProductCategory.search([
            ("name", "=", subcategoria),
            ("parent_id", "=", parent.id),
        ], limit=1)
        if not categ:
            categ = ProductCategory.create({
                "name": subcategoria,
                "parent_id": parent.id,
            })
        return categ.id

    def _gruponucleo_public_categ_ids_from_row(self, row, parent_public_categ_id):
        """
        Return list of product.public.category ids for ecommerce from row (categoria, subcategoria)
        under the configured parent. Used to suggest ecommerce categories for publishing.
        """
        if not parent_public_categ_id:
            return []
        categoria = (row.get("categoria") or row.get("Categoria") or "").strip()
        subcategoria = (row.get("subcategoria") or row.get("Subcategoria") or "").strip()
        if not categoria and not subcategoria:
            return []
        root = self.env["product.public.category"].browse(parent_public_categ_id)
        if not root.exists():
            _logger.warning(
                "Grupo Núcleo sync: categoría padre ecommerce (id=%s) no existe; no se asignan categorías públicas.",
                parent_public_categ_id,
            )
            return []
        if categoria:
            main = self._gruponucleo_get_or_create_public_categ(categoria, root.id)
        else:
            main = root
        ids = [main.id]
        if subcategoria:
            sub = self._gruponucleo_get_or_create_public_categ(subcategoria, main.id)
            ids.append(sub.id)
        return ids

    def _gruponucleo_iva_pct_from_row(self, row):
        """
        Extract IVA (VAT) percentage from API row for sale taxes.

        The API returns "impuestos" as a list, e.g. [{"imp_desc": "IVA 21%", "imp_porcentaje": 21},
        {"imp_desc": "Imp. Interno 10.5%", "imp_porcentaje": 10.5}]. We need the IVA entry only
        (customer/sale tax), not the internal tax. Returns the first IVA percentage found (21.0 or
        10.5) or None if none found.
        """
        if not row.get("impuestos") or not isinstance(row["impuestos"], list):
            return None
        for item in row["impuestos"]:
            if not isinstance(item, dict) or item.get("imp_porcentaje") is None:
                continue
            desc = (item.get("imp_desc") or item.get("desc") or "").lower()
            if "interno" in desc or "internal" in desc:
                continue
            if "iva" in desc or "vat" in desc:
                try:
                    return float(item["imp_porcentaje"])
                except (TypeError, ValueError):
                    pass
        return None

    def _gruponucleo_sale_tax_ids_for_iva_pct(self, iva_pct):
        """
        Return account.tax ids for sale (customer) taxes matching the given IVA percentage.

        Used to set product.template.taxes_id from Grupo Núcleo API data (21% or 10.5%).
        Searches by type_tax_use='sale' and amount equal to iva_pct in current company.
        Returns empty list if no matching tax is found (then product keeps default taxes).
        """
        if iva_pct is None:
            return []
        Tax = self.env["account.tax"].sudo()
        amount = round(float(iva_pct), 2)
        taxes = Tax.search([
            ("type_tax_use", "=", "sale"),
            ("amount", "=", amount),
            "|",
            ("company_id", "=", self.env.company.id),
            ("company_id", "=", False),
        ], limit=1)
        if not taxes:
            _logger.debug(
                "Grupo Núcleo sync: no account.tax found for sale IVA %.2f%% (company=%s); product will keep default taxes.",
                amount,
                self.env.company.id,
            )
            return []
        return taxes.ids

    def _gruponucleo_purchase_tax_ids_for_iva_pct(self, iva_pct):
        """
        Return account.tax ids for purchase (supplier) taxes matching the given IVA percentage.

        Used to set product.template.supplier_taxes_id so purchase taxes match sale (21% or 10.5%).
        Searches by type_tax_use='purchase' and amount equal to iva_pct in current company.
        Returns empty list if no matching tax is found.
        """
        if iva_pct is None:
            return []
        Tax = self.env["account.tax"].sudo()
        amount = round(float(iva_pct), 2)
        taxes = Tax.search([
            ("type_tax_use", "=", "purchase"),
            ("amount", "=", amount),
            "|",
            ("company_id", "=", self.env.company.id),
            ("company_id", "=", False),
        ], limit=1)
        if not taxes:
            _logger.debug(
                "Grupo Núcleo sync: no account.tax found for purchase IVA %.2f%% (company=%s); product will keep default supplier taxes.",
                amount,
                self.env.company.id,
            )
            return []
        return taxes.ids

    def _gruponucleo_sync_sale_taxes_from_api(self, catalog=None):
        """
        Sync sale and purchase taxes (IVA) for all Grupo Núcleo products from the API catalog.

        For each catalog item that exists in Odoo (by gn_item_id), sets
        product.template.taxes_id and supplier_taxes_id according to the IVA percentage
        returned by the API (21%% or 10.5%%). Purchase taxes are kept equal to sale taxes.
        Does not assume a single rate; each product gets the rate from its catalog row.
        Safe to run from server action or cron.

        :param catalog: optional pre-fetched catalog (dict or list); if None, fetches
            via get_gruponucleo_api().get_catalog().
        :return: dict with updated, processed, reason.
        """
        if catalog is None:
            api_client = self.env["res.config.settings"].get_gruponucleo_api()
            if not api_client:
                _logger.debug("Grupo Núcleo sync taxes: API not configured.")
                return {"updated": 0, "processed": 0, "reason": "no_api"}
            try:
                catalog = api_client.get_catalog()
            except GrupNucleoAPIError as e:
                _logger.warning("Grupo Núcleo sync taxes: API error %s", e)
                return {"updated": 0, "processed": 0, "reason": "api_error"}
        items = catalog if isinstance(catalog, list) else (catalog if isinstance(catalog, dict) else [])
        if isinstance(catalog, dict) and "items" in catalog:
            items = catalog["items"]
        if not isinstance(items, list) or not items:
            _logger.debug("Grupo Núcleo sync taxes: empty or invalid catalog.")
            return {"updated": 0, "processed": 0, "reason": "empty_catalog"}
        ProductTemplate = self.env["product.template"].with_context(active_test=False)
        updated = 0
        for idx, row in enumerate(items):
            if not isinstance(row, dict):
                continue
            item_id = row.get("id") or row.get("item_id") or row.get("Id")
            if item_id is None:
                continue
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                continue
            product = ProductTemplate.search([("gn_item_id", "=", item_id)], limit=1)
            if not product:
                continue
            iva_pct = self._gruponucleo_iva_pct_from_row(row)
            if iva_pct is None:
                continue
            sale_tax_ids = self._gruponucleo_sale_tax_ids_for_iva_pct(iva_pct)
            purchase_tax_ids = self._gruponucleo_purchase_tax_ids_for_iva_pct(iva_pct)
            write_vals = {}
            if sale_tax_ids:
                write_vals["taxes_id"] = [(6, 0, sale_tax_ids)]
            if purchase_tax_ids:
                write_vals["supplier_taxes_id"] = [(6, 0, purchase_tax_ids)]
            if not write_vals:
                continue
            product.write(write_vals)
            updated += 1
            if (idx + 1) % GN_SYNC_BATCH_SIZE == 0:
                self.env.cr.commit()
        if updated:
            _logger.debug(
                "Grupo Núcleo sync taxes: updated %d product(s) sale+purchase from API (processed %d items).",
                updated,
                len(items),
            )
        return {"updated": updated, "processed": len(items), "reason": "ok"}

    def _gruponucleo_catalog_row_to_vals(
        self, row, item_id, usd_currency=None, stock_source="sum", parent_public_categ_id=None
    ):
        """
        Map one catalog row to product.template write/create vals.
        API format: item_id, codigo, ean, partNumber, item_desc_0, item_desc_1, item_desc_2,
        marca, categoria, subcategoria, peso_gr, precioNeto_USD, impuestos,
        stock_mdp, stock_caba, url_imagenes ([{"url": "..."}]).
        Does not set list_price (sale price). Cost from API is stored only in product.supplierinfo;
        another module can use the latest supplierinfo for replenishment/cost.
        stock_gn is computed from row according to stock_source (stock_mdp, stock_caba, or sum).
        If API brings categoria/subcategoria, sets internal categ_id and optional public_categ_ids
        under parent_public_categ_id (ecommerce).
        """
        # Name: API uses item_desc_0 (short), item_desc_1 (medium), item_desc_2 (long)
        name = (
            row.get("item_desc_0")
            or row.get("item_desc_1")
            or row.get("name")
            or row.get("nombre")
            or row.get("description")
            or row.get("descripcion")
            or f"GN-{item_id}"
        )
        code = (
            row.get("codigo")
            or row.get("code")
            or row.get("sku")
            or row.get("default_code")
            or str(item_id)
        )
        # Price from API: used only for product.supplierinfo (no replenishment_base_cost here)
        price_gn = None
        for key in ("precioNeto_USD", "precio_neto", "precioNeto", "price", "precio", "list_price"):
            if key in row and row[key] is not None:
                try:
                    price_gn = float(row[key])
                    break
                except (TypeError, ValueError):
                    pass

        # For supplier cost we only apply IMPUESTO INTERNO (e.g. 10.5%), not IVA (21%).
        # API may return "impuestos" as list [{"imp_desc": "IVA 21%", "imp_porcentaje": 21}, {"imp_desc": "Imp. Interno 10.5%", "imp_porcentaje": 10.5}].
        # We must not sum both; cost = base * (1 + interno%), e.g. 80.79 * 1.105 = 89.27 USD.
        impuesto_interno_pct = 0.0
        for key in ("impuesto_interno", "impuestos_internos", "internal_tax"):
            if key in row and row[key] is not None:
                try:
                    impuesto_interno_pct = float(row[key])
                    break
                except (TypeError, ValueError):
                    pass
        if impuesto_interno_pct == 0.0 and row.get("impuestos") and isinstance(row["impuestos"], list):
            for item in row["impuestos"]:
                if not isinstance(item, dict) or item.get("imp_porcentaje") is None:
                    continue
                desc = (item.get("imp_desc") or item.get("desc") or "").lower()
                if "interno" in desc or "internal" in desc:
                    try:
                        impuesto_interno_pct += float(item["imp_porcentaje"])
                    except (TypeError, ValueError):
                        pass
        impuesto_keys_checked = ("impuesto_interno", "impuestos_internos", "impuestos", "internal_tax")

        # One-time detailed log for first article (impuesto interno debug)
        global _gn_logged_first_row
        if not _gn_logged_first_row:
            _gn_logged_first_row = True
            _logger.debug(
                "Grupo Núcleo API - [1 artículo] item_id=%s - Todas las claves del row: %s",
                item_id,
                sorted(row.keys()),
            )
            _logger.debug(
                "Grupo Núcleo API - [1 artículo] item_id=%s - Row completo (raw): %s",
                item_id,
                row,
            )
            tax_like = {
                k: row[k]
                for k in row
                if any(
                    x in k.lower()
                    for x in ("impuesto", "tax", "iva", "interno")
                )
            }
            _logger.debug(
                "Grupo Núcleo API - [1 artículo] item_id=%s - Claves tipo impuesto/tax/iva/interno: %s",
                item_id,
                tax_like,
            )
            values_checked = {k: row.get(k) for k in impuesto_keys_checked}
            _logger.debug(
                "Grupo Núcleo API - [1 artículo] item_id=%s - Valores leídos (keys que usamos): %s → impuesto_interno_pct=%.4f",
                item_id,
                values_checked,
                impuesto_interno_pct,
            )
            _logger.debug(
                "Grupo Núcleo API - [1 artículo] item_id=%s - precioNeto/precio keys: precio_gn=%s",
                item_id,
                price_gn,
            )
            if price_gn is not None and impuesto_interno_pct > 0:
                _logger.debug(
                    "Grupo Núcleo API - [1 artículo] item_id=%s - Aplicando solo impuesto interno: %.2f%% → price %s → %s",
                    item_id,
                    impuesto_interno_pct,
                    price_gn,
                    price_gn * (1 + impuesto_interno_pct / 100),
                )
            elif price_gn is not None and impuesto_interno_pct == 0:
                _logger.debug(
                    "Grupo Núcleo API - [1 artículo] item_id=%s - Sin impuesto interno (impuesto_interno_pct=0), precio_gn=%s",
                    item_id,
                    price_gn,
                )

        if price_gn is not None and impuesto_interno_pct > 0:
            price_gn_with_tax = price_gn * (1 + impuesto_interno_pct / 100)
            _logger.debug(
                "Grupo Núcleo sync: solo impuesto interno %.2f%% on item_id=%s, price %s → %s",
                impuesto_interno_pct,
                item_id,
                price_gn,
                price_gn_with_tax,
            )
            price_gn = price_gn_with_tax
        # Debug cost: log for every row the price data used for supplierinfo (API doc: https://apimanual.gruponucleo.com.ar/apign/catalogo-con-precio-y-stock)
        raw_precio = row.get("precioNeto_USD") or row.get("precio_neto") or row.get("precioNeto") or row.get("price") or row.get("precio") or row.get("list_price")
        _logger.debug(
            "GN [row→costo] item_id=%s codigo=%s | API precioNeto_USD/raw=%s impuesto_interno_pct=%.2f → price_gn_supplierinfo=%s",
            item_id,
            code,
            raw_precio,
            impuesto_interno_pct,
            price_gn,
        )
        vals = {
            "name": name,
            "gn_product_code": code,
            "gn_last_sync": fields.Datetime.now(),
            "replenishment_cost_type": "supplier_price",
        }
        # Descripción de ventas (item_desc_0); Descripción eCommerce (item_desc_1 + item_desc_2)
        desc_0 = (row.get("item_desc_0") or "").strip()
        desc_1 = (row.get("item_desc_1") or "").strip()
        desc_2 = (row.get("item_desc_2") or "").strip()
        if desc_0:
            vals["description_sale"] = desc_0
        desc_ecom_parts = [d for d in (desc_1, desc_2) if d]
        if desc_ecom_parts:
            vals["website_description"] = "\n".join(desc_ecom_parts)
        if price_gn is not None:
            vals["_price_gn"] = price_gn  # used only for supplierinfo; popped before write/create
        # Stock: read stock_mdp / stock_caba (with key variants), compute stock_gn from setting
        stock_mdp = self._gruponucleo_row_stock_float(row, "stock_mdp", "stock_Mdp", "StockMdp")
        stock_caba = self._gruponucleo_row_stock_float(row, "stock_caba", "stock_Caba", "StockCaba")
        if stock_source == "stock_mdp":
            stock_gn = stock_mdp
        elif stock_source == "stock_caba":
            stock_gn = stock_caba
        else:
            stock_gn = stock_mdp + stock_caba
        vals["stock_gn"] = stock_gn
        # Internal category from API (categoria, subcategoria)
        internal_categ_id = self._gruponucleo_internal_categ_from_row(row)
        if internal_categ_id:
            vals["categ_id"] = internal_categ_id
        # Ecommerce public categories under configured parent (for publishing)
        if parent_public_categ_id:
            public_ids = self._gruponucleo_public_categ_ids_from_row(row, parent_public_categ_id)
            if public_ids:
                vals["public_categ_ids"] = [(6, 0, public_ids)]
        # Odoo 17: detailed_type for product type
        if row.get("type") == "service":
            vals["detailed_type"] = "service"
        else:
            vals["detailed_type"] = "product"
        # Barcode from API (ean)
        ean = row.get("ean")
        if ean is not None and str(ean).strip() and str(ean) != "0":
            vals["barcode"] = str(ean).strip()
        # Weight: API returns peso_gr (grams)
        if row.get("peso_gr") is not None:
            try:
                vals["weight"] = float(row["peso_gr"]) / 1000.0
            except (TypeError, ValueError):
                pass
        # Dimensions (API: alto_cm, ancho_cm, largo_cm) → volume (m³) and Zippin fields for shipping
        largo_cm = self._gruponucleo_row_dimension(row, "largo_cm", "largo")
        ancho_cm = self._gruponucleo_row_dimension(row, "ancho_cm", "ancho")
        alto_cm = self._gruponucleo_row_dimension(row, "alto_cm", "alto")
        if largo_cm > 0 and ancho_cm > 0 and alto_cm > 0:
            vals["volume"] = (largo_cm * ancho_cm * alto_cm) / 1_000_000.0
        if largo_cm > 0:
            vals["zippin_product_length"] = largo_cm
        if ancho_cm > 0:
            vals["zippin_product_width"] = ancho_cm
        if alto_cm > 0:
            vals["zippin_product_height"] = alto_cm
        # Image: API may return url_imagenes as list of {"url": "..."} or {"Url": "..."}
        image_url = None
        url_imagenes = row.get("url_imagenes") or row.get("urlImagenes") or []
        if url_imagenes and isinstance(url_imagenes, list):
            first = url_imagenes[0]
            if isinstance(first, dict):
                image_url = first.get("url") or first.get("Url") or first.get("URL")
            elif isinstance(first, str):
                image_url = first
        if not image_url:
            image_url = (
                row.get("image_url")
                or row.get("imageUrl")
                or row.get("imagen")
                or row.get("link_imagen")
            )
        if image_url and isinstance(image_url, str):
            b64 = self._gruponucleo_fetch_image_b64(image_url)
            if b64:
                vals["image_1920"] = b64
            else:
                _logger.debug(
                    "Grupo Núcleo sync: image fetch failed for item_id=%s url=%s",
                    item_id,
                    image_url[:80] if image_url else "",
                )
        elif item_id and not image_url:
            _logger.debug(
                "Grupo Núcleo sync: no image URL in catalog row for item_id=%s (keys: %s)",
                item_id,
                list(row.keys())[:15],
            )
        # Venta sin stock + ruta MTO: según ajuste en Grupo Núcleo
        allow_no_stock = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("grupo_nucleo_integration.gn_allow_out_of_stock_order", "False")
            .lower()
            in ("1", "true", "yes")
        )
        vals["allow_out_of_stock_order"] = allow_no_stock
        if allow_no_stock:
            vals["route_ids"] = [(4, GN_MTO_ROUTE_ID)]
        else:
            vals["route_ids"] = [(3, GN_MTO_ROUTE_ID)]
        # Sale and purchase taxes (IVA): from API impuestos take the IVA entry (21% or 10.5%), not internal tax.
        # Keep supplier_taxes_id equal to taxes_id so purchase orders use the same rate.
        iva_pct = self._gruponucleo_iva_pct_from_row(row)
        if iva_pct is not None:
            sale_tax_ids = self._gruponucleo_sale_tax_ids_for_iva_pct(iva_pct)
            if sale_tax_ids:
                vals["taxes_id"] = [(6, 0, sale_tax_ids)]
            purchase_tax_ids = self._gruponucleo_purchase_tax_ids_for_iva_pct(iva_pct)
            if purchase_tax_ids:
                vals["supplier_taxes_id"] = [(6, 0, purchase_tax_ids)]
        return vals

    def _gruponucleo_fetch_image_b64(self, url):
        """Fetch image from URL and return base64 string for image_1920. Return False on failure."""
        import base64
        import urllib.request
        # Use a browser-like User-Agent to avoid blocks (e.g. Cloudflare on gruponucleo.com.ar)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Odoo/17; GrupoNucleo-Integration/1.0)"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return base64.b64encode(resp.read()).decode("ascii")
        except Exception as e:
            _logger.debug("Grupo Núcleo sync: image fetch error url=%s error=%s", url[:80], e)
            return False

    def action_sync_gruponucleo_catalog(self):
        """Manual action to sync catalog (from settings or product view)."""
        api_client = self.env["res.config.settings"].get_gruponucleo_api()
        if not api_client:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Error"),
                    "message": _("Configure Grupo Núcleo API credentials in Settings."),
                    "type": "danger",
                    "sticky": True,
                },
            }
        try:
            _logger.info("Grupo Núcleo sync: manual trigger, fetching catalog.")
            catalog = api_client.get_catalog()
            result = self._sync_gruponucleo_catalog_data(catalog)
        except GrupNucleoAPIError as e:
            _logger.warning("Grupo Núcleo sync failed: %s", e)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Sync failed"),
                    "message": str(e),
                    "type": "danger",
                    "sticky": True,
                },
            }
        if result and isinstance(result, dict):
            s = result["stats"]
            total = result["total"]
            next_off = result["next_offset"]
            processed = s["updated"] + s["created"] + s["barcode_fallback"] + s["skipped"]
            if next_off == 0 and result.get("batch_size", 0) > 0:
                msg = _(
                    "Batch done: %d processed (updated=%d created=%d). Sync complete (%d total)."
                ) % (processed, s["updated"], s["created"], total)
            elif next_off > 0:
                msg = _(
                    "Batch done: %d processed (updated=%d created=%d). Next offset %d/%d. Run again or wait for cron to continue."
                ) % (processed, s["updated"], s["created"], next_off, total)
            else:
                msg = _("Batch done: %d processed.") % processed
        else:
            msg = _("No catalog data to process (empty or already at end).")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Grupo Núcleo sync"),
                "message": msg,
                "type": "success",
                "sticky": True,
            },
        }

    # ------------------------------------------------------------------
    # API health check
    # ------------------------------------------------------------------

    @api.model
    def _cron_check_gruponucleo_api_health(self):
        """Lightweight login check to verify the GN API is alive.

        Updates ICP status flags and sends a Discuss notification to the
        configured user when the status transitions to error.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        previous_status = ICP.get_param("grupo_nucleo_integration.api_status", "unknown")
        now_str = fields.Datetime.to_string(fields.Datetime.now())

        api_client = self.env["res.config.settings"].get_gruponucleo_api()
        if not api_client:
            self._gn_health_set_error(
                ICP, now_str,
                "Credenciales Grupo Núcleo no configuradas.",
                previous_status,
            )
            return

        try:
            api_client._get_token()
        except GrupNucleoAPIError as e:
            self._gn_health_set_error(ICP, now_str, str(e)[:500], previous_status)
            return
        except Exception as e:
            self._gn_health_set_error(ICP, now_str, str(e)[:500], previous_status)
            return

        ICP.set_param("grupo_nucleo_integration.api_status", "ok")
        ICP.set_param("grupo_nucleo_integration.api_last_check", now_str)
        ICP.set_param("grupo_nucleo_integration.api_last_error", "")
        _logger.info("Grupo Núcleo API health check: OK")

    @api.model
    def _gn_health_set_error(self, ICP, now_str, error_msg, previous_status):
        """Record error status and notify the configured user on first failure."""
        ICP.set_param("grupo_nucleo_integration.api_status", "error")
        ICP.set_param("grupo_nucleo_integration.api_last_check", now_str)
        ICP.set_param("grupo_nucleo_integration.api_last_error", error_msg)
        _logger.warning("Grupo Núcleo API health check FAILED: %s", error_msg)

        if previous_status == "error":
            return

        notify_uid = int(
            ICP.get_param("grupo_nucleo_integration.gn_api_notify_user_id") or 0
        )
        if not notify_uid:
            return
        user = self.env["res.users"].sudo().browse(notify_uid)
        if not user.exists() or not user.partner_id:
            return
        self.env["mail.thread"].message_notify(
            partner_ids=user.partner_id.ids,
            body=_(
                "<b>Grupo Núcleo API: Error de conexión</b><br/>%s",
                error_msg,
            ),
            subject=_("Grupo Núcleo API: Error de conexión"),
        )
