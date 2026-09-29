"""
Pasul 1 din docs/superpowers/plans/2026-09-30-next-steps.md.

The commit that added GSC history (c8f72b4) inserted upsert_gsc_history
between the route decorator and sync_property. The decorator glued itself to
the wrong function: POST /api/gsc/properties/{id}/sync started expecting
db_path/pid/q_daily/p_daily as request parameters instead of running the
actual sync -- exposing an attacker-controlled db_path parameter, and giving
the Sync button in api/templates/gsc.html a 422 on every click.

This pins the route to sync_property so the bug can't silently come back.
"""
import unittest

from api.main import app


class TestGscSyncRouteIsWiredCorrectly(unittest.TestCase):
    def _route(self, path: str, method: str):
        for route in app.routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
                return route
        return None

    def test_sync_route_points_at_sync_property(self):
        from api.routes.gsc.oauth_sync import sync_property

        route = self._route("/api/gsc/properties/{property_id}/sync", "POST")
        self.assertIsNotNone(route, "POST /api/gsc/properties/{property_id}/sync is not registered")
        self.assertIs(route.endpoint, sync_property)

    def test_upsert_gsc_history_is_not_registered_as_a_route(self):
        from api.routes.gsc.oauth_sync import upsert_gsc_history

        for route in app.routes:
            self.assertIsNot(
                getattr(route, "endpoint", None), upsert_gsc_history,
                f"upsert_gsc_history must not be a route endpoint (found at {route.path})",
            )

    def test_sync_route_does_not_expose_a_db_path_parameter(self):
        """The regressed route took an attacker-controlled db_path -- guard the request shape too."""
        route = self._route("/api/gsc/properties/{property_id}/sync", "POST")
        self.assertIsNotNone(route)
        param_names = set(route.endpoint.__code__.co_varnames[: route.endpoint.__code__.co_argcount])
        self.assertNotIn("db_path", param_names)


if __name__ == "__main__":
    unittest.main()
