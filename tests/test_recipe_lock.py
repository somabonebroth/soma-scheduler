"""Recipe lock (recipes.py, 2026-10-02).

Recipes are the one source every cascade reads, so editing needs RECIPE_PASSWORD
on top of the manager login. Every write route refuses until the session is
unlocked; no password configured = locked for everyone.
"""
import os
import unittest
from unittest import mock

import app
import recipes  # noqa: F401  (registers the blueprint routes)

WRITES = [
    ("POST", "/api/recipes"),
    ("PUT", "/api/recipes/X"),
    ("POST", "/api/recipes/X/apply-certification"),
    ("DELETE", "/api/recipes/X"),
    ("POST", "/api/recipes/X/duplicate"),
    ("POST", "/api/recipes/X/archive"),
    ("POST", "/api/recipes/X/unarchive"),
    ("POST", "/api/recipes/migrate-all"),
    ("POST", "/api/recipes/X/photo"),
    ("POST", "/api/recipes/order"),
    ("POST", "/api/recipes/upload"),
    ("POST", "/api/recipes/upload-json"),
]


class RecipeLock(unittest.TestCase):
    def client(self, role="manager", unlocked=False):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = role
            if unlocked:
                s["recipe_unlocked"] = True
        return c

    def test_every_write_route_in_the_blueprint_is_locked(self):
        """Walks the live route map, so a future write route can't slip past."""
        found = []
        for r in app.app.url_map.iter_rules():
            if not r.endpoint.startswith("recipes.") or r.endpoint in ("recipes.unlock_recipes", "recipes.lock_recipes"):
                continue
            for m in r.methods & {"POST", "PUT", "DELETE", "PATCH"}:
                found.append((m, r.rule.replace("<path:name>", "X")))
        self.assertEqual(sorted(found), sorted(WRITES))

    def test_manager_without_unlock_is_refused(self):
        with mock.patch.dict(os.environ, {"RECIPE_PASSWORD": "rpw"}):
            c = self.client()
            for method, url in WRITES:
                r = c.open(url, method=method, json={})
                self.assertEqual(r.status_code, 403, (method, url))
                self.assertIn("locked", r.get_json()["error"])

    def test_production_is_refused_even_if_flag_forged(self):
        with mock.patch.dict(os.environ, {"RECIPE_PASSWORD": "rpw"}):
            c = self.client(role="production", unlocked=True)
            self.assertEqual(c.put("/api/recipes/X", json={}).status_code, 403)
            self.assertEqual(c.post("/api/recipes/unlock", json={"password": "rpw"}).status_code, 403)

    def test_unlock_wrong_then_right_then_lock(self):
        with mock.patch.dict(os.environ, {"RECIPE_PASSWORD": "rpw"}):
            c = self.client()
            self.assertEqual(c.post("/api/recipes/unlock", json={"password": "nope"}).status_code, 401)
            self.assertEqual(c.post("/api/recipes/order", json={}).status_code, 403)
            self.assertEqual(c.post("/api/recipes/unlock", json={"password": "rpw"}).status_code, 200)
            self.assertNotEqual(c.post("/api/recipes/order", json={}).status_code, 403)
            c.post("/api/recipes/lock")
            self.assertEqual(c.post("/api/recipes/order", json={}).status_code, 403)

    def test_no_password_configured_locks_everyone(self):
        env = {k: v for k, v in os.environ.items() if k != "RECIPE_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True):
            c = self.client(unlocked=True)
            self.assertEqual(c.put("/api/recipes/X", json={}).status_code, 403)
            self.assertEqual(c.post("/api/recipes/unlock", json={"password": ""}).status_code, 403)

    def test_page_renders_read_only_until_unlocked(self):
        with mock.patch.dict(os.environ, {"RECIPE_PASSWORD": "rpw"}):
            locked = self.client().get("/recipes").get_data(as_text=True)
            self.assertIn("var READ_ONLY = true", locked)
            self.assertIn("Unlock editing", locked)
            self.assertNotIn("+ Add recipe", locked)
            unlocked = self.client(unlocked=True).get("/recipes").get_data(as_text=True)
            self.assertIn("var READ_ONLY = false", unlocked)
            self.assertIn("Lock editing", unlocked)
            floor = self.client(role="production").get("/recipes").get_data(as_text=True)
            self.assertNotIn("Unlock editing", floor)


if __name__ == "__main__":
    unittest.main()
