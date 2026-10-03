import unittest
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
        res = self.client.post("/api/system/clean_test_data")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("cleared_jobs", data)
        self.assertIn("deleted_work_files", data)


if __name__ == "__main__":
    unittest.main()
