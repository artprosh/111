"""Tests for phone-based standalone app sessions (no SMS network calls)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from db import Database
from webapp.standalone_auth import StandaloneAuth


class StandaloneAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {
            "APP_AUTH_SECRET": "test-auth-secret-that-is-long-enough-123456",
            "SMSRU_API_ID": "not-used-in-these-tests",
            "APP_ADMIN_PHONE": "+7 999 000-00-01",
            "APP_ADMIN_ID": "987654321",
        }
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = Database(Path(self.tmp.name) / "auth.sqlite3")
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        self.auth = StandaloneAuth(self.db)

    def test_phone_normalization_accepts_russian_formats(self):
        from webapp.standalone_auth import normalize_phone

        self.assertEqual(normalize_phone("8 (999) 000-00-02"), ("+79990000002", "79990000002"))
        self.assertEqual(normalize_phone("9990000002"), ("+79990000002", "79990000002"))

    def test_verified_customer_gets_session_cookie_token_and_new_internal_id(self):
        phone = "+79990000002"
        code = "014725"
        now = int(time.time())
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO app_auth_codes(phone, code_hash, expires_at, attempts, first_name) VALUES (?, ?, ?, 0, '')",
                (phone, self.auth._digest("otp", phone + ":" + code), now + 300),
            )
        result = self.auth.verify_code(phone, code, "Анна")
        self.assertEqual(result.user.first_name, "Анна")
        self.assertNotEqual(result.user.id, 987654321)
        self.assertEqual(self.auth.user_for_token(result.token).id, result.user.id)
        self.auth.logout(result.token)
        self.assertIsNone(self.auth.user_for_token(result.token))

    def test_admin_phone_maps_only_to_configured_admin_id(self):
        phone = "+79990000001"
        code = "902614"
        now = int(time.time())
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO app_auth_codes(phone, code_hash, expires_at, attempts, first_name) VALUES (?, ?, ?, 0, '')",
                (phone, self.auth._digest("otp", phone + ":" + code), now + 300),
            )
        result = self.auth.verify_code(phone, code)
        self.assertEqual(result.user.id, 987654321)


if __name__ == "__main__":
    unittest.main()
