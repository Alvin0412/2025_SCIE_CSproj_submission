from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import CreditLedgerEntry, PlanTier, Role
from .services import assign_role
from .subscriptions import InsufficientCredits, ensure_subscription_state, spend_credits

User = get_user_model()


class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_assigns_default_role_and_returns_tokens(self):
        payload = {
            "email": "viewer@example.com",
            "password": "StrongPass123!",
            "first_name": "View",
            "last_name": "Er",
        }
        response = self.client.post("/api/accounts/auth/register/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        user_id = data["user"]["id"]
        self.assertIn("access_token", data)
        self.assertIn("refresh_token", data)
        viewer = Role.objects.get(slug="viewer")
        self.assertTrue(
            User.objects.get(pk=user_id).role_memberships.filter(role=viewer, is_active=True).exists()
        )

    def test_role_assignment_requires_permissions(self):
        target = User.objects.create_user(email="target@example.com", password="StrongPass123!")
        admin = User.objects.create_user(email="admin@example.com", password="StrongPass123!")
        admin_role = Role.objects.get(slug="admin")
        assign_role(admin, admin_role)

        login = self.client.post(
            "/api/accounts/auth/login/",
            {"email": "admin@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.content)
        token = login.json()["access_token"]

        assign_response = self.client.post(
            "/api/accounts/memberships/assign/",
            {"user_id": str(target.id), "role_slug": "editor"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(assign_response.status_code, 201, assign_response.content)
        self.assertTrue(
            target.role_memberships.filter(role__slug="editor", is_active=True).exists()
        )

        viewer = User.objects.create_user(email="viewer2@example.com", password="StrongPass123!")
        login_viewer = self.client.post(
            "/api/accounts/auth/login/",
            {"email": "viewer2@example.com", "password": "StrongPass123!"},
            format="json",
        )
        token_viewer = login_viewer.json()["access_token"]
        forbidden = self.client.post(
            "/api/accounts/memberships/assign/",
            {"user_id": str(target.id), "role_slug": "viewer"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token_viewer}",
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.content)


class AccountSubscriptionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.plan, _ = PlanTier.objects.get_or_create(
            slug="free",
            defaults={
                "name": "Free",
                "description": "Default plan",
                "monthly_price": Decimal("0.00"),
                "monthly_credits": 100,
                "concurrency_limit": 1,
                "is_active": True,
                "is_default": True,
                "features": ["basic-search"],
            },
        )
        if not self.plan.is_default:
            self.plan.is_default = True
            self.plan.save(update_fields=["is_default"])
        self.user = User.objects.create_user(email="sub@example.com", password="StrongPass123!")
        self.cycle = ensure_subscription_state(self.user)

    def test_me_endpoint_includes_plan_and_credit_snapshot(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/accounts/auth/me/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["plan"]["slug"], self.plan.slug)
        self.assertEqual(data["credits"]["total_remaining"], self.plan.monthly_credits)
        self.assertTrue(data["status"]["has_credits"])
        self.client.force_authenticate(user=None)

    def test_spend_credits_consumes_promo_before_monthly(self):
        promo = CreditLedgerEntry.objects.create(
            user=self.user,
            source_type=CreditLedgerEntry.SOURCE_PROMO,
            amount=2,
            remaining_amount=2,
        )
        monthly_entry = CreditLedgerEntry.objects.filter(
            user=self.user, source_type=CreditLedgerEntry.SOURCE_MONTHLY, cycle=self.cycle
        ).first()
        spend_credits(self.user, credits=3, reason="ai_search")
        promo.refresh_from_db()
        monthly_entry.refresh_from_db()
        self.assertEqual(promo.remaining_amount, 0)
        self.assertEqual(monthly_entry.remaining_amount, self.plan.monthly_credits - 1)

    def test_spend_credits_raises_when_balance_is_empty(self):
        CreditLedgerEntry.objects.filter(user=self.user).update(remaining_amount=0)
        with self.assertRaises(InsufficientCredits):
            spend_credits(self.user, credits=1, reason="ai_search")

    def test_account_stats_defaults_to_billing_cycle_range(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/accounts/stats/?range=billing_cycle")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["period"]["label"], "billing_cycle")
        self.assertTrue(data["period"]["start"].startswith(self.cycle.cycle_start.isoformat()[:19]))
        self.assertTrue(data["period"]["end"].startswith(self.cycle.cycle_end.isoformat()[:19]))
        self.client.force_authenticate(user=None)
