import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ics_agent.app.main import app


class AdminPageTest(unittest.TestCase):
    def test_admin_page_is_served(self) -> None:
        response = TestClient(app).get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("<!doctype html>", response.text.lower())

    def test_admin_page_declares_favicon(self) -> None:
        response = TestClient(app).get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn('rel="icon"', response.text)
        self.assertIn('href="/admin/favicon.svg"', response.text)

    def test_admin_favicon_is_served(self) -> None:
        response = TestClient(app).get("/admin/favicon.svg")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn("<svg", response.text)

    def test_admin_login_uses_configured_settings(self) -> None:
        with patch(
            "ics_agent.app.admin.routes.authenticate_admin",
            return_value="test-token",
        ) as authenticate_admin:
            response = TestClient(app).post(
                "/auth/login",
                json={"username": "admin", "password": "secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["access_token"], "test-token")
        authenticate_admin.assert_called_once()
        self.assertEqual(authenticate_admin.call_args.args[:2], ("admin", "secret"))
        self.assertIsNotNone(authenticate_admin.call_args.kwargs["settings"])


if __name__ == "__main__":
    unittest.main()
