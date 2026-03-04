===============
Grupo Núcleo Integration
===============

.. |badge1| image:: https://img.shields.io/badge/maturity-Stable-brightgreen
    :target: https://odoo-community.org/page/development-status
    :alt: Stable
.. |badge2| image:: https://img.shields.io/badge/licence-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3
.. |badge3| image:: https://onlyone.odoo.com/web/image/website/1/logo/OnlyOne%20Soft?unique=dccda5b
    :target: https://onlyone.odoo.com/
    :alt: OnlyOne

|badge1| |badge2| |badge3|

Módulo de integración con la API de Grupo Núcleo S.A. (mayorista de electrónica de consumo) para **Odoo 17**. Permite sincronizar el catálogo de productos (precios en supplierinfo, stock del proveedor, dimensiones para envío/Zippin, imágenes, categorías, descripciones), enviar pedidos a nombre del mayorista al confirmar la orden de venta y crear automáticamente la orden de compra en Odoo para el flujo nativo de recepción (picking).

**Dependencias:** ``website_sale``, ``zippin`` (dimensiones alto_cm, ancho_cm, largo_cm → zippin_product_* y volume), ``product_replenishment_cost`` (OCA, para costo desde supplierinfo).

**Tabla de contenidos**

.. contents::
   :local:

Install
=======

* Copiar el módulo en un addons path de Odoo 17 (o clonar el repositorio en un path de addons).
* Asegurarse de tener instalados los módulos **website_sale** y **zippin** (o instalar las dependencias desde Odoo).
* En Odoo: Aplicaciones > Actualizar lista de aplicaciones; buscar "Grupo Núcleo Integration" e instalar.

Configure
=========

1. Ir a **Ajustes > Ventas > Grupo Núcleo**.
2. Completar **API ID (numérico)**, **Usuario** y **Contraseña** (credenciales proporcionadas por Grupo Núcleo).
3. Opcional: **URL de la API** (por defecto https://api.gruponucleosa.com).
4. Opcional: **Proveedor para órdenes de compra** (partner usado en las OC generadas y en las líneas de product.supplierinfo al sincronizar).
5. Opcional: **Stock del proveedor a usar**: Mar del Plata (stock_mdp), Buenos Aires (stock_caba) o suma de ambos; define qué stock se guarda en el campo *Stock Grupo Núcleo* para filtros/crons.
6. Opcional: **Categoría padre ecommerce**: categoría raíz bajo la cual se crean categoría/subcategoría desde la API para publicar en la tienda.
7. Opcional: **Habilitar venta sin stock**: si está activo, los productos GN tendrán *Permitir pedido sin stock* en la tienda (allow_out_of_stock_order).
8. Opcional: **Umbral stock para publicar en tienda**: productos GN con *Stock Grupo Núcleo* mayor que este valor se publican; con stock ≤ umbral se despublican (usado por la acción de servidor/cron de publicar por stock). Por defecto 3.
9. Opcional: activar **Enviar a GN al confirmar** si se desea el envío automático al confirmar la orden de venta.
10. Opcional: **Usuario a notificar si la API falla**: usuario que recibirá un mensaje interno en Discuss cuando el health check detecte que la API no responde (404, 502, etc.). El indicador **Estado API** (● API OK / ● API Error / Sin verificar) y la fecha del último chequeo se muestran en Ajustes.
11. Usar **Probar conexión** para validar credenciales y **Sincronizar catálogo ahora** para importar/actualizar productos.

Usage
=====

**Sincronizar catálogo**

* Desde Ajustes > Grupo Núcleo: botón **Sincronizar catálogo ahora** (sincronización completa).
* La API de GN solo expone GetCatalog (no hay endpoint separado para precio/stock). El sync completo del catálogo funciona así:

  * **Grupo Núcleo: Activar sync diario de catálogo** — cron **1 vez al día**. Marca “sync solicitado hoy” y activa el cron de lotes de catálogo. No descarga el catálogo.
  * **Grupo Núcleo: Lotes de sync catálogo** — corre **cada 5 minutos** solo cuando el sync fue solicitado ese día. Cada ejecución: GetCatalog + un lote de 80 ítems (crea/actualiza productos, supplierinfo, stock, dimensiones, etc.). Al terminar todo el catálogo deja un flag; el cron **Desactivar cron de lotes si corresponde** (cada 10 min) desactiva este cron para que no vuelva a correr hasta el trigger del día siguiente.
  * **Grupo Núcleo: Activar sync precio y stock** — cron **cada 12 horas**. Marca “sync precio/stock solicitado hoy” y activa el cron de lotes de precio/stock. Misma idea que el catálogo: ejecuciones rápidas hasta completar y luego se desactiva vía el mismo cron de cleanup.
  * **Grupo Núcleo: Lotes de sync precio y stock** — corre **cada 5 minutos** solo cuando el sync precio/stock fue solicitado. Solo actualiza productos existentes: supplierinfo (precio), *Stock Grupo Núcleo*, dimensiones. No crea productos. Al completar, el cron de cleanup lo desactiva.
  * **Grupo Núcleo: Desactivar cron de lotes si corresponde** — cada 10 minutos. Si algún batch cron (catálogo o precio/stock) terminó y pidió desactivación, este cron (que es otro registro) hace el ``write(active=False)`` para evitar el error de bloqueo.
  * **Grupo Núcleo: Health check API** — cada 1 hora. Hace una verificación liviana (login) a la API; actualiza el indicador en Ajustes (API OK / API Error) y, si hay error, notifica al usuario configurado por Discuss (solo cuando el estado pasa a error, para no repetir el mensaje cada hora).
  * El botón **Sincronizar catálogo ahora** en Ajustes ejecuta **un solo lote** (80 ítems) de forma manual.
  * **Grupo Núcleo: Publicar/despublicar productos por stock** — cron cada 6 horas que ejecuta una **acción de servidor** (código editable en Ajustes > Técnico > Automatización > Acciones de servidor). Por defecto: publica productos GN con *Stock Grupo Núcleo* > umbral y despublica los que tienen stock ≤ umbral. El administrador puede editar la acción para añadir filtros o condiciones.

* Por cada ítem del catálogo (sync completo): se crea o actualiza el producto con nombre, referencia, código de barras, descripción de ventas (item_desc_0), descripción eCommerce (item_desc_1 + item_desc_2), peso, dimensiones (alto_cm, ancho_cm, largo_cm → volume y campos Zippin), imagen, categoría interna y opcionalmente categorías públicas ecommerce; se guarda el **stock del proveedor** en *Stock Grupo Núcleo* según la opción configurada, y se crea/actualiza la línea de **product.supplierinfo** del proveedor GN con el precio de la API. El módulo usa **product_replenishment_cost** (OCA): se setea ``replenishment_cost_type = "supplier_price"`` y tras cada batch de precio/stock se llama ``_update_cost_from_replenishment_cost()`` para que el costo contable (standard_price) se derive del supplierinfo. Así, listas de precios basadas en costos se mantienen actualizadas.

**Enviar pedidos a Grupo Núcleo**

1. En la orden de venta, marcar **Enviar a Grupo Núcleo** (solo en borrador).
2. Confirmar la orden: se llama a la API (CheckoutConfirm + NewSelfSaleOrder) y se crea la orden de compra vinculada para recepción.
3. Máximo 15 líneas por pedido (límite de la API). Los productos deben tener **Grupo Núcleo Item ID** (asignado al sincronizar el catálogo).

**Orden de compra y recepción**

* Tras enviar el pedido a GN se crea automáticamente una orden de compra confirmada, enlazada a la orden de venta, para usar el flujo estándar de recepción (entrada de mercadería / picking).

Version
=======

* **17.0.2.0.0** — Odoo 17. Sync catálogo y precio/stock por lotes (trigger + batch + cleanup), dimensiones Zippin, descripciones venta/eCommerce, venta sin stock, publicar por stock (acción de servidor editable), health check de API cada hora con indicador en Ajustes y notificación por Discuss, costo de reposición desde supplierinfo vía ``product_replenishment_cost``.

Known issues / Roadmap
======================

* La API de Grupo Núcleo limita a 15 ítems por pedido en CheckoutConfirm.
* El campo *nota* del pedido se trunca a 350 caracteres en la API.

Bug Tracker
===========

* Soporte: https://onlyone.odoo.com/

Credits
=======

Authors
~~~~~~~

* Be OnlyOne

Contributors
~~~~~~~~~~~~

* `Be OnlyOne <https://onlyone.odoo.com/>`_

  * Matías Bressanello

Maintainers
~~~~~~~~~~~

This module is maintained by Be OnlyOne.
