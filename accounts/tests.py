from django.test import Client, TestCase
from django.urls import reverse

from .models import User


class StaffLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="login-user",
            password="test-password",
        )
        self.url = reverse("accounts:login")

    def test_login_page_has_password_toggle_and_remember_option(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'id="togglePassword"')
        self.assertContains(response, 'name="remember_me"')

    def test_valid_login_preserves_next_redirect_and_remember_setting(self):
        response = self.client.post(
            self.url,
            {
                "username": self.user.username,
                "password": "test-password",
                "remember_me": "on",
                "next": "/reports/",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/reports/")
        self.assertFalse(self.client.session.get_expire_at_browser_close())
        self.assertEqual(self.client.session.get_expiry_age(), 14 * 24 * 60 * 60)

    def test_unchecked_remember_option_uses_browser_session(self):
        response = self.client.post(
            self.url,
            {"username": self.user.username, "password": "test-password"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_invalid_credentials_show_generic_error(self):
        response = self.client.post(
            self.url,
            {"username": self.user.username, "password": "incorrect"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "We couldn't sign you in.")

    def test_invalid_login_csrf_token_refreshes_form_without_authenticating(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.get(self.url)

        response = csrf_client.post(
            f"{self.url}?next=%2Freports%2F",
            {
                "csrfmiddlewaretoken": "invalid-token",
                "username": self.user.username,
                "password": "test-password",
                "next": "/reports/",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{self.url}?csrf_retry=1&next=%2Freports%2F")
        self.assertNotIn("_auth_user_id", csrf_client.session)

        response = csrf_client.get(response["Location"])
        self.assertContains(response, "Your security token was refreshed.")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
