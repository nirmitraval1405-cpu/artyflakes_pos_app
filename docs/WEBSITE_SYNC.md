# WooCommerce Website Sync

The POS already contains the WooCommerce integration. Its management page is `/woo/` and is limited to users with the admin role. Website orders remain available to staff from the POS navigation.

## Configure and run

1. Set `WOO_URL`, `WOO_KEY`, and `WOO_SECRET` in the project `.env` file. Use an HTTPS URL and a WooCommerce REST API key with read/write access. Never put the key values in source control.
2. From the project folder, apply database migrations before opening the Website Sync section in Django Admin:

   ```powershell
   .\venv\Scripts\python.exe manage.py migrate
   ```

   This also applies new catalog and product-sync tables/fields. If code is updated without running migrations, pages may raise a `ProgrammingError` because Django selects a column that does not exist yet. Confirm app migrations were applied with:

   ```powershell
   .\venv\Scripts\python.exe manage.py showmigrations inventory pos woosync
   ```

   All listed migrations should be marked `[X]`. If any are not, run `migrate` above, then restart the Django server if it is running.

3. Sign in as an administrator, open **Website Integration → Website Sync**, and use **Test connection**. Switch sync on to record the initial order cutoff, then use **Sync now** for the first run.
4. To run scheduled synchronization every two minutes while the Windows user is logged in, register the Task Scheduler task:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\static\js\register_sync_task.ps1
   ```

   The task invokes `manage.py sync_woo`; output is appended to `woosync.log` at the project root. Remove it with `-Remove`.

5. To receive immediate product updates, set a long random `WOO_WEBHOOK_SECRET` and register WooCommerce webhooks for product created, updated, and deleted at `/woo/webhook/products/`, using that secret. The scheduled sync also polls product changes. Variable products are deliberately sent to the review list and are not imported as simple products.
6. POS product images are uploaded to WordPress Media when changed. Configure `WOO_MEDIA_USER` and `WOO_MEDIA_APP_PASSWORD` with a WordPress account and an Application Password to enable uploads; WooCommerce REST API keys alone do not authenticate the WordPress Media endpoint. Leave both unset if local POS image upload is not needed.

WooCommerce tax classes are retained on the product, but they are not automatically translated into POS GST rates. Review tax/GST settings on imported products; the sync does not overwrite the POS GST rate.

The sync page and history show the actual result of each run, including newly imported and failed orders. The cutoff is preserved when sync is switched off and back on. Refreshing Website Orders only reloads locally imported orders; it does not call WooCommerce.

## Django Administration

The **Website Sync** section in `/admin/` exposes the sync state, runs, imported orders, order items, and product links as read-only records. WooCommerce credentials remain in `.env` and are intentionally not exposed in Django Administration.
