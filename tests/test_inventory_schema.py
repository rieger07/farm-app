import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from homestead.database import create_user
from inventory.db import get_connection, init_inventory_db
from inventory.merchants import (
    MANAGER,
    OWNER,
    STAFF,
    add_member,
    can_access,
    create_merchant,
    get_membership,
    home_page,
    list_members,
    remove_member,
)
from inventory.stock import add_ledger_entry, list_stock


def _add_sku(merchant_id, name="Clover honey", price=900, threshold=10):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO skus (merchant_id, zettle_product_uuid, name, price_cents,"
        " low_stock_threshold, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (merchant_id, name, name, price, threshold, datetime.utcnow().isoformat()),
    )
    conn.commit()
    sku_id = cur.lastrowid
    conn.close()
    return sku_id


class InventorySchemaTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        os.environ["FARM_DB_PATH"] = os.path.join(self._dir.name, "t.db")
        init_inventory_db()
        self.zach = create_user("zach", "password1", "Zach")["id"]
        self.marty = create_user("marty", "password1", "Marty")["id"]
        self.mid = create_merchant("Willow Creek Apiary", self.zach)

    def tearDown(self):
        self._dir.cleanup()

    def test_wal_enabled(self):
        conn = get_connection()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_roles_and_home_pages(self):
        add_member(self.mid, self.marty, STAFF)
        self.assertEqual(get_membership(self.marty)["role"], STAFF)
        self.assertEqual(home_page(STAFF), "quick_sale")
        self.assertEqual(home_page(OWNER), "inventory")
        self.assertFalse(can_access(STAFF, "inventory"))
        self.assertTrue(can_access(STAFF, "quick_sale"))
        self.assertTrue(can_access(MANAGER, "sales"))
        self.assertFalse(can_access(MANAGER, "settings"))
        self.assertEqual(len(list_members(self.mid)), 2)

    def test_last_owner_cannot_be_removed(self):
        with self.assertRaises(ValueError):
            remove_member(self.mid, self.zach)
        add_member(self.mid, self.marty, OWNER)
        remove_member(self.mid, self.zach)
        self.assertIsNone(get_membership(self.zach))

    def test_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            add_member(self.mid, self.marty, "admin")

    def test_ledger_derives_stock_and_allows_negative(self):
        sku = _add_sku(self.mid, threshold=10)
        self.assertEqual(list_stock(self.mid)[0]["status"], "out_of_stock")
        add_ledger_entry(self.mid, sku, 20, "initial")
        self.assertEqual(list_stock(self.mid)[0]["status"], "in_stock")
        add_ledger_entry(self.mid, sku, -12, "adjustment")
        row = list_stock(self.mid)[0]
        self.assertEqual((row["on_hand"], row["status"]), (8, "low"))
        add_ledger_entry(self.mid, sku, -8, "adjustment")
        row = list_stock(self.mid)[0]
        self.assertEqual((row["on_hand"], row["status"]), (0, "out_of_stock"))
        add_ledger_entry(self.mid, sku, -12, "adjustment")
        row = list_stock(self.mid)[0]
        self.assertEqual((row["on_hand"], row["status"]), (-12, "count_needed"))

    def test_sale_idempotency_by_external_id(self):
        conn = get_connection()
        args = (self.mid, "zettle", "abc-uuid", "2026-11-14T12:00:00", 800, "2026-11-14")
        sql = (
            "INSERT INTO sales (merchant_id, source, external_id, occurred_at,"
            " total_cents, created_at) VALUES (?, ?, ?, ?, ?, ?)"
        )
        conn.execute(sql, args)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, args)
        conn.close()


if __name__ == "__main__":
    unittest.main()
