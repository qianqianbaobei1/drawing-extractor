import unittest
import test_support  # noqa: F401
from fastapi.testclient import TestClient
from app import app
from db import init_db


class TestAuthAndCleanup(unittest.TestCase):
    def setUp(self):
        init_db()
        self.client = TestClient(app)

    def test_default_admin_login(self):
        # 1. 登录默认系统管理员
        res = self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("token").startswith("tk_"))
        user = data.get("user")
        self.assertEqual(user["username"], "admin")
        self.assertEqual(user["display_name"], "系统工程师")
        self.assertIn("电柜智核", user["tenant_name"])

        # 2. 验证 /api/auth/me
        token = data["token"]
        me_res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_res.status_code, 200)
        me_data = me_res.json()
        self.assertTrue(me_data.get("authenticated"))
        self.assertEqual(me_data["user"]["username"], "admin")

    def test_login_failure(self):
        # 错误密码登录
        res = self.client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(res.status_code, 401)

    def test_register_and_logout(self):
        import secrets
        uname = f"engineer_{secrets.token_hex(4)}"
        # 注册新企业与用户
        res = self.client.post("/api/auth/register", json={
            "username": uname,
            "password": "testpassword123",
            "display_name": "李工",
            "tenant_name": "华东智能成套制造工区"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        token = data["token"]
        user = data["user"]
        self.assertEqual(user["username"], uname)
        self.assertEqual(user["display_name"], "李工")
        self.assertEqual(user["tenant_name"], "华东智能成套制造工区")

        # 验证 Me 接口
        me_res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_res.status_code, 200)
        self.assertTrue(me_res.json()["authenticated"])

        # 登出
        out_res = self.client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(out_res.status_code, 200)

        # 再次查 Me，Token 应已失效
        me_res_after = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertFalse(me_res_after.json()["authenticated"])

    def test_system_clean_test_data(self):
        # 1. 匿名未认证调用应被 403 严格拒绝
        unauth_res = self.client.post("/api/system/clean_test_data")
        self.assertEqual(unauth_res.status_code, 403)
        self.assertIn("权限不足", unauth_res.json()["detail"])

        # 2. 以系统管理员身份登录并携带 Token，成功清空测试数据
        login_res = self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(login_res.status_code, 200)
        token = login_res.json()["token"]

        res = self.client.post("/api/system/clean_test_data", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("cleared_jobs", data)
        self.assertIn("deleted_work_files", data)

    def test_system_clean_production_lock(self):
        import os
        old_env = os.environ.get("ENV")
        try:
            os.environ["ENV"] = "production"
            res = self.client.post("/api/system/clean_test_data")
            self.assertEqual(res.status_code, 403)
            self.assertIn("生产环境保护", res.json()["detail"])
        finally:
            if old_env is not None:
                os.environ["ENV"] = old_env
            else:
                os.environ.pop("ENV", None)

    def test_pbkdf2_hash_and_legacy_upgrade(self):
        import hashlib
        from db import _hash_password, _verify_password, _get_conn
        # 1. 验证新密码哈希采用 PBKDF2 强哈希格式
        pwd_hash = _hash_password("mypassword")
        self.assertTrue(pwd_hash.startswith("pbkdf2:sha256:100000$"))
        valid, needs_rehash = _verify_password("mypassword", pwd_hash)
        self.assertTrue(valid)
        self.assertFalse(needs_rehash)

        # 2. 模拟旧版单轮 SHA-256 哈希
        legacy_hash = hashlib.sha256("cabinet_core_salt_v2:legacy_pass".encode("utf-8")).hexdigest()
        valid_legacy, needs_rehash_legacy = _verify_password("legacy_pass", legacy_hash)
        self.assertTrue(valid_legacy)
        self.assertTrue(needs_rehash_legacy)

        # 3. 验证通过旧哈希登录后数据库自动平滑升级为 PBKDF2
        conn = _get_conn()
        with conn:
            conn.execute("""
            INSERT OR REPLACE INTO users (id, tenant_id, username, password_hash, display_name, role, created_at)
            VALUES ('test_legacy_user_id', 'default', 'legacy_user', ?, '旧员工', 'operator', datetime('now', 'localtime'))
            """, (legacy_hash,))

        res = self.client.post("/api/auth/login", json={"username": "legacy_user", "password": "legacy_pass"})
        self.assertEqual(res.status_code, 200)

        # 查询数据库内哈希是否已升级
        cur = conn.execute("SELECT password_hash FROM users WHERE username = 'legacy_user'")
        row = cur.fetchone()
        self.assertTrue(row["password_hash"].startswith("pbkdf2:sha256:100000$"))

    def test_api_key_public_settings_mask(self):
        import store
        orig = store.settings().get("vision_api_key", "")
        try:
            store.save_settings({"vision_api_key": "sk-1234567890abcdef"})
            pub = store.public_settings()
            self.assertTrue(pub["vision_api_key_set"])
            self.assertEqual(pub["vision_api_key_hint"], "***")
            self.assertNotIn("cdef", pub["vision_api_key_hint"])
        finally:
            store.save_settings({"vision_api_key": orig})


if __name__ == "__main__":
    unittest.main()
