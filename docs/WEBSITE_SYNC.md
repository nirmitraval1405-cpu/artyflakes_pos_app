# WooCommerce Website Sync

The POS already contains the WooCommerce integration. Its management page is `/woo/` and is limited to users with the admin role. Website orders remain available to staff from the POS navigation.

## Configure and run

1. Set `WOO_URL`, `WOO_KEY`, and `WOO_SECRET` in the project `.env` file. Use an HTTPS URL and a WooCommerce REST API key with read/write access. Never put the key values in source control.
2. From the project folder, apply database migrations before opening the Website Sync section in Django Admin:

   ```powershell
   .\venv\Scripts\python.exe manage.py migrate
   ```

   In particular, migration `woosync.0002_syncrun_orders_failed` adds the `orders_failed` column to the sync-run table. If the code is updated but this migration has not been applied to the configured database, the Admin sync-history page raises a `ProgrammingError` because Django selects a column that does not exist yet. Confirm it was applied with:

   ```powershell
   .\venv\Scripts\python.exe manage.py showmigrations woosync
   ```

   The `0002_syncrun_orders_failed` row should be marked `[X]`. If it is not, run `migrate` above, then restart the Django server if it is running.

3. Sign in as an administrator, open **Website Integration → Website Sync**, and use **Test connection**. Switch sync on to record the initial order cutoff, then use **Sync now** for the first run.
4. To run scheduled synchronization every two minutes while the Windows user is logged in, register the Task Scheduler task:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\static\js\register_sync_task.ps1
   ```

   The task invokes `manage.py sync_woo`; output is appended to `woosync.log` at the project root. Remove it with `-Remove`.

The sync page and history show the actual result of each run, including newly imported and failed orders. The cutoff is preserved when sync is switched off and back on. Refreshing Website Orders only reloads locally imported orders; it does not call WooCommerce.

## Django Administration

The **Website Sync** section in `/admin/` exposes the sync state, runs, imported orders, order items, and product links as read-only records. WooCommerce credentials remain in `.env` and are intentionally not exposed in Django Administration.
