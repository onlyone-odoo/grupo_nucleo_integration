# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

{
    "name": "Grupo Núcleo Integration",
    "summary": "Integration with Grupo Núcleo API (catalog sync, order submission, purchase order for receiving).",
    "author": "Be OnlyOne",
    "maintainers": ["onlyone-odoo"],
    "website": "https://onlyone.odoo.com/",
    "license": "LGPL-3",
    "category": "Inventory/Purchase",
    "version": "17.0.2.0.0",
    "development_status": "Production/Stable",
    "application": False,
    "installable": True,
    "external_dependencies": {
        "python": [],
        "bin": [],
    },
    "depends": [
        "sale",
        "product",
        "stock",
        "purchase",
        "purchase_stock",
        "website_sale",
        "product_replenishment_cost",
        "zippin",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/cron_data.xml",
        "views/res_config_settings_views.xml",
        "views/product_template_views.xml",
        "views/sale_order_views.xml",
        "views/purchase_order_views.xml",
    ],
}
